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


@pytest.fixture(autouse=True)
def _reset_trip_ledger_between_api_tests(tmp_path: Path, monkeypatch):
    ledger_db = tmp_path / "finch_trip_ledger.db"
    monkeypatch.setenv("FINCH_TRIP_LEDGER_DB_PATH", str(ledger_db))
    if ledger_db.exists():
        ledger_db.unlink()
    from finch.trip_ledger import reset_trip

    reset_trip(db_path=ledger_db)
    yield


@pytest.fixture
def api_env(monkeypatch, alias_db: Path, tmp_path: Path):
    monkeypatch.setenv("FINCH_API_TEST_MODE", "1")
    monkeypatch.setenv("FINCH_API_KEY", "test-secret-key")
    monkeypatch.delenv("FINCH_LIVE_CART", raising=False)
    monkeypatch.setenv("FINCH_ALIASES_DB_PATH", str(alias_db))
    monkeypatch.setenv("FINCH_ACTIVITY_DB_PATH", str(tmp_path / "finch_activity.db"))
    monkeypatch.setenv("FINCH_TRIP_LEDGER_DB_PATH", str(tmp_path / "finch_trip_ledger.db"))


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


class TestFinchApiHistory:
    def test_history(self, api_client, alias_db: Path, tmp_path: Path):
        from finch.trip_ledger import record_trip_add, reset_trip

        ledger_db = tmp_path / "finch_trip_ledger.db"
        trip_id = reset_trip(db_path=ledger_db)
        record_trip_add(
            trip_id=trip_id,
            normalized_name="eggs",
            display_name="Kroger Grade A Large Eggs 12 ct",
            product_id=None,
            upc="0001111081708",
            quantity=1,
            requested_text="eggs",
            source="api",
            db_path=ledger_db,
        )

        response = api_client.get("/finch/cart/history", headers=AUTH_HEADERS)

        assert response.status_code == 200
        payload = response.json()
        assert payload["title"] == "Finch added list"
        assert len(payload["items"]) == 1
        assert payload["items"][0]["normalized_name"] == "eggs"
        assert "Finch added list" in payload["text"]

    def test_history_empty(self, api_client):
        response = api_client.get("/finch/cart/history", headers=AUTH_HEADERS)
        assert response.status_code == 200
        payload = response.json()
        assert payload["items"] == []
        assert "empty" in payload["text"].lower()


class TestFinchApiTripLedger:
    def test_duplicate_add_returns_message(self, api_client, monkeypatch):
        monkeypatch.setenv("FINCH_LIVE_CART", "true")
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.load_kroger_client_from_env") as mock_load:
                mock_client = MagicMock()
                mock_client.add_to_cart.return_value = {"status": "ok"}
                mock_load.return_value = mock_client
                with patch("finch.api.ensure_fresh_user_token"):
                    first = api_client.post(
                        "/finch/cart/add",
                        json={"item": "eggs"},
                        headers=AUTH_HEADERS,
                    )
                    second = api_client.post(
                        "/finch/cart/add",
                        json={"item": "eggs"},
                        headers=AUTH_HEADERS,
                    )
        assert first.status_code == 200
        assert first.json()["ok"] is True
        assert second.status_code == 200
        assert second.json()["duplicate"] is True
        assert "already added eggs this trip" in second.json()["message"]
        assert mock_client.add_to_cart.call_count == 1

    def test_reset_trip(self, api_client, monkeypatch):
        monkeypatch.setenv("FINCH_LIVE_CART", "true")
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.load_kroger_client_from_env") as mock_load:
                mock_client = MagicMock()
                mock_client.add_to_cart.return_value = {"status": "ok"}
                mock_load.return_value = mock_client
                with patch("finch.api.ensure_fresh_user_token"):
                    api_client.post(
                        "/finch/cart/add",
                        json={"item": "eggs"},
                        headers=AUTH_HEADERS,
                    )
                    reset = api_client.post("/finch/trip/reset", headers=AUTH_HEADERS)
                    again = api_client.post(
                        "/finch/cart/add",
                        json={"item": "eggs"},
                        headers=AUTH_HEADERS,
                    )
        assert reset.status_code == 200
        assert again.status_code == 200
        assert again.json()["ok"] is True
        assert mock_client.add_to_cart.call_count == 2
