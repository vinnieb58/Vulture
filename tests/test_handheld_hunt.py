"""
Regression tests for Steam Deck / gaming handheld hunt matching.

Ensures Craigslist-style false positives (controllers, PEMF, restaurant gear)
do not pass title rules, while GPU hunts remain unchanged.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("VULTURE_TRANSLATOR", "rules")

import pytest

from engine.llm_translator import translate
from engine.rules import rejection_reason
from models.listing import Listing


def _listing(title: str, price: int = 200) -> Listing:
    return Listing("craigslist", title, price, "Houston", "http://example.com")


def _rules_from_intent(intent: str) -> dict:
    t = translate(intent)
    rules: dict = {}
    ao = t.adapter_options or {}
    _vertical = (t.category or "").replace(" ", "_").strip("_")
    if _vertical:
        rules["vertical"] = _vertical
    if t.max_price is not None:
        rules["max_price"] = t.max_price
    if t.include_keywords:
        rules["include_keywords"] = t.include_keywords
    if t.exclude_keywords:
        rules["exclude_keywords"] = t.exclude_keywords
    if ao.get("require_all_keywords"):
        rules["require_all_keywords"] = list(ao["require_all_keywords"])
    return rules


def _passes(title: str, rules: dict, *, price: int = 200) -> bool:
    return rejection_reason(_listing(title, price), rules) is None


def _fails(title: str, rules: dict, *, price: int = 200) -> bool:
    return rejection_reason(_listing(title, price), rules) is not None


class TestSteamDeckTranslation:
    def test_include_keywords_require_steam_deck_phrase(self):
        t = translate("steam deck under 400")
        incl = {k.lower() for k in t.include_keywords}
        assert "steam deck" in incl
        assert "steamdeck" in incl

    def test_search_term_is_steam_deck(self):
        t = translate("steam deck under 400")
        assert any("steam deck" in term.lower() for term in t.search_terms)

    def test_oled_search_term(self):
        t = translate("steam deck oled under 500")
        assert any("oled" in term.lower() for term in t.search_terms)


class TestSteamDeckTitleRegression:
    @pytest.fixture
    def rules(self):
        return _rules_from_intent("steam deck under 400")

    def test_pass_real_steam_deck(self, rules):
        assert _passes("Valve Steam Deck OLED 512GB", rules)

    def test_pass_steamdeck_one_word(self, rules):
        assert _passes("Steamdeck 64GB LCD good condition", rules)

    def test_fail_8bitdo_controller(self, rules):
        assert _fails("8BitDo Ultimate Bluetooth Controller", rules)

    def test_fail_pemf_machine(self, rules):
        assert _fails("PEMF Machine Professional Grade Therapy", rules)

    def test_fail_restaurant_equipment(self, rules):
        assert _fails("Commercial Restaurant Equipment Lot - stoves ovens", rules)


class TestGpuHuntUnchanged:
    def test_rtx_3080_still_uses_model_include(self):
        t = translate("rtx 3080 under 400")
        assert "3080" in {k.lower() for k in t.include_keywords}

    def test_rtx_3080_passes_gpu_listing(self):
        rules = _rules_from_intent("rtx 3080 under 400")
        assert _passes("EVGA RTX 3080 10GB XC3 Gaming", rules)

    def test_steam_deck_rules_not_applied_to_gpu_hunt(self):
        rules = _rules_from_intent("rtx 3080 under 400")
        incl = {k.lower() for k in rules.get("include_keywords", [])}
        assert "steam deck" not in incl
        assert "steamdeck" not in incl


class TestGenericHandheld:
    def test_generic_includes_strong_phrases(self):
        t = translate("gaming handheld under 300")
        incl = {k.lower() for k in t.include_keywords}
        assert "steam deck" in incl
        assert "rog ally" in incl

    def test_generic_rejects_controller_without_device(self):
        rules = _rules_from_intent("gaming handheld under 300")
        assert _fails("8BitDo Pro 2 Wireless Controller", rules)
