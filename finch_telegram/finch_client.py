"""HTTP client for the local Finch API."""

from __future__ import annotations

from typing import Any

import httpx

from finch_telegram import config

FINCH_KEY_HEADER = "X-Finch-Key"


class FinchApiError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


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
        raise FinchApiError(response.status_code, detail)
    return response.json()


def preview(text: str) -> dict[str, Any]:
    return _request("POST", "/finch/preview", json={"text": text})


def cart_add(item: str, *, source: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"item": item}
    if source:
        body["source"] = source
    return _request("POST", "/finch/cart/add", json=body)


def cart_add_list(text: str, *, source: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"text": text}
    if source:
        body["source"] = source
    return _request("POST", "/finch/cart/add-list", json=body)


def cart_history(limit: int = 10, *, scope: str = "trip") -> dict[str, Any]:
    return _request("GET", "/finch/cart/history", params={"limit": limit, "scope": scope})


def trip_reset() -> dict[str, Any]:
    return _request("POST", "/finch/trip/reset")


def trip_undo_last() -> dict[str, Any]:
    return _request("POST", "/finch/trip/undo-last")
