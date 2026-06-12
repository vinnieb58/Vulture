"""
Unit tests for Finch grocery parsing, alias matching, preview, setup, search, and Kroger client.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finch.aliases import (
    ensure_seeded,
    find_alias_matches,
    get_alias,
    get_all_aliases,
    init_db,
    seed_aliases_from_yaml,
    upsert_alias,
)
from finch.env_check import CheckStatus, format_check_line, run_env_checks, search_ready
from finch.kroger_client import (
    KrogerCartDisabledError,
    KrogerClient,
    KrogerOAuthConfig,
    KrogerProduct,
    load_kroger_client_from_env,
)
from finch.models import AliasEntry, MatchStatus
from finch.parser import parse_grocery_text
from finch.preview import build_preview, format_preview_line, main as preview_main, resolve_intent
from finch.search import (
    confirm_save,
    format_product_line,
    product_to_alias,
    run_search,
    save_alias_from_product,
)
from finch.setup import print_setup_report


MESSY_GROCERY_TEXT = """
# weekly staples
- 2 eggs
* 1 gal milk
3x coffee pods

instant coffee
2 lb flank steak

organic spinach   # not in aliases
"""


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict, url: str = "https://api.kroger.com/v1/products"):
        self.status_code = status_code
        self._payload = payload
        self.content = b"{}"
        self.url = url

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise RuntimeError("no fake responses left")
        return self._responses.pop(0)


@pytest.fixture
def alias_db(tmp_path: Path) -> Path:
    db = tmp_path / "aliases.db"
    yaml_src = Path(__file__).resolve().parent.parent / "finch" / "data" / "default_aliases.yaml"
    init_db(db)
    seed_aliases_from_yaml(yaml_src, db, overwrite=True)
    return db


class TestParser:
    def test_parse_comma_separated(self):
        intents = parse_grocery_text("eggs, milk, coffee pods")
        names = [i.normalized_name for i in intents]
        assert names == ["eggs", "milk", "coffee pods"]

    def test_parse_multiline_messy_text(self):
        intents = parse_grocery_text(MESSY_GROCERY_TEXT)
        by_name = {i.normalized_name: i for i in intents}
        assert by_name["eggs"].quantity == 2.0
        assert by_name["milk"].quantity == 1.0
        assert by_name["milk"].unit == "gal"
        assert by_name["coffee pods"].quantity == 3.0
        assert "organic spinach" in by_name

    def test_parse_fractional_quantity(self):
        intents = parse_grocery_text("1/2 lb flank steak")
        assert len(intents) == 1
        assert intents[0].quantity == 0.5
        assert intents[0].unit == "lb"

    def test_parse_empty_returns_empty(self):
        assert parse_grocery_text("") == []
        assert parse_grocery_text("   \n  ") == []


class TestAliases:
    def test_seed_and_exact_match(self, alias_db: Path):
        matches = find_alias_matches("eggs", alias_db)
        assert len(matches) == 1
        assert matches[0].display_name.startswith("Kroger")

    def test_partial_match_is_ambiguous_candidate(self, alias_db: Path):
        matches = find_alias_matches("coffee", alias_db)
        assert len(matches) >= 2

    def test_unknown_item_no_match(self, alias_db: Path):
        assert find_alias_matches("quinoa", alias_db) == []

    def test_ensure_seeded_on_empty_db(self, tmp_path: Path):
        db = tmp_path / "fresh.db"
        ensure_seeded(db)
        assert len(get_all_aliases(db)) >= 5


class TestPreview:
    def test_exact_default_for_eggs(self, alias_db: Path):
        lines = build_preview("eggs", db_path=alias_db)
        assert len(lines) == 1
        assert lines[0].status == MatchStatus.EXACT_DEFAULT
        assert lines[0].upc == "0001111081708"
        assert lines[0].matched_alias is not None

    def test_needs_search_for_coffee_pods(self, alias_db: Path):
        lines = build_preview("coffee pods", db_path=alias_db)
        assert lines[0].status == MatchStatus.NEEDS_SEARCH
        assert lines[0].upc is None
        assert lines[0].search_term

    def test_missing_for_unknown(self, alias_db: Path):
        lines = build_preview("quinoa", db_path=alias_db)
        assert lines[0].status == MatchStatus.MISSING
        assert lines[0].matched_alias is None

    def test_ambiguous_for_partial_coffee(self, alias_db: Path):
        line = resolve_intent(parse_grocery_text("coffee")[0], db_path=alias_db)
        assert line.status == MatchStatus.AMBIGUOUS

    def test_quantity_preserved(self, alias_db: Path):
        lines = build_preview("2 eggs, 3x milk", db_path=alias_db)
        qty = {l.normalized_name: l.quantity for l in lines}
        assert qty["eggs"] == 2.0
        assert qty["milk"] == 3.0

    def test_format_preview_line(self, alias_db: Path):
        line = build_preview("eggs", db_path=alias_db)[0]
        text = format_preview_line(line)
        assert "exact_default" in text
        assert "eggs" in text

    def test_cli_json_output(self, alias_db: Path, capsys):
        with patch("finch.preview.ensure_seeded"), patch(
            "finch.preview.build_preview",
            return_value=build_preview("eggs", db_path=alias_db),
        ):
            rc = preview_main(["eggs", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data[0]["status"] == "exact_default"


class TestKrogerClient:
    def _client(self, session: _FakeSession, user_token: str | None = None) -> KrogerClient:
        oauth = KrogerOAuthConfig(
            client_id="cid",
            client_secret="secret",
            location_id="01400441",
        )
        return KrogerClient(oauth, session=session, user_access_token=user_token)

    def test_client_credentials_token(self):
        session = _FakeSession(
            [_FakeResponse(200, {"access_token": "client-tok"}, url="https://api.kroger.com/v1/connect/oauth2/token")]
        )
        client = self._client(session)
        token = client.fetch_client_credentials_token()
        assert token == "client-tok"
        assert "Authorization" in session.calls[0]["headers"]
        assert "secret" not in str(session.calls)

    def test_search_products(self):
        session = _FakeSession(
            [
                _FakeResponse(200, {"access_token": "client-tok"}, url="https://api.kroger.com/v1/connect/oauth2/token"),
                _FakeResponse(
                    200,
                    {
                        "data": [
                            {
                                "productId": "123",
                                "upc": "0001111081708",
                                "description": "Kroger Large Eggs",
                                "brand": "Kroger",
                                "items": [{"price": {"regular": 2.99}, "size": "12 ct", "upc": "0001111081708"}],
                            }
                        ]
                    },
                ),
            ]
        )
        client = self._client(session)
        products = client.search_products("eggs")
        assert len(products) == 1
        assert products[0].product_id == "123"
        assert products[0].price == "2.99"
        assert products[0].size == "12 ct"

    def test_format_price(self):
        p = KrogerProduct("1", "upc", "Eggs", price="3.5")
        assert p.format_price() == "$3.50"

    def test_add_to_cart_blocked_by_default(self):
        session = _FakeSession([])
        client = self._client(session, user_token="user-tok")
        with pytest.raises(KrogerCartDisabledError):
            client.add_to_cart("0001111081708", quantity=1)

    def test_add_to_cart_live_with_user_token(self):
        session = _FakeSession([_FakeResponse(200, {"status": "ok"}, url="https://api.kroger.com/v1/cart/add")])
        client = self._client(session, user_token="user-tok")
        result = client.add_to_cart("0001111081708", quantity=2, live=True)
        assert result["status"] == "ok"
        call = session.calls[0]
        assert call["method"] == "PUT"
        assert call["json"]["items"][0]["quantity"] == 2
        auth = call["headers"]["Authorization"]
        assert auth.startswith("Bearer ")
        assert "user-tok" in auth

    def test_load_from_env_missing_raises(self, monkeypatch):
        monkeypatch.delenv("FINCH_KROGER_CLIENT_ID", raising=False)
        monkeypatch.delenv("FINCH_KROGER_CLIENT_SECRET", raising=False)
        with pytest.raises(Exception):
            load_kroger_client_from_env()

    def test_authorize_url_requires_redirect(self):
        oauth = KrogerOAuthConfig(client_id="cid", client_secret="secret")
        client = KrogerClient(oauth, session=MagicMock())
        with pytest.raises(Exception):
            client.build_authorize_url(state="xyz")


class TestEnvCheck:
    def test_search_not_ready_without_credentials(self, monkeypatch):
        monkeypatch.delenv("FINCH_KROGER_CLIENT_ID", raising=False)
        monkeypatch.delenv("FINCH_KROGER_CLIENT_SECRET", raising=False)
        checks = run_env_checks()
        assert not search_ready(checks)
        missing = [c for c in checks if c.status == CheckStatus.MISSING]
        assert any(c.name == "FINCH_KROGER_CLIENT_ID" for c in missing)

    def test_live_cart_off_by_default(self, monkeypatch):
        monkeypatch.delenv("FINCH_LIVE_CART", raising=False)
        checks = run_env_checks()
        cart = next(c for c in checks if c.name == "FINCH_LIVE_CART")
        assert cart.status == CheckStatus.OK
        assert "off" in cart.message

    def test_format_check_line_no_secret(self):
        check = run_env_checks()[0]
        line = format_check_line(check)
        assert "secret" not in line.lower() or "not set" in line.lower()


class TestSetup:
    def test_setup_report_missing_credentials(self, monkeypatch, capsys):
        monkeypatch.delenv("FINCH_KROGER_CLIENT_ID", raising=False)
        monkeypatch.delenv("FINCH_KROGER_CLIENT_SECRET", raising=False)
        rc = print_setup_report()
        out = capsys.readouterr().out
        assert "Finch setup check" in out
        assert "FINCH_KROGER_CLIENT_ID" in out
        assert rc == 1

    def test_setup_does_not_print_secret_values(self, monkeypatch, capsys):
        monkeypatch.setenv("FINCH_KROGER_CLIENT_SECRET", "super-secret-value-12345")
        monkeypatch.setenv("FINCH_KROGER_CLIENT_ID", "my-client-id")
        print_setup_report()
        out = capsys.readouterr().out
        assert "super-secret-value-12345" not in out
        assert "my-client-id" not in out


class TestSearch:
    def _sample_products(self) -> list[KrogerProduct]:
        return [
            KrogerProduct(
                product_id="001",
                upc="0001111081708",
                description="Kroger Grade A Large Eggs 12 ct",
                brand="Kroger",
                size="12 ct",
                price="2.99",
            ),
            KrogerProduct(
                product_id="002",
                upc="0001111081709",
                description="Simple Truth Organic Eggs 12 ct",
                brand="Simple Truth",
                size="12 ct",
                price="4.29",
            ),
        ]

    def test_format_product_line(self):
        line = format_product_line(1, self._sample_products()[0])
        assert "[1]" in line
        assert "Kroger Grade A Large Eggs" in line
        assert "0001111081708" in line
        assert "$2.99" in line
        assert "12 ct" in line

    def test_run_search_mocked(self):
        session = _FakeSession(
            [
                _FakeResponse(200, {"access_token": "tok"}, url="https://api.kroger.com/v1/connect/oauth2/token"),
                _FakeResponse(
                    200,
                    {
                        "data": [
                            {
                                "productId": "001",
                                "upc": "0001111081708",
                                "description": "Kroger Large Eggs",
                                "brand": "Kroger",
                                "items": [{"size": "12 ct", "price": {"regular": 2.99}}],
                            }
                        ]
                    },
                ),
            ]
        )
        oauth = KrogerOAuthConfig(client_id="c", client_secret="s", location_id="01400441")
        client = KrogerClient(oauth, session=session)
        products = run_search("eggs", client=client)
        assert len(products) == 1
        assert products[0].size == "12 ct"

    def test_product_to_alias(self):
        product = self._sample_products()[0]
        entry = product_to_alias("eggs", product, search_term="eggs")
        assert entry.alias_key == "eggs"
        assert entry.upc == "0001111081708"
        assert entry.kroger_product_id == "001"

    def test_save_alias_requires_confirmation(self, alias_db: Path):
        product = self._sample_products()[0]
        with patch("finch.search.get_alias", return_value=None):
            with patch("finch.search.upsert_alias") as mock_upsert:
                result = save_alias_from_product(
                    "eggs",
                    product,
                    "eggs",
                    db_path=alias_db,
                    confirm=False,
                    input_fn=lambda _: "n",
                )
        assert result is None
        mock_upsert.assert_not_called()

    def test_save_alias_with_confirm(self, alias_db: Path):
        product = self._sample_products()[0]
        saved = save_alias_from_product(
            "eggs",
            product,
            "eggs",
            db_path=alias_db,
            confirm=True,
        )
        assert saved is not None
        stored = get_alias("eggs", alias_db)
        assert stored is not None
        assert stored.upc == "0001111081708"

    def test_upsert_alias_replaces_existing(self, alias_db: Path):
        upsert_alias(
            AliasEntry(alias_key="eggs", display_name="Old Eggs"),
            alias_db,
        )
        upsert_alias(
            AliasEntry(
                alias_key="eggs",
                display_name="New Eggs",
                upc="999",
            ),
            alias_db,
        )
        entry = get_alias("eggs", alias_db)
        assert entry.display_name == "New Eggs"
        assert entry.upc == "999"


SAMPLE_LOCATIONS_PAYLOAD = {
    "data": [
        {
            "locationId": "01400441",
            "chain": "KROGER",
            "name": "Kroger",
            "phone": "2815551234",
            "address": {
                "addressLine1": "123 Main St",
                "city": "Richmond",
                "state": "TX",
                "zipCode": "77406",
            },
            "departments": [
                {"departmentId": "94", "name": "Pickup"},
                {"departmentId": "01", "name": "Deli"},
            ],
        },
        {
            "locationId": "01400442",
            "chain": "KROGER",
            "name": "Kroger Marketplace",
            "phone": "2815559999",
            "address": {
                "addressLine1": "456 FM 1092",
                "city": "Missouri City",
                "state": "TX",
                "zipCode": "77459",
            },
            "departments": [{"departmentId": "01", "name": "Deli"}],
        },
    ]
}


class TestLocations:
    def test_has_pickup_department(self):
        from finch.kroger_client import has_pickup_department

        assert has_pickup_department([{"departmentId": "94", "name": "Pickup"}])
        assert not has_pickup_department([{"departmentId": "01", "name": "Deli"}])
        assert not has_pickup_department(None)

    def test_parse_locations_payload(self):
        from finch.kroger_client import _parse_locations_payload

        locations = _parse_locations_payload(SAMPLE_LOCATIONS_PAYLOAD)
        assert len(locations) == 2
        assert locations[0].location_id == "01400441"
        assert locations[0].has_pickup is True
        assert locations[0].phone == "(281) 555-1234"
        assert locations[1].has_pickup is False

    def test_run_location_search_mocked(self):
        from finch.locations import run_location_search

        session = _FakeSession(
            [
                _FakeResponse(200, {"access_token": "tok"}, url="https://api.kroger.com/v1/connect/oauth2/token"),
                _FakeResponse(200, SAMPLE_LOCATIONS_PAYLOAD, url="https://api.kroger.com/v1/locations"),
            ]
        )
        oauth = KrogerOAuthConfig(client_id="c", client_secret="s")
        client = KrogerClient(oauth, session=session)
        locations = run_location_search("77406", client=client)
        assert len(locations) == 2
        assert locations[0].city_state_zip == "Richmond, TX 77406"

    def test_format_location_line_shows_pickup(self):
        from finch.kroger_client import _parse_locations_payload
        from finch.locations import format_location_line

        loc = _parse_locations_payload(SAMPLE_LOCATIONS_PAYLOAD)[0]
        line = format_location_line(1, loc)
        assert "pickup (dept 94): yes" in line
        assert "01400441" in line

    def test_save_location_config(self, monkeypatch, tmp_path: Path):
        from finch.local_config import load_finch_config, resolve_location_id, save_location_config

        monkeypatch.delenv("FINCH_KROGER_LOCATION_ID", raising=False)
        cfg_path = tmp_path / "finch_config.json"
        save_location_config(
            "01400441",
            store_name="Kroger",
            store_address="123 Main St, Richmond, TX 77406",
            saved_from_zip="77406",
            config_path=cfg_path,
        )
        data = load_finch_config(cfg_path)
        assert data["kroger_location_id"] == "01400441"
        assert data["saved_from_zip"] == "77406"
        assert resolve_location_id(cfg_path) == "01400441"

    def test_resolve_location_id_env_wins(self, monkeypatch, tmp_path: Path):
        from finch.local_config import resolve_location_id, save_location_config

        cfg_path = tmp_path / "finch_config.json"
        save_location_config("from-file", config_path=cfg_path)
        monkeypatch.setenv("FINCH_KROGER_LOCATION_ID", "from-env")
        assert resolve_location_id(cfg_path) == "from-env"

    def test_save_location_requires_confirmation(self, tmp_path: Path):
        from finch.kroger_client import _parse_locations_payload
        from finch.locations import save_selected_location

        loc = _parse_locations_payload(SAMPLE_LOCATIONS_PAYLOAD)[0]
        cfg_path = tmp_path / "finch_config.json"
        result = save_selected_location(
            loc,
            "77406",
            confirm=False,
            input_fn=lambda _: "n",
            config_path=cfg_path,
        )
        assert result is None
        assert not cfg_path.exists()

    def test_save_location_with_confirm(self, tmp_path: Path):
        from finch.kroger_client import _parse_locations_payload
        from finch.locations import save_selected_location

        loc = _parse_locations_payload(SAMPLE_LOCATIONS_PAYLOAD)[0]
        cfg_path = tmp_path / "finch_config.json"
        result = save_selected_location(
            loc,
            "77406",
            confirm=True,
            config_path=cfg_path,
        )
        assert result is not None
        assert result["kroger_location_id"] == "01400441"


class TestSetupLocationWarning:
    def test_setup_shows_locations_hint_when_missing(self, monkeypatch, capsys):
        monkeypatch.setenv("FINCH_KROGER_CLIENT_ID", "cid")
        monkeypatch.setenv("FINCH_KROGER_CLIENT_SECRET", "secret")
        monkeypatch.delenv("FINCH_KROGER_LOCATION_ID", raising=False)
        with patch("finch.env_check.resolve_location_id", return_value=None):
            print_setup_report()
        out = capsys.readouterr().out
        assert "finch.locations" in out
        assert "--save" in out

    def test_env_check_location_missing_message(self, monkeypatch):
        monkeypatch.delenv("FINCH_KROGER_LOCATION_ID", raising=False)
        with patch("finch.env_check.resolve_location_id", return_value=None):
            checks = run_env_checks()
        loc = next(c for c in checks if c.name == "FINCH_KROGER_LOCATION_ID")
        assert loc.status == CheckStatus.MISSING
        assert "finch.locations" in loc.message


class TestTokenStore:
    def test_save_tokens_sets_mode_600(self, tmp_path: Path):
        from finch.token_store import load_tokens, save_tokens_from_response

        token_path = tmp_path / "finch_tokens.json"
        with patch("finch.token_store.os.chmod") as mock_chmod:
            save_tokens_from_response(
                {
                    "access_token": "access-abc",
                    "refresh_token": "refresh-xyz",
                    "expires_in": 1800,
                    "token_type": "bearer",
                },
                tokens_path=token_path,
            )
            mock_chmod.assert_called_once_with(token_path, 0o600)

        stored = load_tokens(token_path)
        assert stored is not None
        assert stored.access_token == "access-abc"
        assert stored.refresh_token == "refresh-xyz"

    def test_token_expiry_detection(self):
        from datetime import datetime, timedelta, timezone

        from finch.token_store import StoredTokens

        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        tokens = StoredTokens(access_token="a", expires_in=3600, saved_at=old)
        assert tokens.is_expired()

    def test_resolve_user_access_token_prefers_env(self, monkeypatch, tmp_path: Path):
        from finch.token_store import resolve_user_access_token, save_tokens_from_response

        token_path = tmp_path / "finch_tokens.json"
        save_tokens_from_response({"access_token": "from-file"}, tokens_path=token_path)
        monkeypatch.setenv("FINCH_KROGER_USER_ACCESS_TOKEN", "from-env")
        assert resolve_user_access_token(token_path) == "from-env"


class TestAuth:
    def test_auth_exchange_and_save(self, monkeypatch, tmp_path: Path, capsys):
        from finch.auth import run_auth_flow

        monkeypatch.setenv("FINCH_KROGER_CLIENT_ID", "cid")
        monkeypatch.setenv("FINCH_KROGER_CLIENT_SECRET", "secret")
        monkeypatch.setenv("FINCH_KROGER_REDIRECT_URI", "http://localhost:8765/callback")

        mock_client = MagicMock()
        mock_client.oauth.redirect_uri = "http://localhost:8765/callback"
        mock_client.build_authorize_url.return_value = "https://api.kroger.com/v1/connect/oauth2/authorize?test=1"
        mock_client.exchange_authorization_code_full.return_value = {
            "access_token": "user-access-token-secret",
            "refresh_token": "user-refresh-token-secret",
            "expires_in": 1800,
        }

        with patch("finch.auth.load_kroger_client_from_env", return_value=mock_client):
            with patch("finch.auth.FINCH_TOKENS_PATH", tmp_path / "finch_tokens.json"):
                with patch("finch.auth.save_tokens_from_response") as mock_save:
                    rc = run_auth_flow(input_fn=lambda _: "auth-code-123")

        assert rc == 0
        mock_client.exchange_authorization_code_full.assert_called_once_with("auth-code-123")
        mock_save.assert_called_once()
        out = capsys.readouterr().out
        assert "authorize" in out.lower() or "Open this URL" in out
        assert "user-access-token-secret" not in out
        assert "user-refresh-token-secret" not in out

    def test_refresh_user_token_mocked(self):
        session = _FakeSession(
            [
                _FakeResponse(
                    200,
                    {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 1800},
                    url="https://api.kroger.com/v1/connect/oauth2/token",
                )
            ]
        )
        oauth = KrogerOAuthConfig(
            client_id="c",
            client_secret="s",
            redirect_uri="http://localhost/cb",
        )
        client = KrogerClient(oauth, session=session)
        payload = client.refresh_user_token("old-refresh")
        assert payload["access_token"] == "new-access"
        assert session.calls[0]["data"]["grant_type"] == "refresh_token"
        assert "secret" not in str(session.calls[0].get("headers", {}))


class TestCart:
    def test_resolve_cart_item(self, alias_db: Path):
        from finch.cart_ops import resolve_cart_item

        attempt = resolve_cart_item("eggs", db_path=alias_db)
        assert attempt.upc == "0001111081708"
        assert attempt.quantity == 1

    def test_cart_guard_live_cart_off(self, monkeypatch, alias_db: Path):
        from finch.cart_ops import CartGuardError, execute_cart_add, resolve_cart_item

        monkeypatch.delenv("FINCH_LIVE_CART", raising=False)
        attempt = resolve_cart_item("eggs", db_path=alias_db)
        client = MagicMock()
        with pytest.raises(CartGuardError):
            execute_cart_add(attempt, client)

    def test_cart_guard_no_token(self, monkeypatch):
        from finch.cart_ops import CartGuardError, require_saved_token

        monkeypatch.delenv("FINCH_KROGER_USER_ACCESS_TOKEN", raising=False)
        with patch("finch.cart_ops.resolve_user_access_token", return_value=None):
            with pytest.raises(CartGuardError):
                require_saved_token()

    def test_cart_add_request_mocked(self, monkeypatch, alias_db: Path):
        from finch.cart_ops import execute_cart_add, resolve_cart_item

        monkeypatch.setenv("FINCH_LIVE_CART", "true")
        attempt = resolve_cart_item("eggs", db_path=alias_db)
        session = _FakeSession(
            [_FakeResponse(200, {"status": "ok"}, url="https://api.kroger.com/v1/cart/add")]
        )
        oauth = KrogerOAuthConfig(client_id="c", client_secret="s")
        client = KrogerClient(oauth, session=session, user_access_token="user-tok")
        execute_cart_add(attempt, client)
        call = session.calls[0]
        assert call["method"] == "PUT"
        assert call["json"]["items"][0]["upc"] == "0001111081708"
        assert call["json"]["items"][0]["quantity"] == 1
        auth = call["headers"]["Authorization"]
        assert auth.startswith("Bearer ")
        assert "user-tok" in auth

    def test_ensure_fresh_user_token_refreshes_when_expired(self, tmp_path: Path):
        from datetime import datetime, timedelta, timezone

        from finch.cart_ops import ensure_fresh_user_token
        from finch.token_store import save_tokens_from_response

        token_path = tmp_path / "finch_tokens.json"
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        save_tokens_from_response(
            {
                "access_token": "expired-access",
                "refresh_token": "keep-refresh",
                "expires_in": 3600,
                "saved_at": old_time,
            },
            tokens_path=token_path,
        )
        # overwrite saved_at since save_tokens_from_response sets now
        import json

        data = json.loads(token_path.read_text())
        data["saved_at"] = old_time
        token_path.write_text(json.dumps(data))

        session = _FakeSession(
            [
                _FakeResponse(
                    200,
                    {"access_token": "fresh-access", "expires_in": 1800},
                    url="https://api.kroger.com/v1/connect/oauth2/token",
                )
            ]
        )
        oauth = KrogerOAuthConfig(client_id="c", client_secret="s")
        client = KrogerClient(oauth, session=session)
        token = ensure_fresh_user_token(client, tokens_path=token_path)
        assert token == "fresh-access"

    def test_cart_test_validation_only_when_live_cart_off(self, monkeypatch, alias_db: Path, capsys):
        from finch.cart.__main__ import cmd_test

        monkeypatch.delenv("FINCH_LIVE_CART", raising=False)
        with patch("finch.cart.__main__.pick_test_alias", return_value="eggs"):
            with patch("finch.cart.__main__.resolve_cart_item") as mock_resolve:
                        from finch.cart_ops import CartAttempt
                        from finch.models import MatchStatus

                        mock_resolve.return_value = CartAttempt(
                            requested_item="eggs",
                            normalized_name="eggs",
                            alias_name="Kroger Eggs",
                            upc="0001111081708",
                            product_id=None,
                            quantity=1,
                            modality="pickup",
                            status=MatchStatus.EXACT_DEFAULT,
                        )
                        rc = cmd_test()
        assert rc == 0
        out = capsys.readouterr().out
        assert "validation ok" in out
        assert "FINCH_LIVE_CART is off" in out
