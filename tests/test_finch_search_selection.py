"""
Tests for Finch Kroger search-selection flow (pending choice, prefer, Telegram replies).
"""

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
    clear_pending_selection,
    get_pending_selection,
    make_chat_key,
)


@pytest.fixture(autouse=True)
def _search_selection_env_isolation(monkeypatch):
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
    monkeypatch.setenv("FINCH_SEARCH_RESULT_LIMIT", "10")


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
        description="Plain Bagels 6 ct",
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


def _tortilla_results(count: int = 25) -> list[PendingSearchResult]:
    return [
        PendingSearchResult(
            product_id=f"tortilla-{index}",
            upc=f"0001111{index:06d}",
            description=f"Tortilla option {index}",
            size=f"{index} ct",
            price=f"${index}.99",
        )
        for index in range(1, count + 1)
    ]


def _patch_search(
    results: list[PendingSearchResult] | None = None,
    *,
    total_count: int | None = None,
):
    items = results if results is not None else BAGEL_RESULTS
    resolved_total = total_count if total_count is not None else len(items)

    def fake_search(query: str, *, client=None, limit: int = 10, start: int = 0):
        from finch.kroger_client import KrogerProduct, ProductSearchResult

        page = items[start : start + limit]
        products = [
            KrogerProduct(
                product_id=item.product_id,
                upc=item.upc,
                description=item.description,
                size=item.size,
                price=item.price.replace("$", "") if item.price else None,
            )
            for item in page
        ]
        return ProductSearchResult(products=products, total_count=resolved_total)

    return patch("finch.cart_choice.run_search", side_effect=fake_search)


class TestSearchSelectionApi:
    def test_no_preference_returns_search_results(self, api_client):
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.load_kroger_client_from_env", return_value=_mock_kroger_client()):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search():
                        response = api_client.post(
                            "/finch/cart/add",
                            json={"item": "bagels", "chat_key": CHAT_KEY, "source": "telegram"},
                            headers=AUTH_HEADERS,
                        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["needs_choice"] is True
        assert payload["normalized_name"] == "bagel"
        assert len(payload["results"]) == 2
        assert payload["results"][0]["description"] == "Plain Bagels 6 ct"
        assert payload["results"][0]["price"] == "$3.99"
        assert payload["page_start"] == 1
        assert payload["page_end"] == 2
        pending = get_pending_selection(CHAT_KEY)
        assert pending is not None
        assert pending.normalized_name == "bagel"

    def test_choose_one_adds_selected_item(self, api_client):
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            mock_client = _mock_kroger_client()
            with patch("finch.api.load_kroger_client_from_env", return_value=mock_client):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search():
                        api_client.post(
                            "/finch/cart/add",
                            json={"item": "bagels", "chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
                        choose = api_client.post(
                            "/finch/cart/choose",
                            json={
                                "chat_key": CHAT_KEY,
                                "selection": 1,
                                "source": "telegram",
                            },
                            headers=AUTH_HEADERS,
                        )
        assert choose.status_code == 200
        payload = choose.json()
        assert payload["ok"] is True
        assert payload["attempt"]["upc"] == "0001111000001"
        mock_client.add_to_cart.assert_called_once()
        assert get_pending_selection(CHAT_KEY) is None

    def test_prefer_one_saves_alias_and_adds(self, api_client, alias_db: Path):
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            mock_client = _mock_kroger_client()
            with patch("finch.api.load_kroger_client_from_env", return_value=mock_client):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search():
                        api_client.post(
                            "/finch/cart/add",
                            json={"item": "bagels", "chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
                        choose = api_client.post(
                            "/finch/cart/choose",
                            json={
                                "chat_key": CHAT_KEY,
                                "selection": 1,
                                "prefer": True,
                                "source": "telegram",
                            },
                            headers=AUTH_HEADERS,
                        )
        assert choose.status_code == 200
        assert choose.json()["preferred"] is True

        from finch.aliases import get_alias

        saved = get_alias("bagel", alias_db)
        assert saved is not None
        assert saved.upc == "0001111000001"

    def test_future_add_uses_saved_preference(self, api_client, alias_db: Path):
        from finch.aliases import upsert_alias
        from finch.models import AliasEntry

        upsert_alias(
            AliasEntry(
                alias_key="bagel",
                display_name="Plain Bagels 6 ct",
                kroger_product_id="bagel-1",
                upc="0001111000001",
                search_term="bagel",
                notes="test",
            ),
            alias_db,
        )
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            mock_client = _mock_kroger_client()
            with patch("finch.api.load_kroger_client_from_env", return_value=mock_client):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search() as mock_search:
                        response = api_client.post(
                            "/finch/cart/add",
                            json={"item": "bagels", "chat_key": CHAT_KEY, "source": "telegram"},
                            headers=AUTH_HEADERS,
                        )
        assert response.status_code == 200
        payload = response.json()
        assert payload.get("needs_choice") is not True
        assert payload["ok"] is True
        mock_search.assert_not_called()
        mock_client.add_to_cart.assert_called_once()

    def test_nvm_clears_pending_selection(self, api_client):
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.load_kroger_client_from_env", return_value=_mock_kroger_client()):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search():
                        api_client.post(
                            "/finch/cart/add",
                            json={"item": "bagels", "chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
        assert get_pending_selection(CHAT_KEY) is not None
        cancel = api_client.post(
            "/finch/cart/pending/cancel",
            json={"chat_key": CHAT_KEY},
            headers=AUTH_HEADERS,
        )
        assert cancel.status_code == 200
        assert cancel.json()["cleared"] is True
        assert get_pending_selection(CHAT_KEY) is None

    def test_search_query_replaces_pending_results(self, api_client):
        alt_results = [
            PendingSearchResult(
                product_id="bagel-3",
                upc="0001111000003",
                description="Mini Bagels 12 ct",
                size="12 ct",
                price="$5.49",
            ),
        ]
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.load_kroger_client_from_env", return_value=_mock_kroger_client()):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search():
                        api_client.post(
                            "/finch/cart/add",
                            json={"item": "bagels", "chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
                    with _patch_search(alt_results):
                        search = api_client.post(
                            "/finch/cart/pending/search",
                            json={"chat_key": CHAT_KEY, "query": "mini bagels"},
                            headers=AUTH_HEADERS,
                        )
        assert search.status_code == 200
        payload = search.json()
        assert payload["needs_choice"] is True
        assert payload["search_query"] == "mini bagels"
        assert payload["results"][0]["description"] == "Mini Bagels 12 ct"
        pending = get_pending_selection(CHAT_KEY)
        assert pending is not None
        assert pending.search_query == "mini bagels"

    def test_duplicate_guard_blocks_repeated_selected_adds(self, api_client):
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            mock_client = _mock_kroger_client()
            with patch("finch.api.load_kroger_client_from_env", return_value=mock_client):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search():
                        api_client.post(
                            "/finch/cart/add",
                            json={"item": "bagels", "chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
                        api_client.post(
                            "/finch/cart/choose",
                            json={
                                "chat_key": CHAT_KEY,
                                "selection": 1,
                                "prefer": True,
                            },
                            headers=AUTH_HEADERS,
                        )
                        second = api_client.post(
                            "/finch/cart/add",
                            json={"item": "bagels", "chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
        assert second.status_code == 200
        assert second.json()["duplicate"] is True
        assert mock_client.add_to_cart.call_count == 1

    def test_force_add_bypasses_duplicate_guard(self, api_client):
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            mock_client = _mock_kroger_client()
            with patch("finch.api.load_kroger_client_from_env", return_value=mock_client):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search():
                        api_client.post(
                            "/finch/cart/add",
                            json={"item": "bagels", "chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
                        api_client.post(
                            "/finch/cart/choose",
                            json={
                                "chat_key": CHAT_KEY,
                                "selection": 1,
                                "prefer": True,
                            },
                            headers=AUTH_HEADERS,
                        )
                        force = api_client.post(
                            "/finch/cart/add",
                            json={"item": "force add bagels", "chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
        assert force.status_code == 200
        assert force.json()["ok"] is True
        assert mock_client.add_to_cart.call_count == 2

    def test_add_list_unresolved_returns_needs_choice(self, api_client):
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            mock_client = _mock_kroger_client()
            with patch("finch.api.load_kroger_client_from_env", return_value=mock_client):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search():
                        response = api_client.post(
                            "/finch/cart/add-list",
                            json={
                                "text": "eggs, bagels",
                                "chat_key": CHAT_KEY,
                                "source": "telegram",
                            },
                            headers=AUTH_HEADERS,
                        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["needs_choice"] is True
        assert payload["normalized_name"] == "bagel"
        assert len(payload.get("partial_outcomes") or []) == 1
        assert mock_client.add_to_cart.call_count == 1


class TestSearchPaginationApi:
    def test_initial_display_shows_ten_results(self, api_client):
        tortillas = _tortilla_results(25)
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.load_kroger_client_from_env", return_value=_mock_kroger_client()):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search(tortillas, total_count=37):
                        response = api_client.post(
                            "/finch/cart/add",
                            json={"item": "tortillas", "chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["needs_choice"] is True
        assert len(payload["results"]) == 10
        assert payload["page_start"] == 1
        assert payload["page_end"] == 10
        assert payload["total_count"] == 37
        assert payload["has_more"] is True
        assert payload["has_back"] is False
        assert payload["results"][0]["description"] == "Tortilla option 1"
        assert payload["results"][9]["description"] == "Tortilla option 10"

    def test_select_result_8_on_first_page(self, api_client):
        tortillas = _tortilla_results(15)
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            mock_client = _mock_kroger_client()
            with patch("finch.api.load_kroger_client_from_env", return_value=mock_client):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search(tortillas):
                        api_client.post(
                            "/finch/cart/add",
                            json={"item": "tortillas", "chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
                        choose = api_client.post(
                            "/finch/cart/choose",
                            json={"chat_key": CHAT_KEY, "selection": 8},
                            headers=AUTH_HEADERS,
                        )
        assert choose.status_code == 200
        payload = choose.json()
        assert payload["ok"] is True
        assert payload["attempt"]["product_id"] == "tortilla-8"
        assert get_pending_selection(CHAT_KEY) is None

    def test_select_result_14_after_more(self, api_client):
        tortillas = _tortilla_results(25)
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            mock_client = _mock_kroger_client()
            with patch("finch.api.load_kroger_client_from_env", return_value=mock_client):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search(tortillas, total_count=25) as mock_search:
                        api_client.post(
                            "/finch/cart/add",
                            json={"item": "tortillas", "chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
                        more = api_client.post(
                            "/finch/cart/pending/more",
                            json={"chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
                        choose = api_client.post(
                            "/finch/cart/choose",
                            json={"chat_key": CHAT_KEY, "selection": 14},
                            headers=AUTH_HEADERS,
                        )
        assert more.status_code == 200
        more_payload = more.json()
        assert more_payload["page_start"] == 11
        assert more_payload["page_end"] == 20
        assert more_payload["has_back"] is True
        assert mock_search.call_count == 2
        assert choose.status_code == 200
        assert choose.json()["attempt"]["product_id"] == "tortilla-14"

    def test_more_uses_cached_results_without_extra_search(self, api_client):
        tortillas = _tortilla_results(25)
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.load_kroger_client_from_env", return_value=_mock_kroger_client()):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search(tortillas, total_count=25) as mock_search:
                        api_client.post(
                            "/finch/cart/add",
                            json={"item": "tortillas", "chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
                        first_more = api_client.post(
                            "/finch/cart/pending/more",
                            json={"chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
                        second_more = api_client.post(
                            "/finch/cart/pending/more",
                            json={"chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
        assert first_more.status_code == 200
        assert second_more.status_code == 200
        assert second_more.json()["page_start"] == 21
        assert second_more.json()["page_end"] == 25
        assert mock_search.call_count == 3

    def test_back_returns_previous_page(self, api_client):
        tortillas = _tortilla_results(25)
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.load_kroger_client_from_env", return_value=_mock_kroger_client()):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search(tortillas, total_count=25):
                        api_client.post(
                            "/finch/cart/add",
                            json={"item": "tortillas", "chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
                        api_client.post(
                            "/finch/cart/pending/more",
                            json={"chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
                        back = api_client.post(
                            "/finch/cart/pending/back",
                            json={"chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
        assert back.status_code == 200
        payload = back.json()
        assert payload["page_start"] == 1
        assert payload["page_end"] == 10
        assert payload["has_back"] is False


class TestPendingSelectionStore:
    def test_pending_expires(self, pending_db: Path, monkeypatch):
        monkeypatch.setenv("FINCH_PENDING_SELECTION_DB_PATH", str(pending_db))
        monkeypatch.setenv("FINCH_PENDING_SELECTION_TTL_MINUTES", "0")
        from finch.pending_selection import save_pending_selection

        save_pending_selection(
            chat_key=CHAT_KEY,
            requested_item="bagels",
            normalized_name="bagels",
            search_query="bagels",
            quantity=1,
            cached_results=BAGEL_RESULTS,
            db_path=pending_db,
            ttl_minutes=0,
        )
        assert get_pending_selection(CHAT_KEY, db_path=pending_db) is None


class TestTelegramSearchSelection:
    @patch("finch_telegram.handler.telegram_client.send_text_message")
    @patch("finch_telegram.handler.finch_client.cart_add")
    def test_add_unresolved_formats_needs_choice(self, mock_add, mock_send, monkeypatch):
        monkeypatch.setenv("FINCH_TELEGRAM_TEST_MODE", "1")
        monkeypatch.setenv("FINCH_TELEGRAM_BOT_TOKEN", "test-telegram-bot-token")
        monkeypatch.setenv("FINCH_TELEGRAM_ALLOWED_USER_IDS", "111222333")
        monkeypatch.setenv("FINCH_API_KEY", "test-fin-api-key")

        from finch_telegram.handler import process_inbound
        from finch_telegram.telegram_client import InboundTextMessage

        mock_add.return_value = {
            "needs_choice": True,
            "requested_item": "bagels",
            "search_query": "bagels",
            "page_start": 1,
            "page_end": 1,
            "has_more": False,
            "has_back": False,
            "results": [
                {
                    "description": "Plain Bagels 6 ct",
                    "size": "6 ct",
                    "price": "$3.99",
                }
            ],
        }
        message = InboundTextMessage(
            chat_id="111222333",
            user_id="111222333",
            text="add bagels",
            update_id=50,
        )
        process_inbound(message)
        mock_add.assert_called_once()
        _, kwargs = mock_add.call_args
        assert kwargs["chat_key"] == CHAT_KEY
        body = mock_send.call_args[0][1]
        assert "Found multiple matches" in body
        assert "1. Plain Bagels 6 ct" in body
        assert "cancel" in body.lower()

    @patch("finch_telegram.handler.telegram_client.send_text_message")
    @patch("finch_telegram.handler.finch_client.pending_more")
    def test_more_reply_formats_paginated_results(self, mock_more, mock_send, monkeypatch):
        monkeypatch.setenv("FINCH_TELEGRAM_TEST_MODE", "1")
        monkeypatch.setenv("FINCH_TELEGRAM_BOT_TOKEN", "test-telegram-bot-token")
        monkeypatch.setenv("FINCH_TELEGRAM_ALLOWED_USER_IDS", "111222333")
        monkeypatch.setenv("FINCH_API_KEY", "test-fin-api-key")

        from finch_telegram.handler import process_inbound
        from finch_telegram.telegram_client import InboundTextMessage

        mock_more.return_value = {
            "needs_choice": True,
            "requested_item": "tortillas",
            "search_query": "tortillas",
            "total_count": 37,
            "page_start": 11,
            "page_end": 20,
            "has_more": True,
            "has_back": True,
            "results": [
                {
                    "description": f"Tortilla option {index}",
                    "size": f"{index} ct",
                }
                for index in range(11, 21)
            ],
        }
        message = InboundTextMessage(
            chat_id="111222333",
            user_id="111222333",
            text="more",
            update_id=53,
        )
        process_inbound(message)
        mock_more.assert_called_once_with(CHAT_KEY)
        body = mock_send.call_args[0][1]
        assert "Showing 11-20 of 37" in body
        assert "11. Tortilla option 11" in body
        assert "- back" in body
        assert "- more" in body

    @patch("finch_telegram.handler.telegram_client.send_text_message")
    @patch("finch_telegram.handler.finch_client.cart_choose")
    def test_reply_one_chooses_product(self, mock_choose, mock_send, monkeypatch):
        monkeypatch.setenv("FINCH_TELEGRAM_TEST_MODE", "1")
        monkeypatch.setenv("FINCH_TELEGRAM_BOT_TOKEN", "test-telegram-bot-token")
        monkeypatch.setenv("FINCH_TELEGRAM_ALLOWED_USER_IDS", "111222333")
        monkeypatch.setenv("FINCH_API_KEY", "test-fin-api-key")

        from finch_telegram.handler import process_inbound
        from finch_telegram.telegram_client import InboundTextMessage

        mock_choose.return_value = {
            "ok": True,
            "attempt": {
                "normalized_name": "bagels",
                "alias_name": "Plain Bagels 6 ct",
            },
        }
        message = InboundTextMessage(
            chat_id="111222333",
            user_id="111222333",
            text="1",
            update_id=51,
        )
        process_inbound(message)
        mock_choose.assert_called_once_with(
            CHAT_KEY,
            1,
            prefer=False,
            source="telegram",
        )

    @patch("finch_telegram.handler.telegram_client.send_text_message")
    @patch("finch_telegram.handler.finch_client.pending_cancel")
    def test_nvm_cancels_pending(self, mock_cancel, mock_send, monkeypatch):
        monkeypatch.setenv("FINCH_TELEGRAM_TEST_MODE", "1")
        monkeypatch.setenv("FINCH_TELEGRAM_BOT_TOKEN", "test-telegram-bot-token")
        monkeypatch.setenv("FINCH_TELEGRAM_ALLOWED_USER_IDS", "111222333")
        monkeypatch.setenv("FINCH_API_KEY", "test-fin-api-key")

        from finch_telegram.handler import process_inbound
        from finch_telegram.telegram_client import InboundTextMessage

        mock_cancel.return_value = {
            "ok": True,
            "cleared": True,
            "message": "Cancelled pending product choice.",
        }
        message = InboundTextMessage(
            chat_id="111222333",
            user_id="111222333",
            text="nvm",
            update_id=52,
        )
        process_inbound(message)
        mock_cancel.assert_called_once_with(CHAT_KEY)

    def test_help_text_mentions_choose_flow(self):
        from finch_telegram.commands import HELP_TEXT

        assert "prefer 1" in HELP_TEXT
        assert "more" in HELP_TEXT
        assert "back" in HELP_TEXT
        assert "cancel" in HELP_TEXT
        assert "FINCH_LIVE_CART" not in HELP_TEXT
        assert "Kroger is still the live cart" in HELP_TEXT

    def test_parse_pending_replies(self):
        from finch_telegram.commands import (
            BackPendingCommand,
            CancelPendingCommand,
            ChooseReplyCommand,
            MorePendingCommand,
            SearchPendingCommand,
            parse_pending_reply,
        )

        assert isinstance(parse_pending_reply("1"), ChooseReplyCommand)
        assert parse_pending_reply("1").selection == 1
        assert isinstance(parse_pending_reply("choose 2"), ChooseReplyCommand)
        assert parse_pending_reply("choose 2").selection == 2
        prefer = parse_pending_reply("prefer 3")
        assert isinstance(prefer, ChooseReplyCommand)
        assert prefer.prefer is True
        assert isinstance(parse_pending_reply("nvm"), CancelPendingCommand)
        assert isinstance(parse_pending_reply("cancel"), CancelPendingCommand)
        assert isinstance(parse_pending_reply("more"), MorePendingCommand)
        assert isinstance(parse_pending_reply("back"), BackPendingCommand)
        search = parse_pending_reply("search mini bagels")
        assert isinstance(search, SearchPendingCommand)
        assert search.query == "mini bagels"
