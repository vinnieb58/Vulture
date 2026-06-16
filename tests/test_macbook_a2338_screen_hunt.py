"""Regression tests for the macbook_a2338_screen manual hunt spec."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.hunt_service import hunt_to_execution_dict
from engine.manual_hunt_specs import MACBOOK_A2338_SCREEN_NAME, MACBOOK_A2338_SCREEN_SPEC
from engine.rules import rejection_reason
from engine.source_selection import resolve_source_sites
from models.hunt import Hunt
from models.listing import Listing


def _hunt_from_spec() -> Hunt:
    return Hunt(
        hunt_id="test-macbook-a2338",
        status="active",
        **MACBOOK_A2338_SCREEN_SPEC,
    )


def _listing(title: str, price: int | None = 150) -> Listing:
    return Listing("craigslist", title, price, "Houston", "http://example.com/x")


class TestMacbookA2338ScreenSpec:
    def test_spec_identity(self):
        assert MACBOOK_A2338_SCREEN_SPEC["name"] == MACBOOK_A2338_SCREEN_NAME
        assert MACBOOK_A2338_SCREEN_SPEC["category"] == "electronics"
        assert MACBOOK_A2338_SCREEN_SPEC["max_price"] == 250
        assert MACBOOK_A2338_SCREEN_SPEC["location"] == "houston"
        assert MACBOOK_A2338_SCREEN_SPEC["radius"] == 50

    def test_electronics_source_fanout(self):
        assert MACBOOK_A2338_SCREEN_SPEC["source_sites"] == resolve_source_sites("electronics")

    def test_execution_dict_round_trip(self):
        d = hunt_to_execution_dict(_hunt_from_spec())
        assert d["name"] == MACBOOK_A2338_SCREEN_NAME
        assert d["city"] == "houston"
        assert d["rules"]["max_price"] == 250
        assert d["rules"]["vertical"] == "electronics"
        assert d["adapter_options"]["display_name"] == "MacBook Pro A2338 M1 Screen"
        assert d["adapter_options"]["priority"] == "medium"
        assert len(d["source_sites"]) >= 4

    def test_accepts_display_assembly_listing(self):
        rules = hunt_to_execution_dict(_hunt_from_spec())["rules"]
        title = "MacBook Pro A2338 display assembly 13 inch M1 2020"
        assert rejection_reason(_listing(title, 140), rules) is None

    def test_accepts_donor_laptop_for_parts(self):
        rules = hunt_to_execution_dict(_hunt_from_spec())["rules"]
        title = "MacBook Pro M1 for parts - won't power on A2338"
        assert rejection_reason(_listing(title, 175), rules) is None

    def test_rejects_accessory_junk(self):
        rules = hunt_to_execution_dict(_hunt_from_spec())["rules"]
        assert rejection_reason(_listing("MacBook Pro A2338 case cover shell"), rules) is not None
        assert rejection_reason(_listing("MacBook Pro M1 screen protector"), rules) is not None
        assert rejection_reason(_listing("MacBook Pro A2338 charger adapter"), rules) is not None

    def test_rejects_over_max_price(self):
        rules = hunt_to_execution_dict(_hunt_from_spec())["rules"]
        title = "MacBook Pro A2338 display assembly"
        assert rejection_reason(_listing(title, 325), rules) is not None

    def test_pricing_guidance_metadata(self):
        guidance = MACBOOK_A2338_SCREEN_SPEC["adapter_options"]["pricing_guidance"]
        assert guidance["great"] == [100, 150]
        assert guidance["good"] == [150, 225]
        assert guidance["maybe"] == [225, 275]
        assert guidance["skip_above"] == 300
