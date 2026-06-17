"""
Finch local API v0.2 — localhost-only HTTP surface for preview and guarded cart ops.

Future integrations (WhatsApp webhook, Nest, Crow) call this API on Raven; it reuses
Finch core modules and never performs checkout or payment.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from finch import __version__
from finch.cart_choice import (
    NeedsChoiceOutcome,
    execute_pending_choice,
    prepare_add_or_needs_choice,
    process_add_list_with_choice,
    rerun_pending_search,
)
from finch.cart_ops import (
    CartAttempt,
    CartGuardError,
    CartResolveError,
    check_trip_duplicate,
    ensure_fresh_user_token,
    execute_cart_add,
    live_cart_enabled,
    parse_add_item,
    record_cart_activity,
    record_successful_trip_add,
    require_live_cart,
    require_saved_token,
    resolve_cart_item,
)
from finch.trip_ledger import (
    TripItemRecord,
    format_added_list,
    get_or_create_open_trip,
    list_added_today,
    list_trip_items,
    reset_trip,
    undo_last_trip_item,
)
from finch.pending_selection import clear_pending_selection, get_pending_selection
from finch.aliases import delete_aliases_matching_normalized, get_all_aliases, lookup_alias
from finch.preferences import (
    alias_preference_key,
    forget_preference,
    format_preferences_list,
    get_preference_text,
    preference_to_dict,
    prepare_change_preference,
)
from finch.env_util import load_env
from finch.kroger_client import KrogerAuthError, KrogerError, load_kroger_client_from_env
from finch.preview import build_preview

FINCH_KEY_HEADER = "X-Finch-Key"


def _api_test_mode() -> bool:
    return os.getenv("FINCH_API_TEST_MODE", "").strip().lower() in ("1", "true", "yes")


def _api_key() -> str:
    return os.getenv("FINCH_API_KEY", "").strip()


def _validate_startup() -> None:
    if _api_test_mode():
        return
    if not _api_key():
        raise RuntimeError(
            "FINCH_API_KEY is required to start the Finch local API. "
            "Set FINCH_API_KEY in .env or FINCH_API_TEST_MODE=1 for tests."
        )


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    load_env(force=True)
    _validate_startup()
    yield


def _attempt_to_dict(attempt: CartAttempt) -> dict[str, Any]:
    return {
        "requested_item": attempt.requested_item,
        "normalized_name": attempt.normalized_name,
        "alias_name": attempt.alias_name,
        "upc": attempt.upc,
        "product_id": attempt.product_id,
        "quantity": attempt.quantity,
        "modality": attempt.modality,
        "status": attempt.status.value,
    }


async def require_finch_key(
    x_finch_key: Annotated[str | None, Header(alias=FINCH_KEY_HEADER)] = None,
) -> None:
    expected = _api_key()
    if not expected:
        if _api_test_mode():
            return
        raise HTTPException(status_code=503, detail="Finch API key not configured")
    if not x_finch_key:
        raise HTTPException(status_code=401, detail="Missing X-Finch-Key header")
    if x_finch_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _trip_item_to_dict(item: TripItemRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "trip_id": item.trip_id,
        "normalized_name": item.normalized_name,
        "display_name": item.display_name,
        "product_id": item.product_id,
        "upc": item.upc,
        "quantity": item.quantity,
        "requested_text": item.requested_text,
        "source": item.source,
        "added_at": item.added_at,
        "undone": item.undone,
    }


class PreviewRequest(BaseModel):
    text: str = Field(..., min_length=1)


class CartAddRequest(BaseModel):
    item: str = Field(..., min_length=1)
    quantity: int | None = Field(default=None, ge=1)
    force: bool = False
    source: str | None = None
    chat_key: str | None = None


class CartAddListRequest(BaseModel):
    text: str = Field(..., min_length=1)
    source: str | None = None
    chat_key: str | None = None


class CartChooseRequest(BaseModel):
    chat_key: str = Field(..., min_length=1)
    selection: int = Field(..., ge=1)
    prefer: bool = False
    force: bool = False
    source: str | None = None


class PendingSearchRequest(BaseModel):
    chat_key: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1)


class PendingCancelRequest(BaseModel):
    chat_key: str = Field(..., min_length=1)


class PreferenceChangeRequest(BaseModel):
    item: str = Field(..., min_length=1)
    chat_key: str = Field(..., min_length=1)
    source: str | None = None


class PreferenceAliasRequest(BaseModel):
    from_key: str = Field(..., min_length=1)
    to_key: str = Field(..., min_length=1)


def _needs_choice_response(outcome: NeedsChoiceOutcome) -> dict[str, Any]:
    return outcome.to_dict()


def _execute_single_cart_add(
    attempt: CartAttempt,
    client: Any,
    *,
    action: str = "cart_add",
    force: bool = False,
    source: str | None = None,
) -> dict[str, Any]:
    duplicate_msg = check_trip_duplicate(attempt, force=force)
    if duplicate_msg:
        record_cart_activity(
            attempt,
            action=action,
            result="skipped — duplicate this trip",
        )
        return {
            "ok": False,
            "duplicate": True,
            "message": duplicate_msg,
            "attempt": _attempt_to_dict(attempt),
        }

    try:
        result = execute_cart_add(attempt, client, action=action)
    except (CartGuardError, KrogerAuthError, KrogerError) as exc:
        return {
            "ok": False,
            "duplicate": False,
            "message": str(exc),
            "attempt": _attempt_to_dict(attempt),
        }

    record_successful_trip_add(attempt, source=source)
    status = result.get("status", "ok") if isinstance(result, dict) else "ok"
    return {
        "ok": True,
        "duplicate": False,
        "attempt": _attempt_to_dict(attempt),
        "result": f"ok ({status})",
        "live_cart": live_cart_enabled(),
    }


def create_app() -> FastAPI:
    application = FastAPI(
        title="Finch Local API",
        version=__version__,
        lifespan=_lifespan,
    )

    @application.get("/finch/health")
    def finch_health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @application.post("/finch/preview", dependencies=[Depends(require_finch_key)])
    def finch_preview(body: PreviewRequest) -> dict[str, Any]:
        lines = build_preview(body.text)
        return {"lines": [line.to_dict() for line in lines]}

    @application.post("/finch/cart/add", dependencies=[Depends(require_finch_key)])
    def finch_cart_add(body: CartAddRequest) -> dict[str, Any]:
        try:
            require_live_cart()
            require_saved_token()
        except CartGuardError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        client = load_kroger_client_from_env()
        try:
            ensure_fresh_user_token(client)
        except (KrogerAuthError, KrogerError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        try:
            item_text, parsed_force = parse_add_item(body.item)
            resolved = prepare_add_or_needs_choice(
                item_text,
                quantity=body.quantity,
                chat_key=body.chat_key,
                client=client,
            )
        except CartResolveError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (KrogerAuthError, KrogerError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        if isinstance(resolved, NeedsChoiceOutcome):
            return _needs_choice_response(resolved)

        outcome = _execute_single_cart_add(
            resolved,
            client,
            force=body.force or parsed_force,
            source=body.source,
        )
        if outcome.get("duplicate"):
            return outcome
        if not outcome.get("ok"):
            detail = outcome.get("message", "cart add failed")
            if "FINCH_LIVE_CART" in detail:
                raise HTTPException(status_code=403, detail=detail) from None
            raise HTTPException(status_code=502, detail=detail) from None
        return outcome

    @application.post("/finch/cart/add-list", dependencies=[Depends(require_finch_key)])
    def finch_cart_add_list(body: CartAddListRequest) -> dict[str, Any]:
        try:
            require_live_cart()
            require_saved_token()
        except CartGuardError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        client = load_kroger_client_from_env()
        try:
            ensure_fresh_user_token(client)
        except (KrogerAuthError, KrogerError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        try:
            progress = process_add_list_with_choice(
                body.text,
                chat_key=body.chat_key,
                client=client,
                source=body.source,
                execute_add_fn=lambda attempt: _execute_single_cart_add(
                    attempt,
                    client,
                    action="cart_add_list",
                    source=body.source,
                ),
            )
        except CartResolveError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (KrogerAuthError, KrogerError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        if progress.needs_choice is not None:
            payload = _needs_choice_response(progress.needs_choice)
            payload["partial_outcomes"] = progress.added_outcomes
            payload["succeeded"] = [
                o["attempt"] for o in progress.added_outcomes if o.get("attempt")
            ]
            return payload

        outcomes = progress.added_outcomes
        succeeded = [o["attempt"] for o in outcomes if o.get("attempt")]
        return {
            "ok": all(o.get("ok") or o.get("duplicate") for o in outcomes) if outcomes else False,
            "succeeded": succeeded,
            "failed": [],
            "outcomes": outcomes,
            "live_cart": live_cart_enabled(),
        }

    @application.post("/finch/cart/choose", dependencies=[Depends(require_finch_key)])
    def finch_cart_choose(body: CartChooseRequest) -> dict[str, Any]:
        try:
            require_live_cart()
            require_saved_token()
        except CartGuardError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        client = load_kroger_client_from_env()
        try:
            ensure_fresh_user_token(client)
        except (KrogerAuthError, KrogerError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        outcome = execute_pending_choice(
            chat_key=body.chat_key,
            selection=body.selection,
            prefer=body.prefer,
            force=body.force,
            source=body.source,
            client=client,
        )
        if outcome.get("duplicate"):
            return outcome
        if not outcome.get("ok"):
            detail = outcome.get("message", "choice failed")
            if "FINCH_LIVE_CART" in detail:
                raise HTTPException(status_code=403, detail=detail) from None
            raise HTTPException(status_code=422, detail=detail) from None
        outcome["live_cart"] = live_cart_enabled()
        return outcome

    @application.post("/finch/cart/pending/search", dependencies=[Depends(require_finch_key)])
    def finch_cart_pending_search(body: PendingSearchRequest) -> dict[str, Any]:
        try:
            require_live_cart()
            require_saved_token()
        except CartGuardError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        client = load_kroger_client_from_env()
        try:
            ensure_fresh_user_token(client)
        except (KrogerAuthError, KrogerError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        outcome = rerun_pending_search(
            chat_key=body.chat_key,
            query=body.query,
            client=client,
        )
        if isinstance(outcome, NeedsChoiceOutcome):
            return _needs_choice_response(outcome)
        if not outcome.get("ok"):
            raise HTTPException(status_code=422, detail=outcome.get("message", "search failed"))
        return outcome

    @application.post("/finch/cart/pending/cancel", dependencies=[Depends(require_finch_key)])
    def finch_cart_pending_cancel(body: PendingCancelRequest) -> dict[str, Any]:
        cleared = clear_pending_selection(body.chat_key)
        return {
            "ok": True,
            "cleared": cleared,
            "message": "Cancelled pending product choice." if cleared else "No pending choice to cancel.",
        }

    @application.get("/finch/cart/pending", dependencies=[Depends(require_finch_key)])
    def finch_cart_pending(chat_key: str) -> dict[str, Any]:
        pending = get_pending_selection(chat_key)
        if not pending:
            return {"ok": True, "pending": None}
        return {"ok": True, "pending": pending.to_dict()}

    @application.get("/finch/preferences", dependencies=[Depends(require_finch_key)])
    def finch_preferences_list() -> dict[str, Any]:
        entries = get_all_aliases()
        pinned = [
            preference_to_dict(entry)
            for entry in entries
            if entry.notes and "Pinned via" in entry.notes
        ]
        pinned.sort(key=lambda item: item["alias_key"])
        return {
            "ok": True,
            "preferences": pinned,
            "text": format_preferences_list(),
        }

    @application.get("/finch/preferences/{item}", dependencies=[Depends(require_finch_key)])
    def finch_preference_get(item: str) -> dict[str, Any]:
        entry = lookup_alias(item)
        if entry is None:
            return {
                "ok": True,
                "found": False,
                "text": get_preference_text(item),
            }
        return {
            "ok": True,
            "found": True,
            "preference": preference_to_dict(entry),
            "text": get_preference_text(item),
        }

    @application.delete("/finch/preferences/{item}", dependencies=[Depends(require_finch_key)])
    def finch_preference_delete(item: str) -> dict[str, Any]:
        from finch.preferences import format_forget_message

        removed = delete_aliases_matching_normalized(item)
        message = format_forget_message(item, removed)
        return {
            "ok": True,
            "removed": [preference_to_dict(entry) for entry in removed],
            "text": message,
        }

    @application.post("/finch/preferences/change", dependencies=[Depends(require_finch_key)])
    def finch_preference_change(body: PreferenceChangeRequest) -> dict[str, Any]:
        try:
            require_live_cart()
            require_saved_token()
        except CartGuardError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        client = load_kroger_client_from_env()
        try:
            ensure_fresh_user_token(client)
        except (KrogerAuthError, KrogerError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        try:
            outcome = prepare_change_preference(
                body.item,
                chat_key=body.chat_key,
                client=client,
            )
        except CartResolveError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (KrogerAuthError, KrogerError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return _needs_choice_response(outcome)

    @application.post("/finch/preferences/alias", dependencies=[Depends(require_finch_key)])
    def finch_preference_alias(body: PreferenceAliasRequest) -> dict[str, Any]:
        message = alias_preference_key(body.from_key, body.to_key)
        return {"ok": True, "text": message}

    @application.get("/finch/cart/history", dependencies=[Depends(require_finch_key)])
    def finch_cart_history(limit: int = 50, scope: str = "trip") -> dict[str, Any]:
        if limit < 1:
            raise HTTPException(status_code=422, detail="limit must be at least 1")
        if scope not in ("trip", "today"):
            raise HTTPException(status_code=422, detail="scope must be 'trip' or 'today'")

        if scope == "today":
            items = list_added_today()[:limit]
            return {
                "title": "Finch added list (today)",
                "scope": scope,
                "trip_id": None,
                "items": [_trip_item_to_dict(item) for item in items],
                "text": format_added_list(items, title="Finch added list (today)"),
            }

        trip_id = get_or_create_open_trip()
        items = list_trip_items(trip_id)[:limit]
        return {
            "title": "Finch added list",
            "scope": scope,
            "trip_id": trip_id,
            "items": [_trip_item_to_dict(item) for item in items],
            "text": format_added_list(items, trip_id=trip_id),
        }

    @application.post("/finch/trip/reset", dependencies=[Depends(require_finch_key)])
    def finch_trip_reset() -> dict[str, Any]:
        trip_id = reset_trip()
        return {
            "ok": True,
            "trip_id": trip_id,
            "message": (
                f"Started new Finch grocery trip (trip {trip_id}). "
                "Duplicate guard cleared for this trip."
            ),
        }

    @application.post("/finch/trip/undo-last", dependencies=[Depends(require_finch_key)])
    def finch_trip_undo_last() -> dict[str, Any]:
        trip_id = get_or_create_open_trip()
        item = undo_last_trip_item(trip_id)
        if not item:
            return {
                "ok": True,
                "undone": False,
                "message": "Nothing to undo on the Finch added list for this trip.",
            }
        label = item.display_name or item.normalized_name
        return {
            "ok": True,
            "undone": True,
            "item": _trip_item_to_dict(item),
            "message": (
                f"Removed {label!r} from the Finch added list (local only). "
                "Your Kroger cart was not changed — review it in the Kroger app."
            ),
        }

    return application


def record_cart_activity_from_text(item_text: str, err: str) -> None:
    from finch.activity import log_activity

    log_activity(
        requested_text=item_text,
        resolved_alias=None,
        upc=None,
        product_id=None,
        quantity=0,
        action="cart_add_list",
        result=f"skipped — {err}",
    )


app = create_app()


def main() -> None:
    """Run Finch local API bound to localhost (127.0.0.1:8091)."""
    import uvicorn

    host = os.getenv("FINCH_API_HOST", "127.0.0.1")
    port = int(os.getenv("FINCH_API_PORT", "8091"))
    uvicorn.run("finch.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
