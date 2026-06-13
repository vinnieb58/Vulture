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
from finch.activity import ActivityRecord, list_cart_activity
from finch.cart_ops import (
    CartAttempt,
    CartGuardError,
    CartResolveError,
    ensure_fresh_user_token,
    execute_cart_add,
    live_cart_enabled,
    record_cart_activity,
    require_live_cart,
    require_saved_token,
    resolve_cart_item,
    resolve_cart_list,
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


def _activity_to_dict(record: ActivityRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "timestamp": record.timestamp,
        "requested_text": record.requested_text,
        "resolved_alias": record.resolved_alias,
        "upc": record.upc,
        "product_id": record.product_id,
        "quantity": record.quantity,
        "action": record.action,
        "result": record.result,
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


class PreviewRequest(BaseModel):
    text: str = Field(..., min_length=1)


class CartAddRequest(BaseModel):
    item: str = Field(..., min_length=1)
    quantity: int | None = Field(default=None, ge=1)


class CartAddListRequest(BaseModel):
    text: str = Field(..., min_length=1)


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

        try:
            attempt = resolve_cart_item(body.item, quantity=body.quantity)
        except CartResolveError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        client = load_kroger_client_from_env()
        try:
            ensure_fresh_user_token(client)
        except (KrogerAuthError, KrogerError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        try:
            result = execute_cart_add(attempt, client)
        except CartGuardError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (KrogerAuthError, KrogerError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        status = result.get("status", "ok") if isinstance(result, dict) else "ok"
        return {
            "ok": True,
            "attempt": _attempt_to_dict(attempt),
            "result": f"ok ({status})",
            "live_cart": live_cart_enabled(),
        }

    @application.post("/finch/cart/add-list", dependencies=[Depends(require_finch_key)])
    def finch_cart_add_list(body: CartAddListRequest) -> dict[str, Any]:
        try:
            require_live_cart()
            require_saved_token()
        except CartGuardError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        try:
            parsed = resolve_cart_list(body.text)
        except CartResolveError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        for item_text, err in parsed.failed:
            record_cart_activity_from_text(item_text, err)

        if not parsed.succeeded:
            raise HTTPException(
                status_code=422,
                detail="No items could be resolved for cart add.",
            )

        client = load_kroger_client_from_env()
        try:
            ensure_fresh_user_token(client)
        except (KrogerAuthError, KrogerError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        outcomes: list[dict[str, Any]] = []
        ok = True
        for attempt in parsed.succeeded:
            item_result: dict[str, Any] = {"attempt": _attempt_to_dict(attempt)}
            try:
                result = execute_cart_add(attempt, client, action="cart_add_list")
                status = result.get("status", "ok") if isinstance(result, dict) else "ok"
                item_result["ok"] = True
                item_result["result"] = f"ok ({status})"
            except (CartGuardError, KrogerAuthError, KrogerError) as exc:
                item_result["ok"] = False
                item_result["result"] = f"failed — {exc}"
                ok = False
            outcomes.append(item_result)

        return {
            "ok": ok,
            "succeeded": [_attempt_to_dict(a) for a in parsed.succeeded],
            "failed": [{"item": item, "error": err} for item, err in parsed.failed],
            "outcomes": outcomes,
            "live_cart": live_cart_enabled(),
        }

    @application.get("/finch/cart/history", dependencies=[Depends(require_finch_key)])
    def finch_cart_history(limit: int = 50) -> dict[str, Any]:
        if limit < 1:
            raise HTTPException(status_code=422, detail="limit must be at least 1")
        records = list_cart_activity(limit=limit)
        return {"entries": [_activity_to_dict(r) for r in records]}

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
