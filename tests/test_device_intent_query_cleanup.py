"""
Regression tests for Mac Mini device intent, query cleanup, Steam Deck
accessory filtering, and M.2 SATA storage hunts (2026-06-02 live log fixes).
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


def _listing(title: str, price: int | None = 200, source: str = "craigslist") -> Listing:
    return Listing(source, title, price, "Houston", "http://example.com/x")


def _rules_from_intent(intent: str) -> dict:
    t = translate(intent)
    rules: dict = {}
    ao = t.adapter_options or {}
    _vertical = (t.category or "").replace(" ", "_").strip("_")
    if _vertical:
        rules["vertical"] = _vertical
    if ao.get("min_price") is not None:
        rules["min_price"] = int(ao["min_price"])
    if t.max_price is not None:
        rules["max_price"] = t.max_price
    if t.include_keywords:
        rules["include_keywords"] = t.include_keywords
    if t.exclude_keywords:
        rules["exclude_keywords"] = t.exclude_keywords
    if ao.get("require_all_keywords"):
        rules["require_all_keywords"] = list(ao["require_all_keywords"])
    if ao.get("hunt_subtype"):
        rules["hunt_subtype"] = ao["hunt_subtype"]
    if ao.get("product_family"):
        rules["product_family"] = ao["product_family"]
    if ao.get("target_product_type"):
        rules["target_product_type"] = ao["target_product_type"]
    if ao.get("brand"):
        rules["brand"] = ao["brand"]
    if ao.get("storage_form_factor"):
        rules["storage_form_factor"] = ao["storage_form_factor"]
    if ao.get("storage_protocol"):
        rules["storage_protocol"] = ao["storage_protocol"]
    if ao.get("excluded_storage_protocols"):
        rules["excluded_storage_protocols"] = list(ao["excluded_storage_protocols"])
    if ao.get("allowed_capacity_gb"):
        rules["allowed_capacity_gb"] = list(ao["allowed_capacity_gb"])
    if ao.get("reject_bulk_lots"):
        rules["reject_bulk_lots"] = ao["reject_bulk_lots"]
    return rules


def _passes(title: str, rules: dict, *, price: int | None = 200) -> bool:
    return rejection_reason(_listing(title, price), rules) is None


def _fails(title: str, rules: dict, *, price: int | None = 200) -> bool:
    return rejection_reason(_listing(title, price), rules) is not None


class TestMacMiniQueryCleanup:
    @pytest.mark.parametrize(
        "intent,expected",
        [
            ("mac mini for under 50", "Mac Mini"),
            ("mac mini under 50", "Mac Mini"),
            ("apple mac mini less than 100", "Apple Mac Mini"),
            ("used mac mini under 100", "Mac Mini"),
            ("m4 mac mini under 500", "Apple Mac Mini"),
        ],
    )
    def test_search_terms_no_trailing_for(self, intent: str, expected: str):
        t = translate(intent)
        assert t.search_terms == [expected]
        joined = " ".join(t.search_terms).lower()
        assert not joined.endswith(" for")
        assert " for" not in joined or joined == expected.lower()

    def test_never_generates_mac_mini_for(self):
        for intent in (
            "mac mini for",
            "mac mini for under 50",
            "mac mini for home office under 400",
        ):
            t = translate(intent)
            assert "Mac Mini For" not in t.search_terms
            assert t.search_terms[0] in ("Mac Mini", "Apple Mac Mini")


class TestMacMiniTranslation:
    def test_adapter_options(self):
        t = translate("used mac mini under 100")
        ao = t.adapter_options
        assert ao["product_family"] == "apple_mac_mini"
        assert ao["target_product_type"] == "device"
        assert ao["brand"] == "apple"
        assert ao["hunt_subtype"] == "device"
        assert t.vertical == "laptops_computers"

    def test_include_keywords_require_mac_mini_phrase(self):
        t = translate("mac mini under 50")
        incl = {k.lower() for k in t.include_keywords}
        assert "mac mini" in incl
        assert "macmini" in incl


class TestMacMiniListingRules:
    @pytest.fixture
    def rules(self):
        return _rules_from_intent("mac mini")

    def test_reject_mac_cosmetics_brush(self, rules):
        assert _fails("MAC 270s Mini Rounded Slant Brush New in Sleeve", rules)

    def test_reject_mac_mini_lipstick(self, rules):
        assert _fails("MAC Mini Lipstick set", rules)

    def test_reject_mac_cosmetics_crossbody_bag(self, rules):
        assert _fails(
            "MAC COSMETICS Clear crossbody bag + Stone Lipstick + mini",
            rules,
        )

    def test_reject_mac_mini_dock(self, rules):
        assert _fails("Mac mini M4 Dock", rules)

    def test_reject_stand_hub_enclosure(self, rules):
        assert _fails(
            "Stand & Hub with M.2 SSD Enclosure for Apple Mac Mini M4",
            rules,
        )

    def test_allow_late_2014_mac_mini(self, rules):
        assert _passes("Apple Mac Mini Late 2014 I5 8GB 250GB SSD", rules)

    def test_allow_late_2024_desktop(self, rules):
        assert _passes(
            "Mac mini MCX44LL/A (Late 2024) Desktop Computer",
            rules,
        )

    def test_allow_bundle_with_accessories(self, rules):
        assert _passes(
            "Apple Mac Mini M2 8GB 256GB with keyboard and mouse",
            rules,
        )


class TestSteamDeckAccessoryFiltering:
    @pytest.fixture
    def rules(self):
        return _rules_from_intent("steam deck under 400")

    def test_reject_gripcase_accessory(self, rules):
        assert _fails("Skull & Co GripCase for Steam Deck", rules)

    def test_allow_console_bundle_with_case(self, rules):
        assert _passes("Steam Deck LCD 512GB with carrying case", rules)

    def test_product_family_set(self):
        t = translate("steam deck under 400")
        assert t.adapter_options.get("product_family") == "steam_deck"


class TestM2SataStorageHunt:
    @pytest.fixture
    def rules(self):
        return _rules_from_intent("M.2 SATA SSD 256GB or 512GB under $50")

    def test_translation_subtype_and_protocol(self):
        t = translate("M.2 SATA SSD 256GB or 512GB under $50")
        ao = t.adapter_options
        assert ao["hunt_subtype"] == "storage"
        assert ao["storage_form_factor"] == "m2"
        assert ao["storage_protocol"] == "sata"
        assert ao["excluded_storage_protocols"] == ["nvme"]
        assert set(ao["allowed_capacity_gb"]) == {256, 512}
        assert ao["reject_bulk_lots"] is True

    def test_reject_nvme(self, rules):
        assert _fails("Samsung 970 EVO Plus 500GB NVMe M.2 SSD", rules)

    def test_reject_25_inch(self, rules):
        assert _fails("Samsung 860 EVO 500GB 2.5 inch SATA SSD", rules)

    def test_reject_bulk_lot(self, rules):
        assert _fails("Bulk lot of 10 M.2 SATA SSD 256GB mixed", rules)

    def test_reject_wrong_capacity(self, rules):
        assert _fails("Crucial MX500 1TB M.2 SATA SSD", rules)

    def test_allow_valid_m2_sata(self, rules):
        assert _passes("Crucial MX500 512GB M.2 SATA SSD", rules, price=40)


class TestStorageSourceSelection:
    _COMPUTER_PARTS_SOURCES = [
        "craigslist",
        "mercari",
        "offerup",
        "microcenter",
        "swappa",
        "bestbuy",
        "newegg",
    ]

    def test_m2_storage_matches_ddr4_source_expansion(self):
        ddr4 = translate("32GB DDR4 RAM 3200mhz")
        m2 = translate("M.2 SATA SSD 256GB under $50")
        assert ddr4.source_sites == self._COMPUTER_PARTS_SOURCES
        assert m2.source_sites == self._COMPUTER_PARTS_SOURCES

    def test_m2_storage_vertical_is_computer_parts(self):
        t = translate("M.2 SATA SSD 256GB under $50")
        assert t.vertical == "computer_parts"
