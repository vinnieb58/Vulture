"""
Unit tests for Finch grocery parsing, alias matching, preview, and Kroger client.
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
    get_all_aliases,
    init_db,
    seed_aliases_from_yaml,
)
from finch.kroger_client import (
    KrogerCartDisabledError,
    KrogerClient,
    KrogerOAuthConfig,
    load_kroger_client_from_env,
)
from finch.models import MatchStatus
from finch.parser import parse_grocery_text
from finch.preview import build_preview, format_preview_line, main, resolve_intent


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
            rc = main(["eggs", "--json"])
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
                                "items": [{"price": {"regular": 2.99}}],
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
