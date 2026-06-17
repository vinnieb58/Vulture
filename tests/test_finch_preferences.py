"""Tests for Finch preference management commands and API."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finch.env_util import reset_env_load_state
from finch.pending_selection import (
    PendingSearchResult,
    get_pending_selection,
    make_chat_key,
)


@pytest.fixture(autouse=True)
def _preference_env_isolation(monkeypatch):
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
def pending_db(tmp_path: Path) -> Path:
    return tmp_path / "finch_pending_selection.db"


@pytest.fixture
def ledger_db(tmp_path: Path) -> Path:
    db = tmp_path / "finch_trip_ledger.db"
    from finch.trip_ledger import reset_trip

    reset_trip(db_path=db)
    return db


@pytest.fixture
def api_env(monkeypatch, alias_db: Path, pending_db: Path, ledger_db: Path, tmp_path: Path):
    monkeypatch.setenv("FINCH_API_TEST_MODE", "1")
    monkeypatch.setenv("FINCH_API_KEY", "test-secret-key")
    monkeypatch.setenv("FINCH_LIVE_CART", "true")
    monkeypatch.setenv("FINCH_ALIASES_DB_PATH", str(alias_db))
    monkeypatch.setenv("FINCH_PENDING_SELECTION_DB_PATH", str(pending_db))
    monkeypatch.setenv("FINCH_TRIP_LEDGER_DB_PATH", str(ledger_db))
    monkeypatch.setenv("FINCH_ACTIVITY_DB_PATH", str(tmp_path / "finch_activity.db"))
    monkeypatch.setenv("FINCH_SEARCH_RESULT_LIMIT", "5")


@pytest.fixture
def api_client(api_env):
    from finch.api import create_app

    app = create_app()
    with TestClient(app) as client:
        yield client


AUTH_HEADERS = {"X-Finch-Key": "test-secret-key"}
CHAT_KEY = make_chat_key("telegram", "111222333")

BAGEL_RESULTS = [
    PendingSearchResult(
        product_id="bagel-1",
        upc="0001111000001",
        description="Thomas Plain Bagels",
        size="6 ct",
        price="$3.99",
    ),
    PendingSearchResult(
        product_id="bagel-2",
        upc="0001111000002",
        description="Everything Bagels 6 ct",
        size="6 ct",
        price="$4.29",
    ),
]


def _mock_kroger_client():
    mock_client = MagicMock()
    mock_client.add_to_cart.return_value = {"status": "ok"}
    return mock_client


def _patch_search(results: list[PendingSearchResult] | None = None):
    items = results if results is not None else BAGEL_RESULTS

    def fake_search(query: str, *, client=None, limit: int = 5):
        from finch.kroger_client import KrogerProduct

        return [
            KrogerProduct(
                product_id=item.product_id,
                upc=item.upc,
                description=item.description,
                size=item.size,
                price=item.price.replace("$", "") if item.price else None,
            )
            for item in items
        ]

    return patch("finch.cart_choice.run_search", side_effect=fake_search)


def _save_bagel_preference(api_client, *, selection: int = 1) -> None:
    with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
        with patch("finch.api.load_kroger_client_from_env", return_value=_mock_kroger_client()):
            with patch("finch.api.ensure_fresh_user_token"):
                with _patch_search():
                    api_client.post(
                        "/finch/cart/add",
                        json={"item": "bagel", "chat_key": CHAT_KEY},
                        headers=AUTH_HEADERS,
                    )
                    api_client.post(
                        "/finch/cart/choose",
                        json={
                            "chat_key": CHAT_KEY,
                            "selection": selection,
                            "prefer": True,
                        },
                        headers=AUTH_HEADERS,
                    )


class TestPreferenceNormalizationApi:
    def test_prefer_bagel_lookup_bagels(self, api_client, alias_db: Path):
        _save_bagel_preference(api_client)
        from finch.aliases import get_alias

        saved = get_alias("bagels", alias_db)
        assert saved is not None
        assert saved.alias_key == "bagel"
        assert saved.upc == "0001111000001"

    def test_add_bagels_uses_bagel_preference(self, api_client, alias_db: Path):
        _save_bagel_preference(api_client)
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            mock_client = _mock_kroger_client()
            with patch("finch.api.load_kroger_client_from_env", return_value=mock_client):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search() as mock_search:
                        response = api_client.post(
                            "/finch/cart/add",
                            json={"item": "bagels", "chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
        assert response.status_code == 200
        assert response.json().get("needs_choice") is not True
        mock_search.assert_not_called()


class TestPreferenceManagementApi:
    def test_prefs_list_formatting(self, api_client):
        _save_bagel_preference(api_client)
        response = api_client.get("/finch/preferences", headers=AUTH_HEADERS)
        assert response.status_code == 200
        payload = response.json()
        assert "bagel -> Thomas Plain Bagels" in payload["text"]

    def test_pref_item_hit_and_miss(self, api_client):
        _save_bagel_preference(api_client)
        hit = api_client.get("/finch/preferences/bagels", headers=AUTH_HEADERS)
        assert hit.status_code == 200
        assert hit.json()["found"] is True
        assert "Thomas Plain Bagels" in hit.json()["text"]

        miss = api_client.get("/finch/preferences/unknown-item", headers=AUTH_HEADERS)
        assert miss.status_code == 200
        assert miss.json()["found"] is False
        assert "add unknown-item" in miss.json()["text"].lower()

    def test_forget_removes_normalized_key(self, api_client, alias_db: Path):
        _save_bagel_preference(api_client)
        from finch.aliases import get_alias

        assert get_alias("bagel", alias_db) is not None
        response = api_client.delete("/finch/preferences/bagels", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert "Removed preference" in response.json()["text"]
        assert get_alias("bagel", alias_db) is None

    def test_change_creates_pending_despite_existing_preference(self, api_client):
        _save_bagel_preference(api_client)
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.load_kroger_client_from_env", return_value=_mock_kroger_client()):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search():
                        response = api_client.post(
                            "/finch/preferences/change",
                            json={"item": "bagels", "chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["needs_choice"] is True
        pending = get_pending_selection(CHAT_KEY)
        assert pending is not None
        assert pending.normalized_name == "bagel"

    def test_change_prefer_replaces_preference(self, api_client, alias_db: Path):
        _save_bagel_preference(api_client)
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.load_kroger_client_from_env", return_value=_mock_kroger_client()):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search():
                        api_client.post(
                            "/finch/preferences/change",
                            json={"item": "bagels", "chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
                        choose = api_client.post(
                            "/finch/cart/choose",
                            json={
                                "chat_key": CHAT_KEY,
                                "selection": 2,
                                "prefer": True,
                            },
                            headers=AUTH_HEADERS,
                        )
        assert choose.status_code == 200
        from finch.aliases import get_alias

        saved = get_alias("bagel", alias_db)
        assert saved is not None
        assert saved.upc == "0001111000002"


class TestTelegramPreferenceCommands:
    def test_parse_preference_commands(self):
        from finch_telegram.commands import (
            AliasPrefCommand,
            ChangePrefCommand,
            ForgetPrefCommand,
            PrefCommand,
            PrefsCommand,
            parse_command,
        )

        assert isinstance(parse_command("prefs"), PrefsCommand)
        assert isinstance(parse_command("preferences"), PrefsCommand)
        pref = parse_command("pref bagels")
        assert isinstance(pref, PrefCommand)
        assert pref.item == "bagels"
        forget = parse_command("forget bagels")
        assert isinstance(forget, ForgetPrefCommand)
        assert forget.item == "bagels"
        remove = parse_command("remove preference bagels")
        assert isinstance(remove, ForgetPrefCommand)
        change = parse_command("change bagels")
        assert isinstance(change, ChangePrefCommand)
        alias = parse_command("alias bagels to bagel")
        assert isinstance(alias, AliasPrefCommand)
        assert alias.new_key == "bagels"
        assert alias.existing_key == "bagel"

    @patch("finch_telegram.handler.telegram_client.send_text_message")
    @patch("finch_telegram.handler.finch_client.preferences_list")
    def test_prefs_handler(self, mock_list, mock_send, monkeypatch):
        monkeypatch.setenv("FINCH_TELEGRAM_TEST_MODE", "1")
        monkeypatch.setenv("FINCH_TELEGRAM_BOT_TOKEN", "test-telegram-bot-token")
        monkeypatch.setenv("FINCH_TELEGRAM_ALLOWED_USER_IDS", "111222333")
        monkeypatch.setenv("FINCH_API_KEY", "test-fin-api-key")

        from finch_telegram.handler import process_inbound
        from finch_telegram.telegram_client import InboundTextMessage

        mock_list.return_value = {
            "text": "Saved preferences:\nbagel -> Thomas Plain Bagels",
        }
        process_inbound(
            InboundTextMessage(
                chat_id="111222333",
                user_id="111222333",
                text="prefs",
                update_id=60,
            )
        )
        body = mock_send.call_args[0][1]
        assert "bagel -> Thomas Plain Bagels" in body

    @patch("finch_telegram.handler.telegram_client.send_text_message")
    @patch("finch_telegram.handler.finch_client.preference_change")
    def test_change_handler(self, mock_change, mock_send, monkeypatch):
        monkeypatch.setenv("FINCH_TELEGRAM_TEST_MODE", "1")
        monkeypatch.setenv("FINCH_TELEGRAM_BOT_TOKEN", "test-telegram-bot-token")
        monkeypatch.setenv("FINCH_TELEGRAM_ALLOWED_USER_IDS", "111222333")
        monkeypatch.setenv("FINCH_API_KEY", "test-fin-api-key")

        from finch_telegram.handler import process_inbound
        from finch_telegram.telegram_client import InboundTextMessage

        mock_change.return_value = {
            "needs_choice": True,
            "requested_item": "bagels",
            "results": [{"description": "Thomas Plain Bagels", "price": "$3.99"}],
        }
        process_inbound(
            InboundTextMessage(
                chat_id="111222333",
                user_id="111222333",
                text="change bagels",
                update_id=61,
            )
        )
        mock_change.assert_called_once()
        body = mock_send.call_args[0][1]
        assert "Needs choice" in body
