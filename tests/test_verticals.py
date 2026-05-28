"""
tests/test_verticals.py

Pytest tests for the vertical-aware translation and rules-matching pipeline.

Covers:
  1. engine.verticals — constants and GPU tier list integrity
  2. Translator — acceptance tests matching the four spec examples
  3. Rules engine — vertical-specific structured checks
     (TV size, GPU tier, RAM capacity/speed, vehicle year/miles)

Run from project root:
    pip install pytest
    pytest tests/test_verticals.py -v

No external API keys or DB connections required — all tests use the
deterministic 'rules' translator backend and in-memory objects only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# Allow running from project root or the tests/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("VULTURE_TRANSLATOR", "rules")

import pytest

from engine import verticals as V
from engine.llm_translator import VERTICALS, translate, TranslationError
from engine.rules import (
    _extract_tv_size_from_title,
    _extract_gpu_tier_rank_from_title,
    matches_rules,
    rejection_reason,
)
from models.listing import Listing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def listing(title: str, price: int, source: str = "craigslist") -> Listing:
    return Listing(source, title, price, "Houston", "http://example.com")


def sim_rules(t) -> dict:
    """
    Build a rules dict from a HuntTranslation that mirrors what
    hunt_to_execution_dict() produces at runtime.
    """
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
    if ao.get("min_capacity_gb") is not None:
        rules["min_capacity_gb"] = int(ao["min_capacity_gb"])
    if ao.get("min_year") is not None:
        rules["min_year"] = int(ao["min_year"])
    if ao.get("max_year") is not None:
        rules["max_year"] = int(ao["max_year"])
    if ao.get("min_vram_gb") is not None:
        rules["min_vram_gb"] = int(ao["min_vram_gb"])
    if ao.get("min_speed_mhz") is not None:
        rules["min_speed_mhz"] = int(ao["min_speed_mhz"])
    if ao.get("min_size_inches") is not None:
        rules["min_size_inches"] = int(ao["min_size_inches"])
    if ao.get("max_size_inches") is not None:
        rules["max_size_inches"] = int(ao["max_size_inches"])
    if ao.get("min_gpu_class"):
        rules["min_gpu_class"] = ao["min_gpu_class"]
    return rules


def assert_passes(title: str, price: int, rules: dict, *, msg: str = ""):
    """Assert a listing passes the rules; print rejection reason on failure."""
    reason = rejection_reason(listing(title, price), rules)
    assert reason is None, (
        f"Expected PASS but got FAIL: {title!r} — {reason}"
        + (f" | {msg}" if msg else "")
    )


def assert_fails(title: str, price: int, rules: dict, *, msg: str = ""):
    """Assert a listing is rejected by the rules."""
    reason = rejection_reason(listing(title, price), rules)
    assert reason is not None, (
        f"Expected FAIL but got PASS: {title!r}"
        + (f" | {msg}" if msg else "")
    )


# ===========================================================================
# 1. Vertical constants
# ===========================================================================


class TestVerticalConstants:
    def test_all_constants_are_strings(self):
        for name in dir(V):
            if name.startswith("VERTICAL_") and not name.startswith("VERTICAL_G"):
                val = getattr(V, name)
                if not isinstance(val, frozenset):
                    assert isinstance(val, str), f"{name} should be str, got {type(val)}"

    def test_constants_match_verticals_dict_keys(self):
        """Every VERTICAL_* constant must be a key in the translator VERTICALS dict."""
        for key in (
            V.VERTICAL_TV,
            V.VERTICAL_COMPUTER,
            V.VERTICAL_LAPTOPS,
            V.VERTICAL_VEHICLES,
            V.VERTICAL_FURNITURE,
            V.VERTICAL_GENERAL,
        ):
            assert key in VERTICALS, f"Constant {key!r} not in translator VERTICALS dict"

    def test_all_verticals_frozenset_covers_known_keys(self):
        expected = {
            V.VERTICAL_TV,
            V.VERTICAL_COMPUTER,
            V.VERTICAL_LAPTOPS,
            V.VERTICAL_VEHICLES,
            V.VERTICAL_FURNITURE,
            V.VERTICAL_GENERAL,
        }
        assert expected == V.ALL_VERTICALS

    def test_gpu_ram_aliases_match_computer(self):
        assert V.VERTICAL_GPU == V.VERTICAL_COMPUTER
        assert V.VERTICAL_RAM == V.VERTICAL_COMPUTER


# ===========================================================================
# 2. GPU tier list
# ===========================================================================


class TestGpuTier:
    def test_tier_list_non_empty(self):
        assert len(V.GPU_TIER) > 0

    def test_rank_dict_covers_all_tier_entries(self):
        for model in V.GPU_TIER:
            assert model.upper() in V.GPU_TIER_RANK

    def test_rank_order_is_zero_indexed(self):
        assert V.GPU_TIER_RANK[V.GPU_TIER[0].upper()] == 0

    def test_rtx_3090_outranks_rtx_3080(self):
        rank_3080 = V.GPU_TIER_RANK.get("RTX 3080")
        rank_3090 = V.GPU_TIER_RANK.get("RTX 3090")
        assert rank_3080 is not None, "RTX 3080 missing from GPU_TIER"
        assert rank_3090 is not None, "RTX 3090 missing from GPU_TIER"
        assert rank_3090 > rank_3080

    def test_rtx_3080_ti_outranks_rtx_3080(self):
        rank_3080    = V.GPU_TIER_RANK.get("RTX 3080")
        rank_3080_ti = V.GPU_TIER_RANK.get("RTX 3080 TI")
        assert rank_3080 is not None and rank_3080_ti is not None
        assert rank_3080_ti > rank_3080

    def test_rtx_4090_is_highest_nvidia(self):
        rank_4090 = V.GPU_TIER_RANK.get("RTX 4090")
        for model in ("RTX 3080", "RTX 3090", "RTX 4080", "RTX 4070 TI"):
            assert V.GPU_TIER_RANK[model] < rank_4090, f"{model} should be below RTX 4090"

    def test_gtx_1060_outranks_gtx_1050(self):
        assert V.GPU_TIER_RANK["GTX 1060"] > V.GPU_TIER_RANK["GTX 1050"]


# ===========================================================================
# 3. TV size title parser
# ===========================================================================


class TestExtractTvSizeFromTitle:
    def test_inch_suffix(self):
        assert _extract_tv_size_from_title("Samsung 75 inch 4K TV") == 75

    def test_inch_hyphen(self):
        assert _extract_tv_size_from_title("LG 65-inch OLED TV") == 65

    def test_quote_suffix(self):
        assert _extract_tv_size_from_title('Sony 55" Smart TV') == 55

    def test_no_space_in_suffix(self):
        assert _extract_tv_size_from_title("Samsung 65in 4K TV") == 65

    def test_no_suffix_returns_none(self):
        # "Samsung 75 OLED TV" — no inch/in/"  → None (conservative pass-through)
        assert _extract_tv_size_from_title("Samsung 75 OLED TV") is None

    def test_bare_number_in_context_returns_none(self):
        # "65 in good condition" — "65 in" NOT "65in" (space before in)
        assert _extract_tv_size_from_title("65 in good condition TV") is None

    def test_size_below_range_excluded(self):
        # 15 inches < _TV_SIZE_MIN (20) → None
        assert _extract_tv_size_from_title("15 inch portable TV") is None

    def test_size_above_range_excluded(self):
        # 150 inches > _TV_SIZE_MAX (120) → None
        assert _extract_tv_size_from_title("150 inch TV") is None

    def test_valid_boundary_low(self):
        assert _extract_tv_size_from_title("20 inch TV") == 20

    def test_valid_boundary_high(self):
        assert _extract_tv_size_from_title("120 inch TV") == 120


# ===========================================================================
# 4. GPU tier extraction from title
# ===========================================================================


class TestExtractGpuTierFromTitle:
    def test_rtx_3080_detected(self):
        rank = _extract_gpu_tier_rank_from_title("EVGA RTX 3080 10GB XC3")
        assert rank is not None
        assert rank == V.GPU_TIER_RANK["RTX 3080"]

    def test_rtx_3080_ti_outranks_rtx_3080_in_same_title(self):
        # Title contains both "RTX 3080" (substring) and "RTX 3080 TI"
        rank = _extract_gpu_tier_rank_from_title("RTX 3080 Ti 12GB Gaming OC")
        assert rank == V.GPU_TIER_RANK["RTX 3080 TI"]

    def test_unknown_gpu_returns_none(self):
        assert _extract_gpu_tier_rank_from_title("Some Webcam 1080p USB") is None

    def test_rx_6800_xt_detected(self):
        rank = _extract_gpu_tier_rank_from_title("Sapphire RX 6800 XT 16GB")
        assert rank == V.GPU_TIER_RANK["RX 6800 XT"]

    def test_rtx_4090_max_rank_nvidia(self):
        rank = _extract_gpu_tier_rank_from_title("ASUS RTX 4090 24GB STRIX OC")
        assert rank == V.GPU_TIER_RANK["RTX 4090"]


# ===========================================================================
# 5. Acceptance Test 1 — TV: 75 inch 4K under $500
# ===========================================================================


class TestAcceptanceTV75:
    """
    Intent: "Find me a 75 inch 4K TV under $500 near Houston"
    Expected:
      - vertical: tv_home_theater
      - max_price: 500
      - adapter_options["min_size_inches"] == 75
      - adapter_options["max_size_inches"] == 75
      - include_keywords contains 4K aliases (e.g. "4k", "uhd")
    """

    @pytest.fixture(scope="class")
    def translation(self):
        return translate(
            "Find me a 75 inch 4K TV under $500 near Houston",
            location="houston",
        )

    @pytest.fixture(scope="class")
    def rules(self, translation):
        return sim_rules(translation)

    def test_vertical(self, translation):
        assert translation.vertical == V.VERTICAL_TV

    def test_category(self, translation):
        assert translation.category == "tv home theater"

    def test_max_price(self, translation):
        assert translation.max_price == 500

    def test_min_size_inches(self, translation):
        assert translation.adapter_options.get("min_size_inches") == 75

    def test_max_size_inches(self, translation):
        assert translation.adapter_options.get("max_size_inches") == 75

    def test_resolution_in_include_keywords(self, translation):
        kws = [k.lower() for k in translation.include_keywords]
        # At least one 4K alias should appear
        has_4k = any(alias in kws for alias in ("4k", "uhd", "ultra hd", "2160p"))
        assert has_4k, f"No 4K alias in include_keywords: {translation.include_keywords}"

    def test_pass_correct_75in_4k(self, rules):
        assert_passes("Samsung 75 inch 4K UHD Smart TV 2023", 450, rules)

    def test_pass_uhd_alias(self, rules):
        assert_passes("Sony 75 inch UHD Smart TV", 490, rules)

    def test_pass_2160p_alias(self, rules):
        assert_passes("LG 75 inch 2160p 4K TV", 470, rules)

    def test_fail_wrong_size_65(self, rules):
        assert_fails("Samsung 65 inch 4K Smart TV", 420, rules, msg="size 65 should fail for 75-inch hunt")

    def test_fail_no_resolution(self, rules):
        assert_fails("75 inch Smart TV Hisense", 400, rules, msg="no 4K/UHD/2160p")

    def test_fail_excluded_mount(self, rules):
        assert_fails("75 inch 4K TV wall mount bracket", 40, rules)

    def test_fail_over_price(self, rules):
        assert_fails("TCL 75 inch 4K TV", 510, rules)

    def test_pass_no_size_in_title_conservative(self, rules):
        # "Samsung 4K OLED Smart TV" — no inch/in/" suffix → size not extracted
        # → conservative pass-through (include_keywords still checked)
        assert_passes("Samsung 4K OLED Smart TV", 450, rules)

    def test_fail_stand_excluded(self, rules):
        assert_fails("75 inch TV stand bracket", 60, rules)


# ===========================================================================
# 6. Acceptance Test 2 — GPU: RTX 3080 or better, card only
# ===========================================================================


class TestAcceptanceGpuOrBetter:
    """
    Intent: "Find an RTX 3080 or better under $400, card only, not a whole PC"
    Expected:
      - vertical: computer_parts
      - max_price: 400
      - adapter_options["min_gpu_class"] == "RTX 3080"
      - adapter_options["card_only"] == True
      - exclude_keywords include "laptop"
    """

    @pytest.fixture(scope="class")
    def translation(self):
        return translate(
            "Find an RTX 3080 or better under $400, card only, not a whole PC"
        )

    @pytest.fixture(scope="class")
    def rules(self, translation):
        return sim_rules(translation)

    def test_vertical(self, translation):
        assert translation.vertical == V.VERTICAL_COMPUTER

    def test_max_price(self, translation):
        assert translation.max_price == 400

    def test_min_gpu_class_set(self, translation):
        assert translation.adapter_options.get("min_gpu_class") == "RTX 3080"

    def test_card_only_flag(self, translation):
        assert translation.adapter_options.get("card_only") is True

    def test_laptop_excluded(self, translation):
        excl_lower = [k.lower() for k in translation.exclude_keywords]
        assert "laptop" in excl_lower

    def test_pass_rtx_3080(self, rules):
        assert_passes("EVGA RTX 3080 10GB XC3 Gaming", 380, rules)

    def test_pass_rtx_3090_above_floor(self, rules):
        assert_passes("Gigabyte RTX 3090 24GB Gaming OC", 390, rules,
                      msg="3090 is above 3080 tier floor")

    def test_fail_rtx_3070_below_floor(self, rules):
        assert_fails("MSI RTX 3070 8GB Gaming X Trio", 300, rules,
                     msg="3070 is below 3080 tier floor")

    def test_fail_laptop(self, rules):
        assert_fails("ASUS TUF Gaming Laptop RTX 3080 16GB", 350, rules)

    def test_fail_gaming_pc(self, rules):
        assert_fails("Gaming PC RTX 3080 i9 32GB RAM Tower", 380, rules)

    def test_fail_over_price(self, rules):
        assert_fails("RTX 3080 10GB GPU", 420, rules)

    def test_pass_rtx_3080_ti_above_3080(self, rules):
        # 3080 Ti is above 3080 in tier — should pass
        assert_passes("MSI RTX 3080 Ti 12GB Gaming X Trio", 390, rules)

    def test_unknown_gpu_title_passes_conservative(self, rules):
        # If the listing has no recognisable GPU model → tier check passes through
        # (conservative — don't false-reject unknown/newer cards).
        # Price and other rules still apply.
        assert_passes("GPU Graphics Card 10GB GDDR6X PCIe", 350, rules)


# ===========================================================================
# 7. Acceptance Test 3 — RAM: 32GB DDR4 3200MHz or faster
# ===========================================================================


class TestAcceptanceRam32DDR4:
    """
    Intent: "Find 32GB DDR4 RAM 3200mhz or faster"
    Expected:
      - vertical: computer_parts
      - adapter_options["min_capacity_gb"] == 32
      - adapter_options["min_speed_mhz"] == 3200
      - adapter_options["ddr_generation"] == "ddr4"
      - include_keywords contains "ddr4"
    """

    @pytest.fixture(scope="class")
    def translation(self):
        return translate("Find 32GB DDR4 RAM 3200mhz or faster")

    @pytest.fixture(scope="class")
    def rules(self, translation):
        return sim_rules(translation)

    def test_vertical(self, translation):
        assert translation.vertical == V.VERTICAL_COMPUTER

    def test_min_capacity_gb(self, translation):
        assert translation.adapter_options.get("min_capacity_gb") == 32

    def test_min_speed_mhz(self, translation):
        assert translation.adapter_options.get("min_speed_mhz") == 3200

    def test_ddr_generation(self, translation):
        assert translation.adapter_options.get("ddr_generation") == "ddr4"

    def test_ddr4_in_include_keywords(self, translation):
        kws_lower = [k.lower() for k in translation.include_keywords]
        assert "ddr4" in kws_lower

    def test_pass_32gb_3200mhz(self, rules):
        assert_passes("Corsair 32GB DDR4 3200MHz Desktop RAM", 55, rules)

    def test_pass_kit_2x16_faster_speed(self, rules):
        assert_passes("G.Skill 2x16GB DDR4 3600MHz", 60, rules,
                      msg="2×16=32GB, 3600 >= 3200")

    def test_fail_capacity_too_low(self, rules):
        assert_fails("Kingston 16GB DDR4 3200MHz", 28, rules, msg="16 < 32")

    def test_fail_speed_too_low(self, rules):
        assert_fails("Crucial 32GB DDR4 2400MHz", 45, rules, msg="2400 < 3200")

    def test_fail_wrong_generation_ddr5(self, rules):
        assert_fails("Corsair 32GB DDR5 6000MHz", 80, rules, msg="DDR5 not DDR4")

    def test_fail_sodimm_excluded(self, rules):
        assert_fails("32GB DDR4 3200MHz SODIMM laptop", 40, rules)

    def test_pass_no_speed_in_title_conservative(self, rules):
        # Speed absent → pass-through; capacity 32 and DDR4 still checked.
        assert_passes("32GB DDR4 desktop RAM kit", 50, rules)


# ===========================================================================
# 8. Acceptance Test 4 — Vehicles: Toyota Sequoia 2016+ under 150k miles
# ===========================================================================


class TestAcceptanceVehicleSequoia:
    """
    Intent: "Find a Toyota Sequoia 2016 or newer under 150k miles"
    Expected:
      - vertical: vehicles
      - adapter_options["min_year"] == 2016
      - adapter_options["max_miles"] == 150000
      - include_keywords contains "toyota sequoia" (make + model phrase)
    """

    @pytest.fixture(scope="class")
    def translation(self):
        return translate("Find a Toyota Sequoia 2016 or newer under 150k miles")

    @pytest.fixture(scope="class")
    def rules(self, translation):
        return sim_rules(translation)

    def test_vertical(self, translation):
        assert translation.vertical == V.VERTICAL_VEHICLES

    def test_min_year(self, translation):
        assert translation.adapter_options.get("min_year") == 2016

    def test_max_miles(self, translation):
        assert translation.adapter_options.get("max_miles") == 150_000

    def test_make_model_in_include(self, translation):
        kws_lower = [k.lower() for k in translation.include_keywords]
        assert any("toyota" in k and "sequoia" in k for k in kws_lower), (
            f"Expected 'toyota sequoia' in include_keywords, got: {translation.include_keywords}"
        )

    def test_pass_valid_listing(self, rules):
        assert_passes("2019 Toyota Sequoia SR5 4WD 65k miles", 32000, rules)

    def test_fail_year_too_old(self, rules):
        assert_fails("2014 Toyota Sequoia Platinum 90k miles", 25000, rules,
                     msg="2014 < 2016")

    def test_fail_mileage_too_high(self, rules):
        assert_fails("2018 Toyota Sequoia Limited 160k miles", 28000, rules,
                     msg="160k > 150k")

    def test_pass_no_mileage_conservative(self, rules):
        assert_passes("2017 Toyota Sequoia TRD Sport clean title", 30000, rules,
                      msg="no miles in title → pass-through")

    def test_fail_parts_listing(self, rules):
        assert_fails("Toyota Sequoia part out 2018", 800, rules)

    def test_pass_year_at_boundary(self, rules):
        assert_passes("2016 Toyota Sequoia Premium 4WD", 22000, rules,
                      msg="2016 == min_year → should pass")

    def test_fail_wrong_model(self, rules):
        assert_fails("2019 Toyota Highlander 45k miles", 28000, rules,
                     msg="include_keywords requires toyota sequoia")


# ===========================================================================
# 9. Rules engine — structured constraint unit tests
# ===========================================================================


class TestRulesMinMaxSizeInches:
    """Rules-level tests for min_size_inches / max_size_inches."""

    BASE_RULES = {"max_price": 500}

    def _r(self, min_size: Optional[int] = None, max_size: Optional[int] = None) -> dict:
        r = dict(self.BASE_RULES)
        if min_size is not None:
            r["min_size_inches"] = min_size
        if max_size is not None:
            r["max_size_inches"] = max_size
        return r

    def test_exact_size_match_passes(self):
        assert_passes("Samsung 75 inch 4K TV", 400, self._r(75, 75))

    def test_size_below_min_fails(self):
        assert_fails("Samsung 65 inch 4K TV", 400, self._r(75, 75),
                     msg="65 < min 75")

    def test_size_above_max_fails(self):
        assert_fails("LG 85 inch 4K TV", 400, self._r(min_size=55, max_size=65),
                     msg="85 > max 65")

    def test_no_size_in_title_passes_conservative(self):
        # "Samsung 4K TV" has no inch/in/"  → None → pass
        assert_passes("Samsung 4K Smart TV", 400, self._r(75, 75))

    def test_size_with_quote_suffix(self):
        assert_passes('TCL 75" 4K Smart TV', 400, self._r(75, 75))

    def test_size_with_in_suffix(self):
        assert_passes("Hisense 75in 4K TV", 400, self._r(75, 75))

    def test_price_still_enforced(self):
        assert_fails("Samsung 75 inch 4K TV", 600, self._r(75, 75),
                     msg="over price even with correct size")


class TestRulesMinGpuClass:
    """Rules-level tests for min_gpu_class tier enforcement."""

    BASE_RULES: dict = {}

    def _r(self, min_gpu_class: str) -> dict:
        return {**self.BASE_RULES, "min_gpu_class": min_gpu_class}

    def test_equal_tier_passes(self):
        assert_passes("EVGA RTX 3080 10GB Gaming", 350, self._r("RTX 3080"))

    def test_higher_tier_passes(self):
        assert_passes("Gigabyte RTX 3090 24GB", 380, self._r("RTX 3080"))

    def test_lower_tier_fails(self):
        assert_fails("MSI RTX 3070 8GB Gaming", 300, self._r("RTX 3080"),
                     msg="3070 below 3080 floor")

    def test_unknown_gpu_in_title_passes_conservative(self):
        # No recognisable GPU model → tier rank = None → conservative pass
        assert_passes("Generic GPU Card 8GB GDDR6", 200, self._r("RTX 3080"))

    def test_unknown_min_gpu_class_passes_conservative(self):
        # min_gpu_class not in tier list → min_rank = None → pass
        assert_passes("EVGA RTX 3070 8GB", 300, self._r("RTX 9999 FICTIONAL"))

    def test_rtx_3080_ti_above_3080(self):
        assert_passes("RTX 3080 Ti 12GB Founders Edition", 390, self._r("RTX 3080"))

    def test_rtx_4090_above_3080(self):
        assert_passes("ASUS RTX 4090 24GB STRIX OC", 395, self._r("RTX 3080"))


class TestRulesVerticalContextPrefix:
    """Rejection reason strings include the vertical prefix when set."""

    def test_vertical_prefix_in_rejection(self):
        rules = {
            "vertical": "tv_home_theater",
            "max_price": 100,
        }
        reason = rejection_reason(listing("Samsung 75 inch 4K TV", 200), rules)
        assert reason is not None
        assert "tv_home_theater" in reason

    def test_no_prefix_when_vertical_absent(self):
        rules = {"max_price": 100}
        reason = rejection_reason(listing("Some TV", 200), rules)
        assert reason is not None
        assert "[" not in reason  # no vertical bracket prefix


# ===========================================================================
# 10. Translator — edge cases and regression guards
# ===========================================================================


class TestTranslatorEdgeCases:
    def test_empty_intent_raises(self):
        with pytest.raises(TranslationError):
            translate("")

    def test_blank_intent_raises(self):
        with pytest.raises(TranslationError):
            translate("   ")

    def test_vertical_always_in_all_verticals(self):
        for intent in (
            "75 inch 4K TV under $500",
            "RTX 3080 GPU under $400",
            "DDR4 RAM 32GB",
            "Toyota Camry 2018 under $15000",
            "laptop for college",
            "buy something cool",
        ):
            t = translate(intent)
            assert t.vertical in V.ALL_VERTICALS, (
                f"Intent {intent!r} → vertical {t.vertical!r} not in ALL_VERTICALS"
            )

    def test_tv_size_in_adapter_options(self):
        t = translate("75 inch 4K TV under $500")
        assert t.adapter_options.get("min_size_inches") == 75
        assert t.adapter_options.get("max_size_inches") == 75

    def test_tv_no_size_no_size_opts(self):
        t = translate("4K TV under $300")
        assert t.adapter_options.get("min_size_inches") is None
        assert t.adapter_options.get("max_size_inches") is None

    def test_gpu_or_better_sets_min_gpu_class(self):
        t = translate("RTX 3080 or better GPU under $400")
        assert t.adapter_options.get("min_gpu_class") == "RTX 3080"

    def test_gpu_no_or_better_no_min_gpu_class(self):
        t = translate("RTX 3080 GPU under $400")
        assert t.adapter_options.get("min_gpu_class") is None

    def test_gpu_card_only_flag(self):
        t = translate("RTX 3080 GPU card only not a whole PC under $400")
        assert t.adapter_options.get("card_only") is True

    def test_ram_ddr_generation_metadata(self):
        t = translate("32GB DDR4 RAM")
        assert t.adapter_options.get("ddr_generation") == "ddr4"

    def test_ram_exact_gb_fallback(self):
        # No "at least / more than" qualifier — exact GB treated as minimum
        t = translate("Find 32GB DDR4 RAM 3200mhz or faster")
        assert t.adapter_options.get("min_capacity_gb") == 32

    def test_ram_qualified_gb(self):
        t = translate("DDR5 RAM at least 64GB under $150")
        assert t.adapter_options.get("min_capacity_gb") == 64

    def test_vehicle_min_year(self):
        t = translate("Toyota Sequoia 2016 or newer")
        assert t.adapter_options.get("min_year") == 2016

    def test_vehicle_max_miles(self):
        t = translate("Toyota Sequoia under 150k miles")
        assert t.adapter_options.get("max_miles") == 150_000
