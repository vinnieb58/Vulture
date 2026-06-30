"""HTTP client for the local Finch API."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from finch.pending_selection import make_chat_key
from finch_telegram import config

FINCH_KEY_HEADER = "X-Finch-Key"


class FinchApiError(Exception):
    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        method: str | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.method = method
        self.path = path


def telegram_chat_key(chat_id: str) -> str:
    return make_chat_key("telegram", chat_id)


def _headers() -> dict[str, str]:
    return {FINCH_KEY_HEADER: config.finch_api_key()}


def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    url = f"{config.finch_api_base_url()}{path}"
    with httpx.Client(timeout=30.0) as client:
        response = client.request(method, url, headers=_headers(), **kwargs)
    if response.status_code >= 400:
        detail = response.text
        try:
            payload = response.json()
            if isinstance(payload, dict) and payload.get("detail"):
                detail = str(payload["detail"])
        except ValueError:
            pass
        raise FinchApiError(
            response.status_code,
            detail,
            method=method,
            path=path,
        )
    return response.json()


def preview(text: str) -> dict[str, Any]:
    return _request("POST", "/finch/preview", json={"text": text})


def cart_add(item: str, *, source: str | None = None, chat_key: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"item": item}
    if source:
        body["source"] = source
    if chat_key:
        body["chat_key"] = chat_key
    return _request("POST", "/finch/cart/add", json=body)


def cart_add_list(
    text: str,
    *,
    source: str | None = None,
    chat_key: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"text": text}
    if source:
        body["source"] = source
    if chat_key:
        body["chat_key"] = chat_key
    return _request("POST", "/finch/cart/add-list", json=body)


def cart_choose(
    chat_key: str,
    selection: int,
    *,
    prefer: bool = False,
    force: bool = False,
    source: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "chat_key": chat_key,
        "selection": selection,
        "prefer": prefer,
        "force": force,
    }
    if source:
        body["source"] = source
    return _request("POST", "/finch/cart/choose", json=body)


def pending_cancel(chat_key: str) -> dict[str, Any]:
    return _request("POST", "/finch/cart/pending/cancel", json={"chat_key": chat_key})


def pending_search(chat_key: str, query: str) -> dict[str, Any]:
    return _request(
        "POST",
        "/finch/cart/pending/search",
        json={"chat_key": chat_key, "query": query},
    )


def pending_more(chat_key: str) -> dict[str, Any]:
    return _request("POST", "/finch/cart/pending/more", json={"chat_key": chat_key})


def pending_back(chat_key: str) -> dict[str, Any]:
    return _request("POST", "/finch/cart/pending/back", json={"chat_key": chat_key})


def cart_history(limit: int = 10, *, scope: str = "trip") -> dict[str, Any]:
    return _request("GET", "/finch/cart/history", params={"limit": limit, "scope": scope})


def trip_reset() -> dict[str, Any]:
    return _request("POST", "/finch/trip/reset")


def trip_undo_last() -> dict[str, Any]:
    return _request("POST", "/finch/trip/undo-last")


def preferences_list() -> dict[str, Any]:
    return _request("GET", "/finch/preferences")


def preference_get(item: str) -> dict[str, Any]:
    encoded = quote(item, safe="")
    return _request("GET", f"/finch/preferences/{encoded}")


def preference_delete(item: str) -> dict[str, Any]:
    encoded = quote(item, safe="")
    return _request("DELETE", f"/finch/preferences/{encoded}")


def preference_change(item: str, *, chat_key: str, source: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"item": item, "chat_key": chat_key}
    if source:
        body["source"] = source
    return _request("POST", "/finch/preferences/change", json=body)


def preference_alias(new_key: str, existing_key: str) -> dict[str, Any]:
    return _request(
        "POST",
        "/finch/preferences/alias",
        json={"from_key": new_key, "to_key": existing_key},
    )


def staples_list() -> dict[str, Any]:
    return _request("GET", "/finch/staples")


def staples_pending(chat_key: str) -> dict[str, Any]:
    return _request("GET", "/finch/staples/pending", params={"chat_key": chat_key})


def staples_start(chat_key: str) -> dict[str, Any]:
    return _request("POST", "/finch/staples/start", json={"chat_key": chat_key})


def staples_remove(chat_key: str, targets: str) -> dict[str, Any]:
    return _request(
        "POST",
        "/finch/staples/remove",
        json={"chat_key": chat_key, "targets": targets},
    )


def staples_confirm(chat_key: str, *, source: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"chat_key": chat_key}
    if source:
        body["source"] = source
    return _request("POST", "/finch/staples/confirm", json=body)


def staples_cancel(chat_key: str) -> dict[str, Any]:
    return _request("POST", "/finch/staples/cancel", json={"chat_key": chat_key})


def cart_pending(chat_key: str) -> dict[str, Any]:
    return _request("GET", "/finch/cart/pending", params={"chat_key": chat_key})
