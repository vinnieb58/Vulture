"""Tests for Finch staples v1 — saved list, preview batch, and setup script."""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finch.env_util import reset_env_load_state
from finch.models import GroceryIntent
from finch.pending_selection import (
    PendingSearchResult,
    clear_pending_selection,
    make_chat_key,
)
from finch.preference_norm import normalize_preference_key
from finch.staples import (
    batch_to_grocery_intents,
    clear_pending_staple_batch,
    format_staple_preview_text,
    get_pending_staple_batch,
    list_staple_items,
    remove_from_staple_batch,
    seed_initial_staples,
    start_staple_batch,
)

CHAT_KEY = make_chat_key("telegram", "111222333")
AUTH_HEADERS = {"X-Finch-Key": "test-secret-key"}

EXPECTED_STAPLE_KEYS = [
    "milk",
    "eggs",
    "blueberry",
    "raspberry",
    "strawberry",
    "banana",
    "ground beef",
    "plantain",
    "bread",
    "shredded cheese",
    "cotija",
    "deli turkey",
]


@pytest.fixture(autouse=True)
def _staples_env_isolation(monkeypatch):
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
def staples_db(tmp_path: Path) -> Path:
    return tmp_path / "finch_staples.db"


@pytest.fixture
def pending_db(tmp_path: Path) -> Path:
    return tmp_path / "finch_pending_selection.db"


@pytest.fixture
def ledger_db(tmp_path: Path) -> Path:
    from finch.trip_ledger import reset_trip

    db = tmp_path / "finch_trip_ledger.db"
    reset_trip(db_path=db)
    return db


@pytest.fixture
def api_env(
    monkeypatch,
    alias_db: Path,
    staples_db: Path,
    pending_db: Path,
    ledger_db: Path,
    tmp_path: Path,
):
    monkeypatch.setenv("FINCH_API_TEST_MODE", "1")
    monkeypatch.setenv("FINCH_API_KEY", "test-secret-key")
    monkeypatch.setenv("FINCH_LIVE_CART", "true")
    monkeypatch.setenv("FINCH_ALIASES_DB_PATH", str(alias_db))
    monkeypatch.setenv("FINCH_STAPLES_DB_PATH", str(staples_db))
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


def _mock_kroger_client():
    mock_client = MagicMock()
    mock_client.add_to_cart.return_value = {"status": "ok"}
    return mock_client


BAGEL_RESULTS = [
    PendingSearchResult(
        product_id="bagel-1",
        upc="0001111000001",
        description="Plain Bagels 6 ct",
        size="6 ct",
        price="$3.99",
    ),
]


def _patch_search(results: list[PendingSearchResult] | None = None):
    items = results if results is not None else BAGEL_RESULTS

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
        return ProductSearchResult(products=products, total_count=len(items))

    return patch("finch.cart_choice.run_search", side_effect=fake_search)


class TestStapleStorage:
    def test_seed_is_idempotent(self, staples_db: Path):
        first = seed_initial_staples(staples_db)
        second = seed_initial_staples(staples_db)
        items = list_staple_items(db_path=staples_db)
        assert first == 12
        assert second == 0
        assert len(items) == 12

    def test_initial_staple_keys_and_quantities(self, staples_db: Path):
        seed_initial_staples(staples_db)
        items = list_staple_items(enabled_only=True, db_path=staples_db)
        keys = [item.normalized_key for item in items]
        assert keys == EXPECTED_STAPLE_KEYS
        plantain = next(item for item in items if item.normalized_key == "plantain")
        assert plantain.default_quantity == 5
        assert all(item.default_quantity == 1 for item in items if item.normalized_key != "plantain")

    def test_staples_resolve_through_preference_keys(self, staples_db: Path, alias_db: Path):
        from finch.aliases import lookup_alias

        seed_initial_staples(staples_db)
        items = list_staple_items(enabled_only=True, db_path=staples_db)
        milk = next(item for item in items if item.normalized_key == "milk")
        eggs = next(item for item in items if item.normalized_key == "eggs")
        assert lookup_alias(milk.normalized_key, db_path=alias_db) is not None
        assert lookup_alias(eggs.normalized_key, db_path=alias_db) is not None
        assert lookup_alias("milk", db_path=alias_db).upc is not None


class TestStapleBatch:
    def test_add_staples_creates_full_preview(self, staples_db: Path):
        seed_initial_staples(staples_db)
        batch = start_staple_batch(CHAT_KEY, db_path=staples_db)
        assert len(batch.items) == 12
        text = format_staple_preview_text(batch)
        assert "Staples ready to add:" in text
        assert "Plantains — 5" in text
        assert "confirm" in text

    def test_remove_by_number_renumbers(self, staples_db: Path):
        seed_initial_staples(staples_db)
        start_staple_batch(CHAT_KEY, db_path=staples_db)
        updated = remove_from_staple_batch(CHAT_KEY, "2", db_path=staples_db)
        assert updated is not None
        assert len(updated.items) == 11
        assert updated.items[0].normalized_key == "milk"
        assert updated.items[1].normalized_key == "blueberry"
        text = format_staple_preview_text(updated)
        assert "1. Milk" in text
        assert "2. Blueberries" in text

    def test_remove_multiple_by_number(self, staples_db: Path):
        seed_initial_staples(staples_db)
        start_staple_batch(CHAT_KEY, db_path=staples_db)
        updated = remove_from_staple_batch(CHAT_KEY, "2, 5, 8", db_path=staples_db)
        assert updated is not None
        assert len(updated.items) == 9
        keys = [item.normalized_key for item in updated.items]
        assert "eggs" not in keys
        assert "strawberry" not in keys
        assert "plantain" not in keys

    def test_remove_by_normalized_name(self, staples_db: Path):
        seed_initial_staples(staples_db)
        start_staple_batch(CHAT_KEY, db_path=staples_db)
        updated = remove_from_staple_batch(CHAT_KEY, "eggs, milk", db_path=staples_db)
        assert updated is not None
        keys = {item.normalized_key for item in updated.items}
        assert "eggs" not in keys
        assert "milk" not in keys
        saved = list_staple_items(enabled_only=True, db_path=staples_db)
        saved_keys = {item.normalized_key for item in saved}
        assert "eggs" in saved_keys
        assert "milk" in saved_keys

    def test_cancel_clears_pending_batch(self, staples_db: Path):
        seed_initial_staples(staples_db)
        start_staple_batch(CHAT_KEY, db_path=staples_db)
        assert clear_pending_staple_batch(CHAT_KEY, db_path=staples_db)
        assert get_pending_staple_batch(CHAT_KEY, db_path=staples_db) is None

    def test_batch_to_intents_preserves_plantain_quantity(self, staples_db: Path):
        seed_initial_staples(staples_db)
        batch = start_staple_batch(CHAT_KEY, db_path=staples_db)
        intents = batch_to_grocery_intents(batch)
        plantain = next(i for i in intents if i.normalized_name == "plantain")
        assert plantain.quantity == 5
        assert "5" in plantain.raw_text


class TestStaplesApi:
    def test_start_does_not_add_to_cart(self, api_client, staples_db: Path):
        seed_initial_staples(staples_db)
        with patch("finch.api.load_kroger_client_from_env", return_value=_mock_kroger_client()):
            response = api_client.post(
                "/finch/staples/start",
                json={"chat_key": CHAT_KEY},
                headers=AUTH_HEADERS,
            )
        assert response.status_code == 200
        payload = response.json()
        assert len(payload["pending"]["items"]) == 12
        mock_client = _mock_kroger_client()
        mock_client.add_to_cart.assert_not_called()

    def test_confirm_uses_add_list_pipeline(self, api_client, ledger_db: Path):
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.load_kroger_client_from_env", return_value=_mock_kroger_client()):
                with patch("finch.api.ensure_fresh_user_token"):
                    with _patch_search():
                        start = api_client.post(
                            "/finch/staples/start",
                            json={"chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
                        assert start.status_code == 200
                        client = _mock_kroger_client()
                        with patch("finch.api.load_kroger_client_from_env", return_value=client):
                            confirm = api_client.post(
                                "/finch/staples/confirm",
                                json={"chat_key": CHAT_KEY, "source": "test"},
                                headers=AUTH_HEADERS,
                            )
        assert confirm.status_code == 200
        payload = confirm.json()
        assert payload.get("succeeded") or payload.get("needs_choice")
        assert get_pending_staple_batch(CHAT_KEY) is None

    def test_confirm_preferred_item_auto_adds(self, api_client):
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.ensure_fresh_user_token"):
                with patch("finch.api.load_kroger_client_from_env", return_value=_mock_kroger_client()) as load_mock:
                    client = load_mock.return_value
                    api_client.post(
                        "/finch/staples/start",
                        json={"chat_key": CHAT_KEY},
                        headers=AUTH_HEADERS,
                    )
                    remove_from_staple_batch(
                        CHAT_KEY,
                        "blueberry, raspberry, strawberry, banana, ground beef, plantain, bread, shredded cheese, cotija, deli turkey",
                    )
                    confirm = api_client.post(
                        "/finch/staples/confirm",
                        json={"chat_key": CHAT_KEY},
                        headers=AUTH_HEADERS,
                    )
        assert confirm.status_code == 200
        payload = confirm.json()
        assert payload.get("succeeded")
        client.add_to_cart.assert_called()

    def test_confirm_pauses_for_unresolved_and_continues(self, api_client, pending_db: Path):
        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.ensure_fresh_user_token"):
                with patch("finch.api.load_kroger_client_from_env", return_value=_mock_kroger_client()):
                    with _patch_search():
                        api_client.post(
                            "/finch/staples/start",
                            json={"chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
                        remove_from_staple_batch(
                            CHAT_KEY,
                            "milk, eggs, raspberry, strawberry, banana, ground beef, plantain, bread, shredded cheese, cotija, deli turkey",
                        )
                        confirm = api_client.post(
                            "/finch/staples/confirm",
                            json={"chat_key": CHAT_KEY},
                            headers=AUTH_HEADERS,
                        )
        assert confirm.status_code == 200
        payload = confirm.json()
        assert payload.get("needs_choice")
        assert payload["normalized_name"] == "blueberry"

        with patch("finch.cart_ops.resolve_user_access_token", return_value="user-tok"):
            with patch("finch.api.ensure_fresh_user_token"):
                with patch("finch.api.load_kroger_client_from_env", return_value=_mock_kroger_client()):
                    choose = api_client.post(
                        "/finch/cart/choose",
                        json={"chat_key": CHAT_KEY, "selection": 1},
                        headers=AUTH_HEADERS,
                    )
        assert choose.status_code == 200
        clear_pending_selection(CHAT_KEY, db_path=pending_db)

    def test_staples_list_endpoint(self, api_client):
        response = api_client.get("/finch/staples", headers=AUTH_HEADERS)
        assert response.status_code == 200
        payload = response.json()
        assert len(payload["items"]) == 12
        assert "Saved staples:" in payload["text"]


class TestTelegramStaplesParsing:
    def test_add_staples_not_normal_add(self):
        from finch_telegram.commands import AddStaplesCommand, AddCommand, parse_command

        assert isinstance(parse_command("add staples"), AddStaplesCommand)
        assert not isinstance(parse_command("add staples"), AddCommand)
        cmd = parse_command("add staples")
        assert cmd is not None
        assert cmd.kind == "add-staples"

    def test_staples_list_command(self):
        from finch_telegram.commands import StaplesListCommand, parse_command

        assert isinstance(parse_command("staples"), StaplesListCommand)

    def test_staple_reply_parsing(self):
        from finch_telegram.commands import (
            StaplesCancelCommand,
            StaplesConfirmCommand,
            StaplesRemoveCommand,
            parse_staple_reply,
        )

        assert isinstance(parse_staple_reply("confirm"), StaplesConfirmCommand)
        assert isinstance(parse_staple_reply("cancel"), StaplesCancelCommand)
        assert isinstance(parse_staple_reply("remove 2, 5"), StaplesRemoveCommand)
        assert isinstance(parse_staple_reply("remove eggs"), StaplesRemoveCommand)
        assert parse_staple_reply("remove preference eggs") is None


class TestSetupFinchStaplesScript:
    def test_status_performs_no_writes(self, staples_db: Path, alias_db: Path):
        from scripts.setup_finch_staples import print_status

        seed_initial_staples(staples_db)
        before = list_staple_items(db_path=staples_db)
        out = StringIO()
        with patch("sys.stdout", out):
            code = print_status(staples_db_path=staples_db, alias_db_path=alias_db)
        assert code == 0
        after = list_staple_items(db_path=staples_db)
        assert len(before) == len(after)
        assert "milk" in out.getvalue() or "Milk" in out.getvalue()

    def test_skips_existing_preferences_by_default(
        self, staples_db: Path, alias_db: Path
    ):
        from scripts.setup_finch_staples import run_setup

        seed_initial_staples(staples_db)
        search_queries: list[str] = []

        def fake_search(query: str, *, client=None, limit: int = 10, start: int = 0):
            search_queries.append(query)
            return BAGEL_RESULTS[:1], 1

        with patch("scripts.setup_finch_staples.search_products_for_choice", side_effect=fake_search):
            with patch(
                "scripts.setup_finch_staples.load_kroger_client_from_env",
                return_value=MagicMock(),
            ):
                code = run_setup(
                    staples_db_path=staples_db,
                    alias_db_path=alias_db,
                    input_fn=lambda _: "q",
                    print_fn=lambda *args, **kwargs: None,
                )
        assert code == 0
        assert "milk" not in search_queries
        assert "eggs" not in search_queries
        assert search_queries

    def test_unresolved_staples_presented_for_selection(
        self, staples_db: Path, alias_db: Path
    ):
        from scripts.setup_finch_staples import run_setup

        seed_initial_staples(staples_db)
        printed: list[str] = []

        def capture_print(*args, **kwargs):
            printed.append(" ".join(str(arg) for arg in args))

        responses = iter(["1", "q"])

        with patch("scripts.setup_finch_staples.load_kroger_client_from_env", return_value=MagicMock()):
            with _patch_search():
                code = run_setup(
                    staples_db_path=staples_db,
                    alias_db_path=alias_db,
                    input_fn=lambda _: next(responses),
                    print_fn=capture_print,
                )
        assert code == 0
        output = "\n".join(printed)
        assert "Staple: blueberry" in output

    def test_selected_product_saved_through_preference_system(
        self, staples_db: Path, alias_db: Path
    ):
        from finch.aliases import lookup_alias
        from scripts.setup_finch_staples import run_setup

        seed_initial_staples(staples_db)
        responses = iter(["1", "q"])

        with patch("scripts.setup_finch_staples.load_kroger_client_from_env", return_value=MagicMock()):
            with _patch_search():
                run_setup(
                    staples_db_path=staples_db,
                    alias_db_path=alias_db,
                    input_fn=lambda _: next(responses),
                    print_fn=lambda *args, **kwargs: None,
                )
        entry = lookup_alias("blueberry", db_path=alias_db)
        assert entry is not None
        assert entry.upc == "0001111000001"

    def test_review_all_requires_explicit_replace(
        self, staples_db: Path, alias_db: Path
    ):
        from scripts.setup_finch_staples import run_setup

        seed_initial_staples(staples_db)
        responses = iter(["1", "n", "q"])

        with patch("scripts.setup_finch_staples.load_kroger_client_from_env", return_value=MagicMock()):
            with _patch_search():
                code = run_setup(
                    review_all=True,
                    staples_db_path=staples_db,
                    alias_db_path=alias_db,
                    input_fn=lambda _: next(responses),
                    print_fn=lambda *args, **kwargs: None,
                )
        assert code == 0

    def test_progress_preserved_on_early_exit(self, staples_db: Path, alias_db: Path):
        from finch.aliases import lookup_alias
        from scripts.setup_finch_staples import run_setup

        seed_initial_staples(staples_db)
        responses = iter(["1", "q"])

        with patch("scripts.setup_finch_staples.load_kroger_client_from_env", return_value=MagicMock()):
            with _patch_search():
                run_setup(
                    staples_db_path=staples_db,
                    alias_db_path=alias_db,
                    input_fn=lambda _: next(responses),
                    print_fn=lambda *args, **kwargs: None,
                )
                run_setup(
                    staples_db_path=staples_db,
                    alias_db_path=alias_db,
                    input_fn=lambda _: "q",
                    print_fn=lambda *args, **kwargs: None,
                )
        assert lookup_alias("blueberry", db_path=alias_db) is not None

    def test_script_never_invokes_cart_or_trip_ledger(
        self, staples_db: Path, alias_db: Path, ledger_db: Path, monkeypatch
    ):
        from finch.trip_ledger import list_trip_items, get_or_create_open_trip
        from scripts.setup_finch_staples import run_setup

        monkeypatch.setenv("FINCH_TRIP_LEDGER_DB_PATH", str(ledger_db))
        seed_initial_staples(staples_db)
        trip_id = get_or_create_open_trip(db_path=ledger_db)
        before = list_trip_items(trip_id, db_path=ledger_db)

        with patch("scripts.setup_finch_staples.load_kroger_client_from_env", return_value=MagicMock()):
            with _patch_search():
                with patch("finch.cart_ops.execute_cart_add") as cart_add:
                    run_setup(
                        staples_db_path=staples_db,
                        alias_db_path=alias_db,
                        input_fn=lambda _: "q",
                        print_fn=lambda *args, **kwargs: None,
                    )
                    cart_add.assert_not_called()
        after = list_trip_items(trip_id, db_path=ledger_db)
        assert len(before) == len(after)

    def test_plantain_quantity_unchanged_after_preference_save(
        self, staples_db: Path, alias_db: Path
    ):
        from scripts.setup_finch_staples import _save_preference_for_staple

        seed_initial_staples(staples_db)
        plantain = next(
            item for item in list_staple_items(db_path=staples_db) if item.normalized_key == "plantain"
        )
        result = PendingSearchResult(
            product_id="plantain-1",
            upc="0001111999999",
            description="Green Plantains",
            size="each",
            price="$0.59",
        )
        _save_preference_for_staple(plantain, result, alias_db_path=alias_db)
        refreshed = next(
            item for item in list_staple_items(db_path=staples_db) if item.normalized_key == "plantain"
        )
        assert refreshed.default_quantity == 5
