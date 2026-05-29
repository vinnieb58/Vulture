"""
tests/test_search_filter_quality.py

Focused regression tests for search/filter quality improvements.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("VULTURE_TRANSLATOR", "rules")

import pytest

from adapters.registry import get_capabilities
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
    if ao.get("max_miles") is not None:
        rules["max_miles"] = int(ao["max_miles"])
    if ao.get("min_year") is not None:
        rules["min_year"] = int(ao["min_year"])
    if ao.get("min_gpu_class"):
        rules["min_gpu_class"] = ao["min_gpu_class"]
    if ao.get("make"):
        rules["vehicle_make"] = ao["make"]
    if ao.get("model"):
        rules["vehicle_model"] = ao["model"]
    if ao.get("hunt_subtype"):
        rules["hunt_subtype"] = ao["hunt_subtype"]
    return rules


def _passes(title: str, rules: dict, *, price: int | None = 200) -> bool:
    return rejection_reason(_listing(title, price), rules) is None


def _fails(title: str, rules: dict, *, price: int | None = 200) -> bool:
    return rejection_reason(_listing(title, price), rules) is not None


class TestMissingPriceLogging:
    def test_missing_price_with_max_price_required_message(self):
        rules = {"max_price": 500}
        reason = rejection_reason(_listing("Some item", None), rules)
        assert reason is not None
        assert "missing price while max_price is required" in reason
        assert "n/a" not in reason

    def test_missing_price_still_rejects(self):
        rules = {"max_price": 500}
        assert _fails("RTX 3080 GPU", rules, price=None)

    def test_priced_listing_over_max_still_logged_with_amount(self):
        rules = {"max_price": 100}
        reason = rejection_reason(_listing("Item", 150), rules)
        assert reason is not None
        assert "$150 > max_price $100" in reason


class TestVehicleModelOnlyMatching:
    @pytest.fixture
    def rules(self):
        return _rules_from_intent("Find a Toyota Sequoia 2016 or newer under 150k miles")

    def test_sequoia_without_toyota_passes(self, rules):
        assert _passes("SEQUOIA 2018 LIMITED", rules, price=25000)


    def test_sequoia_2008_title_relevance_without_year_rule(self):
        rules = {
            "vertical": "vehicles",
            "vehicle_make": "toyota",
            "vehicle_model": "sequoia",
            "include_keywords": ["toyota sequoia"],
        }
        assert _passes("SEQUOIA 2008 LIMITED", rules, price=25000)

    def test_full_make_model_still_passes(self, rules):
        assert _passes("2019 Toyota Sequoia SR5 65k miles", rules, price=32000)

    def test_mixed_models_rejects(self, rules):
        assert _fails("Toyota Tacoma 4runner Tundra and sequoia", rules, price=25000)

    def test_roof_rack_parts_rejects(self, rules):
        assert _fails("Toyota Sequoia roof rack OEM", rules, price=120)

    def test_part_out_rejects(self, rules):
        assert _fails("Toyota Sequoia part out 2018", rules, price=800)


class TestGpuStandaloneVsSystem:
    @pytest.fixture
    def rules(self):
        return _rules_from_intent("rtx 3080 under 400")

    def test_standalone_gpu_passes(self, rules):
        assert _passes(
            "EVGA NVIDIA GeForce RTX 3080 XC3 ULTRA 10GB LHR GPU Graphics",
            rules,
        )

    def test_ftw3_gpu_passes(self, rules):
        assert _passes("Rtx 3080 FTW3 10GB GPU", rules)

    def test_gaming_pc_rejects(self, rules):
        assert _fails("Gaming PC RTX 3080 i9 32GB RAM Tower", rules)

    def test_laptop_rejects(self, rules):
        assert _fails("ASUS TUF Gaming Laptop RTX 3080 16GB", rules)

    def test_we_buy_spam_rejects(self, rules):
        assert _fails("WE BUY RTX 3080 CASH FOR GPUS", rules)


class TestRamKitVsWholeComputer:
    @pytest.fixture
    def rules(self):
        return _rules_from_intent("Find 32GB DDR4 RAM 3200mhz or faster")

    def test_ram_kit_passes(self, rules):
        assert _passes(
            "32gb Kit: 2x G.SKILL Ripjaws V Series 16GB 288-Pin PC RAM DDR4",
            rules,
        )

    def test_corsair_kit_passes(self, rules):
        assert _passes("CORSAIR VENGEANCE LPX DDR4 RAM 16GB (2x8GB)", rules)

    def test_mini_pc_rejects(self, rules):
        assert _fails("HP Mini PC 32GB DDR4 Desktop Computer", rules)

    def test_optiplex_rejects(self, rules):
        assert _fails("Dell Optiplex 7040 32GB DDR4", rules)

    def test_wtb_rejects(self, rules):
        assert _fails("WTB DDR4 RAM 32GB", rules)


class TestSteamDeckNoiseRejection:
    @pytest.fixture
    def rules(self):
        return _rules_from_intent("steam deck under 400")

    def test_steam_deck_passes(self, rules):
        assert _passes("Valve Steam Deck OLED 512GB", rules)

    def test_steamdeck_one_word_passes(self, rules):
        assert _passes("Steamdeck 64GB LCD good condition", rules)

    def test_restaurant_equipment_rejects(self, rules):
        assert _fails("Commercial dough mixer restaurant equipment", rules)

    def test_food_truck_rejects(self, rules):
        assert _fails("Food truck meat slicer for sale", rules)

    def test_bakery_processing_rejects(self, rules):
        assert _fails("Bakery processing refrigeration unit", rules)


class TestAdapterRegistryMetadata:
    def test_craigslist_stable(self):
        caps = get_capabilities("craigslist")
        assert caps is not None
        assert caps["stable"] is True
        assert caps["experimental"] is False

    def test_offerup_experimental_geoip_only(self):
        caps = get_capabilities("offerup")
        assert caps is not None
        assert caps["stable"] is False
        assert caps["experimental"] is True
        assert caps["location_control"] == "geoip_only"

    def test_carsdotcom_experimental_browser_required(self):
        caps = get_capabilities("carsdotcom")
        assert caps is not None
        assert caps["stable"] is False
        assert caps["experimental"] is True
        assert caps["requires_browser"] is True
        assert caps.get("flaky") is True
