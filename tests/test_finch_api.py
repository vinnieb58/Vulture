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
def isolated_finch_env(tmp_path: Path, monkeypatch):
    """Use temp data paths and deterministic aliases; never Raven's finch_aliases.db."""
    import finch.aliases as aliases_mod
    import finch.config as config_mod
    import finch.activity as activity_mod
    from finch.aliases import init_db, upsert_alias
    from finch.models import AliasEntry

    data_dir = tmp_path / "finch_data"
    data_dir.mkdir()
    aliases_db = data_dir / "finch_aliases.db"
    activity_db = data_dir / "finch_activity.db"

    monkeypatch.setenv("FINCH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("FINCH_ALIASES_DB_PATH", str(aliases_db))
    monkeypatch.setenv("FINCH_ACTIVITY_DB_PATH", str(activity_db))

    monkeypatch.setattr(config_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(config_mod, "ALIASES_DB_PATH", aliases_db)
    monkeypatch.setattr(activity_mod, "ACTIVITY_DB_PATH", activity_db)
    monkeypatch.setattr(aliases_mod, "ALIASES_DB_PATH", aliases_db)

    eggs = AliasEntry(
        alias_key="eggs",
        display_name="Test Eggs 12 ct",
        upc="0000000000001",
        search_term="test eggs",
    )
    milk = AliasEntry(
        alias_key="milk",
        display_name="Test Milk 1 gal",
        upc="0000000000002",
        search_term="test milk",
    )

    init_db(aliases_db)
    upsert_alias(eggs, aliases_db)
    upsert_alias(milk, aliases_db)

    return {
        "data_dir": data_dir,
        "aliases_db": aliases_db,
        "activity_db": activity_db,
        "eggs_upc": eggs.upc,
        "milk_upc": milk.upc,
    }


@pytest.fixture
def api_env(monkeypatch, isolated_finch_env):
    monkeypatch.setenv("FINCH_API_TEST_MODE", "1")
    monkeypatch.setenv("FINCH_API_KEY", "test-secret-key")
    monkeypatch.delenv("FINCH_LIVE_CART", raising=False)


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
    def test_preview(self, api_client, isolated_finch_env):
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
        assert eggs["upc"] == isolated_finch_env["eggs_upc"]


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

    def test_cart_add_ok_when_live_cart_on(self, api_client, isolated_finch_env, monkeypatch):
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
        assert payload["attempt"]["upc"] == isolated_finch_env["eggs_upc"]
        assert payload["live_cart"] is True
        assert "secret" not in response.text.lower()
        assert "user-tok" not in response.text


class TestFinchApiHistory:
    def test_history(self, api_client, isolated_finch_env):
        from finch.activity import list_cart_activity
        from finch.cart_ops import record_cart_activity, resolve_cart_item

        aliases_db = isolated_finch_env["aliases_db"]
        activity_db = isolated_finch_env["activity_db"]
        attempt = resolve_cart_item("eggs", db_path=aliases_db)
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
        assert entries[0]["upc"] == isolated_finch_env["eggs_upc"]

    def test_history_empty(self, api_client):
        with patch("finch.api.list_cart_activity", return_value=[]):
            response = api_client.get("/finch/cart/history", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.json()["entries"] == []
