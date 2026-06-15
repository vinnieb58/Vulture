"""
Tests for Finch local API v0.2 (FastAPI test client).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finch.env_util import reset_env_load_state


@pytest.fixture(autouse=True)
def _finch_api_test_env_isolation(monkeypatch):
    monkeypatch.setenv("FINCH_SKIP_DOTENV", "1")
    reset_env_load_state()
    yield
    reset_env_load_state()


@pytest.fixture
def alias_db(tmp_path: Path) -> Path:
    from finch.aliases import init_db, seed_aliases_from_yaml

    db = tmp_path / "aliases.db"
    yaml_src = Path(__file__).resolve().parent.parent / "finch" / "data" / "default_aliases.yaml"
    init_db(db)
    seed_aliases_from_yaml(yaml_src, db, overwrite=True)
    return db


@pytest.fixture
def api_env(monkeypatch, alias_db: Path, tmp_path: Path):
    monkeypatch.setenv("FINCH_API_TEST_MODE", "1")
    monkeypatch.setenv("FINCH_API_KEY", "test-secret-key")
    monkeypatch.delenv("FINCH_LIVE_CART", raising=False)
    monkeypatch.setenv("FINCH_ALIASES_DB_PATH", str(alias_db))
    monkeypatch.setenv("FINCH_ACTIVITY_DB_PATH", str(tmp_path / "finch_activity.db"))


@pytest.fixture
def api_client(api_env):
    from finch.api import create_app

    app = create_app()
    with TestClient(app) as client:
        yield client


AUTH_HEADERS = {"X-Finch-Key": "test-secret-key"}


class TestFinchApiStartup:
    def test_refuses_start_without_api_key(self, monkeypatch):
        monkeypatch.setenv("FINCH_SKIP_DOTENV", "1")
        monkeypatch.delenv("FINCH_API_TEST_MODE", raising=False)
        monkeypatch.delenv("FINCH_API_KEY", raising=False)
        reset_env_load_state()

        from finch.api import create_app

        app = create_app()
        with pytest.raises(RuntimeError, match="FINCH_API_KEY"):
            with TestClient(app):
                pass


class TestFinchApiHealth:
    def test_health(self, api_client):
        response = api_client.get("/finch/health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["version"] == "0.2.0"


class TestFinchApiAuth:
    def test_preview_rejects_missing_key(self, api_client):
        response = api_client.post("/finch/preview", json={"text": "eggs"})
        assert response.status_code == 401
        assert "Missing X-Finch-Key" in response.json()["detail"]

    def test_preview_rejects_invalid_key(self, api_client):
        response = api_client.post(
            "/finch/preview",
            json={"text": "eggs"},
            headers={"X-Finch-Key": "wrong-key"},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid API key"


class TestFinchApiPreview:
    def test_preview(self, api_client):
        response = api_client.post(
            "/finch/preview",
            json={"text": "eggs, milk"},
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 200
        payload = response.json()
        names = [line["normalized_name"] for line in payload["lines"]]
        assert names == ["eggs", "milk"]
        eggs = payload["lines"][0]
        assert eggs["status"] == "exact_default"
        assert eggs["upc"] == "0001111081708"


class TestFinchApiCart:
    def test_cart_add_blocked_when_live_cart_off(self, api_client):
        response = api_client.post(
            "/finch/cart/add",
            json={"item": "eggs"},
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 403
        assert "FINCH_LIVE_CART" in response.json()["detail"]

    def test_cart_add_list_blocked_when_live_cart_off(self, api_client):
        response = api_client.post(
            "/finch/cart/add-list",
            json={"text": "eggs, milk"},
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 403
        assert "FINCH_LIVE_CART" in response.json()["detail"]

    def test_cart_add_ok_when_live_cart_on(self, api_client, monkeypatch):
        monkeypatch.setenv("FINCH_LIVE_CART", "true")
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.load_kroger_client_from_env") as mock_load:
                mock_client = MagicMock()
                mock_client.add_to_cart.return_value = {"status": "ok"}
                mock_load.return_value = mock_client
                with patch("finch.api.ensure_fresh_user_token"):
                    response = api_client.post(
                        "/finch/cart/add",
                        json={"item": "eggs", "quantity": 2},
                        headers=AUTH_HEADERS,
                    )
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["attempt"]["quantity"] == 2
        assert payload["attempt"]["upc"] == "0001111081708"
        assert payload["live_cart"] is True
        assert "secret" not in response.text.lower()
        assert "user-tok" not in response.text


class TestFinchApiCartCurrent:
    def test_cart_current_not_supported(self, api_client, monkeypatch):
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.load_kroger_client_from_env") as mock_load:
                mock_client = MagicMock()
                from finch.kroger_client import KrogerCartReadNotSupportedError

                mock_client.get_current_cart.side_effect = KrogerCartReadNotSupportedError(
                    "Kroger cart read is not available with Public API access."
                )
                mock_load.return_value = mock_client
                with patch("finch.api.ensure_fresh_user_token"):
                    response = api_client.get("/finch/cart/current", headers=AUTH_HEADERS)
        assert response.status_code == 200
        payload = response.json()
        assert payload["supported"] is False
        assert "Public API" in payload["message"]
        assert payload["items"] == []

    def test_cart_current_ok(self, api_client, monkeypatch):
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.load_kroger_client_from_env") as mock_load:
                from finch.kroger_client import KrogerCartLineItem, KrogerCartSnapshot

                mock_client = MagicMock()
                mock_client.get_current_cart.return_value = KrogerCartSnapshot(
                    items=[
                        KrogerCartLineItem(
                            name="Kroger Large Eggs",
                            quantity=2,
                            price="$2.99",
                            line_total="$5.98",
                        )
                    ],
                    subtotal="$5.98",
                )
                mock_load.return_value = mock_client
                with patch("finch.api.ensure_fresh_user_token"):
                    response = api_client.get("/finch/cart/current", headers=AUTH_HEADERS)
        assert response.status_code == 200
        payload = response.json()
        assert payload["supported"] is True
        assert len(payload["items"]) == 1
        assert payload["items"][0]["name"] == "Kroger Large Eggs"
        assert payload["subtotal"] == "$5.98"
        assert "user-tok" not in response.text

    def test_cart_current_requires_token(self, api_client):
        with patch("finch.cart_ops.resolve_user_access_token", return_value=None):
            response = api_client.get("/finch/cart/current", headers=AUTH_HEADERS)
        assert response.status_code == 403
        assert "token" in response.json()["detail"].lower()


class TestFinchApiHistory:
    def test_history(self, api_client, alias_db: Path, tmp_path: Path):
        from finch.activity import list_cart_activity
        from finch.cart_ops import record_cart_activity, resolve_cart_item

        activity_db = tmp_path / "history_only.db"
        attempt = resolve_cart_item("eggs", db_path=alias_db)
        record_cart_activity(
            attempt,
            action="cart_add",
            result="ok (ok)",
            activity_db_path=activity_db,
        )

        with patch(
            "finch.api.list_cart_activity",
            lambda **kwargs: list_cart_activity(
                limit=kwargs.get("limit", 50),
                db_path=activity_db,
            ),
        ):
            response = api_client.get("/finch/cart/history", headers=AUTH_HEADERS)

        assert response.status_code == 200
        entries = response.json()["entries"]
        assert len(entries) == 1
        assert entries[0]["requested_text"] == "eggs"
        assert entries[0]["action"] == "cart_add"
        assert entries[0]["upc"] == "0001111081708"

    def test_history_empty(self, api_client, tmp_path: Path):
        with patch("finch.api.list_cart_activity", return_value=[]):
            response = api_client.get("/finch/cart/history", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.json()["entries"] == []
