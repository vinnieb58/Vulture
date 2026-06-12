"""
Kroger API client skeleton.

OAuth:
  - Client credentials: product search (product.compact scope)
  - Authorization code: cart add (cart.basic:write scope) — requires browser flow

Security:
  - Never log tokens, refresh tokens, customer IDs, or full Authorization headers.
  - Secrets load from repo-root .env via python-dotenv when available.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from finch.config import (
    FINCH_LIVE_CART,
    KROGER_BASE_URL,
    KROGER_CART_MODALITY,
    KROGER_LOCATION_ID,
)

logger = logging.getLogger(__name__)

OAUTH_TOKEN_PATH = "/v1/connect/oauth2/token"
OAUTH_AUTHORIZE_PATH = "/v1/connect/oauth2/authorize"
PRODUCTS_PATH = "/v1/products"
CART_ADD_PATH = "/v1/cart/add"


class KrogerError(Exception):
    """Base Kroger API error."""


class KrogerAuthError(KrogerError):
    """Authentication or authorization failure."""


class KrogerCartDisabledError(KrogerError):
    """Cart mutation blocked by Finch guardrails."""


@dataclass(frozen=True)
class KrogerProduct:
    product_id: str
    upc: str | None
    description: str
    brand: str | None = None
    size: str | None = None
    price: str | None = None

    def format_price(self) -> str | None:
        if self.price is None:
            return None
        try:
            value = float(self.price)
            return f"${value:.2f}"
        except ValueError:
            return self.price


@dataclass(frozen=True)
class KrogerOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str | None = None
    location_id: str | None = None

    @classmethod
    def from_env(cls) -> KrogerOAuthConfig:
        client_id = os.getenv("FINCH_KROGER_CLIENT_ID", "").strip()
        client_secret = os.getenv("FINCH_KROGER_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise KrogerAuthError(
                "FINCH_KROGER_CLIENT_ID and FINCH_KROGER_CLIENT_SECRET must be set in .env"
            )
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=os.getenv("FINCH_KROGER_REDIRECT_URI", "").strip() or None,
            location_id=os.getenv("FINCH_KROGER_LOCATION_ID", KROGER_LOCATION_ID).strip() or None,
        )


class HttpSession(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _safe_log_response(resp: requests.Response) -> str:
    """Log HTTP status without leaking auth headers or token bodies."""
    return f"HTTP {resp.status_code} from {resp.url.split('?')[0]}"


def _parse_products_payload(data: dict[str, Any]) -> list[KrogerProduct]:
    products: list[KrogerProduct] = []
    for item in data.get("data", []):
        product_id = str(item.get("productId", ""))
        if not product_id:
            continue
        price = None
        size = None
        upc = item.get("upc")
        items = item.get("items") or []
        if items and isinstance(items[0], dict):
            first_item = items[0]
            price = first_item.get("price", {}).get("regular")
            size = first_item.get("size")
            upc = upc or first_item.get("upc")
        products.append(
            KrogerProduct(
                product_id=product_id,
                upc=str(upc) if upc else None,
                description=str(item.get("description", "")),
                brand=item.get("brand"),
                size=str(size) if size else None,
                price=str(price) if price is not None else None,
            )
        )
    return products


class KrogerClient:
    """Thin Kroger API wrapper with dry-run guardrails for cart mutation."""

    def __init__(
        self,
        oauth: KrogerOAuthConfig,
        *,
        session: HttpSession | None = None,
        base_url: str = KROGER_BASE_URL,
        user_access_token: str | None = None,
    ) -> None:
        self.oauth = oauth
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self._client_token: str | None = None
        self._user_access_token = user_access_token or os.getenv(
            "FINCH_KROGER_USER_ACCESS_TOKEN", ""
        ).strip() or None

    def build_authorize_url(
        self,
        *,
        state: str,
        scopes: str = "product.compact cart.basic:write profile.compact",
    ) -> str:
        if not self.oauth.redirect_uri:
            raise KrogerAuthError("FINCH_KROGER_REDIRECT_URI is required for authorization code flow")
        from urllib.parse import urlencode

        params = {
            "client_id": self.oauth.client_id,
            "redirect_uri": self.oauth.redirect_uri,
            "response_type": "code",
            "scope": scopes,
            "state": state,
        }
        return f"{self.base_url}{OAUTH_AUTHORIZE_PATH}?{urlencode(params)}"

    def fetch_client_credentials_token(self, scope: str = "product.compact") -> str:
        url = f"{self.base_url}{OAUTH_TOKEN_PATH}"
        resp = self.session.request(
            "POST",
            url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": _basic_auth_header(self.oauth.client_id, self.oauth.client_secret),
            },
            data={"grant_type": "client_credentials", "scope": scope},
            timeout=30,
        )
        if resp.status_code >= 400:
            logger.warning("%s (client credentials)", _safe_log_response(resp))
            raise KrogerAuthError(f"Token request failed: HTTP {resp.status_code}")
        token = resp.json().get("access_token")
        if not token:
            raise KrogerAuthError("Token response missing access_token")
        self._client_token = str(token)
        return self._client_token

    def exchange_authorization_code(self, code: str) -> str:
        if not self.oauth.redirect_uri:
            raise KrogerAuthError("FINCH_KROGER_REDIRECT_URI is required for authorization code flow")
        url = f"{self.base_url}{OAUTH_TOKEN_PATH}"
        resp = self.session.request(
            "POST",
            url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": _basic_auth_header(self.oauth.client_id, self.oauth.client_secret),
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.oauth.redirect_uri,
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            logger.warning("%s (authorization code)", _safe_log_response(resp))
            raise KrogerAuthError(f"Code exchange failed: HTTP {resp.status_code}")
        token = resp.json().get("access_token")
        if not token:
            raise KrogerAuthError("Token response missing access_token")
        self._user_access_token = str(token)
        return self._user_access_token

    def _product_token(self) -> str:
        if self._client_token:
            return self._client_token
        return self.fetch_client_credentials_token()

    def search_products(
        self,
        term: str,
        *,
        location_id: str | None = None,
        limit: int = 10,
    ) -> list[KrogerProduct]:
        loc = location_id or self.oauth.location_id
        params: dict[str, str | int] = {
            "filter.term": term,
            "filter.limit": limit,
        }
        if loc:
            params["filter.locationId"] = loc

        url = f"{self.base_url}{PRODUCTS_PATH}"
        resp = self.session.request(
            "GET",
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._product_token()}",
            },
            params=params,
            timeout=30,
        )
        if resp.status_code >= 400:
            logger.warning("%s (product search)", _safe_log_response(resp))
            raise KrogerError(f"Product search failed: HTTP {resp.status_code}")
        return _parse_products_payload(resp.json())

    def add_to_cart(
        self,
        upc: str,
        quantity: int = 1,
        *,
        modality: str | None = None,
        live: bool | None = None,
    ) -> dict[str, Any]:
        """
        Add an item to the authenticated user's Kroger cart.

        Guarded by FINCH_LIVE_CART unless live=True is passed explicitly.
        Does not perform checkout or payment.
        """
        allow_live = FINCH_LIVE_CART if live is None else live
        if not allow_live:
            raise KrogerCartDisabledError(
                "Cart add is disabled. Set FINCH_LIVE_CART=true after reviewing dry-run output."
            )
        if not self._user_access_token:
            raise KrogerAuthError(
                "User access token required for cart add. Complete OAuth authorization code flow."
            )

        url = f"{self.base_url}{CART_ADD_PATH}"
        body = {
            "items": [
                {
                    "quantity": quantity,
                    "upc": upc,
                    "modality": modality or KROGER_CART_MODALITY,
                }
            ]
        }
        resp = self.session.request(
            "PUT",
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._user_access_token}",
            },
            json=body,
            timeout=30,
        )
        if resp.status_code >= 400:
            logger.warning("%s (cart add)", _safe_log_response(resp))
            raise KrogerError(f"Cart add failed: HTTP {resp.status_code}")
        return resp.json() if resp.content else {"status": "ok"}


def load_kroger_client_from_env(
    *,
    session: HttpSession | None = None,
    user_access_token: str | None = None,
) -> KrogerClient:
    """Load dotenv from repo root when available, then build a KrogerClient."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    oauth = KrogerOAuthConfig.from_env()
    return KrogerClient(oauth, session=session, user_access_token=user_access_token)
