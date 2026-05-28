"""
tests/test_intent_translator_v2.py

Regression tests for Intent Translator v2.

All six required regression cases from the feature spec are covered, plus
additional unit tests for each pipeline step so individual functions can be
verified in isolation.

Run:
    pytest tests/test_intent_translator_v2.py -v
"""

import pytest

# ---------------------------------------------------------------------------
# Import the pipeline steps under test
# ---------------------------------------------------------------------------
from engine.intent_translator_v2 import (
    VEHICLE_PARTS_EXCLUDE,
    _REQUIRED_VEHICLE_EXCL,
    classify_vertical,
    extract_constraints,
    extract_entities,
    build_hunt,
    validate_hunt,
    translate_v2,
    _expand_k,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _excl_lower(hunt: dict) -> set[str]:
    return {kw.lower() for kw in hunt.get("exclude_keywords", [])}


def _has_required_excl(hunt: dict) -> bool:
    excl = _excl_lower(hunt)
    return _REQUIRED_VEHICLE_EXCL.issubset(excl)


# ===========================================================================
# Step 0: _expand_k
# ===========================================================================

class TestExpandK:
    def test_plain_k(self):
        assert _expand_k("50k") == "50000"

    def test_dollar_prefix(self):
        assert _expand_k("$30k") == "$30000"

    def test_decimal(self):
        assert _expand_k("1.5k") == "1500"

    def test_no_expand_word(self):
        # "ok" should not be expanded — no digit before k
        assert _expand_k("ok") == "ok"

    def test_multiple(self):
        assert _expand_k("under 50k miles under $30k") == "under 50000 miles under $30000"

    def test_4k_resolution(self):
        # "4k" TV resolution — k-suffix on digit, so it IS expanded here.
        # The raw (non-expanded) intent string is used for vertical/resolution detection;
        # expanded string is only used for numeric extraction.
        assert _expand_k("4k") == "4000"


# ===========================================================================
# Step 1: classify_vertical
# ===========================================================================

class TestClassifyVertical:
    def test_vehicles_make_model(self):
        assert classify_vertical("toyota sequoia under 30k") == "vehicles"

    def test_vehicles_miles_keyword(self):
        assert classify_vertical("honda civic less than 100k miles") == "vehicles"

    def test_vehicles_generic_keyword(self):
        assert classify_vertical("used truck under 15k") == "vehicles"

    def test_computer_parts_gpu(self):
        assert classify_vertical("rtx 3080 under $300") == "computer_parts"

    def test_computer_parts_ram(self):
        assert classify_vertical("32gb ddr5 ram under $200") == "computer_parts"

    def test_home_theater_tv(self):
        assert classify_vertical("samsung 75 inch 4k TV under 1500") == "home_theater"

    def test_general_fallback(self):
        v = classify_vertical("vintage lamp")
        assert v == "general_marketplace"


# ===========================================================================
# Step 2: extract_entities
# ===========================================================================

class TestExtractEntities:
    def test_make_and_model(self):
        e = extract_entities("toyota sequoia under 50k miles", "vehicles")
        assert e["make"] == "toyota"
        assert e["model"] == "sequoia"

    def test_model_infers_make(self):
        e = extract_entities("sequoia under 50k miles", "vehicles")
        assert e["make"] == "toyota"
        assert e["model"] == "sequoia"

    def test_make_only(self):
        e = extract_entities("honda under 15k", "vehicles")
        assert e["make"] == "honda"
        assert "model" not in e or e.get("model") == ""

    def test_min_year_newer(self):
        e = extract_entities("2018 or newer toyota sequoia", "vehicles")
        assert e.get("min_year") == 2018

    def test_min_year_from(self):
        e = extract_entities("from 2016 honda civic", "vehicles")
        assert e.get("min_year") == 2016

    def test_no_year(self):
        e = extract_entities("toyota sequoia under 30k", "vehicles")
        assert "min_year" not in e
        assert "max_year" not in e

    def test_non_vehicle_returns_empty(self):
        e = extract_entities("rtx 3080 under $300", "computer_parts")
        assert e == {}


# ===========================================================================
# Step 3: extract_constraints
# ===========================================================================

class TestExtractConstraints:

    # --- Mileage detection ---

    def test_less_than_miles(self):
        c = extract_constraints("toyota sequoia less than 50k miles", "vehicles")
        assert c["max_miles"] == 50_000

    def test_under_miles_abbreviated(self):
        c = extract_constraints("toyota sequoia under 50k mi", "vehicles")
        assert c["max_miles"] == 50_000

    def test_under_miles_full(self):
        c = extract_constraints("under 100000 miles", "vehicles")
        assert c["max_miles"] == 100_000

    def test_miles_or_less(self):
        c = extract_constraints("100k miles or less", "vehicles")
        assert c["max_miles"] == 100_000

    # --- Price detection ---

    def test_dollar_prefix(self):
        c = extract_constraints("toyota sequoia under $30k", "vehicles")
        assert c["max_price"] == 30_000
        assert "max_miles" not in c

    def test_dollars_suffix(self):
        c = extract_constraints("less than 30000 dollars", "vehicles")
        assert c["max_price"] == 30_000

    def test_explicit_dollar_sign_k(self):
        c = extract_constraints("under $30k", "vehicles")
        assert c["max_price"] == 30_000

    # --- Disambiguation ---

    def test_miles_and_price_together(self):
        """Core regression: mileage must not bleed into price."""
        c = extract_constraints(
            "toyota sequoia less than 50k miles and less than 30k dollars",
            "vehicles",
        )
        assert c["max_miles"] == 50_000, f"max_miles wrong: {c}"
        assert c["max_price"] == 30_000, f"max_price wrong: {c}"

    def test_miles_and_dollar_price(self):
        c = extract_constraints(
            "toyota sequoia under 50k miles under $30k",
            "vehicles",
        )
        assert c["max_miles"] == 50_000
        assert c["max_price"] == 30_000

    def test_ambiguous_under_no_unit_vehicle_context(self):
        """'under 30k' with no unit in vehicle context → price, not miles."""
        c = extract_constraints("toyota sequoia under 30k", "vehicles")
        assert c.get("max_price") == 30_000
        assert "max_miles" not in c

    def test_large_miles_does_not_become_price(self):
        """'under 150k miles' must never yield a max_price of 150000."""
        c = extract_constraints("toyota sequoia under 150k miles", "vehicles")
        assert c["max_miles"] == 150_000
        assert "max_price" not in c

    def test_price_before_miles(self):
        """Price comes before miles in the string."""
        c = extract_constraints(
            "toyota sequoia less than 30000 dollars under 50000 miles",
            "vehicles",
        )
        assert c["max_price"] == 30_000
        assert c["max_miles"] == 50_000

    # --- Year range ---

    def test_min_year_newer(self):
        c = extract_constraints(
            "2018 or newer toyota sequoia under 30k with less than 100k miles",
            "vehicles",
        )
        assert c.get("min_year") == 2018

    def test_year_not_extracted_for_non_vehicle(self):
        c = extract_constraints("rtx 3080 under $300", "computer_parts")
        assert "min_year" not in c
        assert "max_year" not in c


# ===========================================================================
# Step 5: validate_hunt (unit tests)
# ===========================================================================

class TestValidateHunt:
    def _make_vehicle_hunt(self, max_price=None, max_miles=None):
        from engine.intent_translator_v2 import VEHICLE_PARTS_EXCLUDE
        return {
            "max_price": max_price,
            "exclude_keywords": list(VEHICLE_PARTS_EXCLUDE),
            "adapter_options": {
                "min_price": 200,
                **({"max_miles": max_miles} if max_miles is not None else {}),
            },
            "_intent": "test",
        }

    def test_valid_hunt_passes(self):
        hunt = self._make_vehicle_hunt(max_price=30_000, max_miles=50_000)
        result = validate_hunt(hunt, "vehicles")
        assert result["max_price"] == 30_000
        assert result["adapter_options"]["max_miles"] == 50_000

    def test_price_equals_miles_is_corrected(self):
        """When max_price equals max_miles, price is nulled conservatively."""
        hunt = self._make_vehicle_hunt(max_price=50_000, max_miles=50_000)
        result = validate_hunt(hunt, "vehicles")
        assert result["max_price"] is None

    def test_implausible_price_corrected(self):
        hunt = self._make_vehicle_hunt(max_price=600_000, max_miles=None)
        result = validate_hunt(hunt, "vehicles")
        assert result["max_price"] is None

    def test_missing_required_exclusions_raises(self):
        from engine.llm_translator import TranslationError
        hunt = {
            "max_price": 30_000,
            "exclude_keywords": [],      # deliberately empty
            "adapter_options": {},
            "_intent": "test",
        }
        with pytest.raises(TranslationError):
            validate_hunt(hunt, "vehicles")


# ===========================================================================
# Full pipeline: translate_v2  — the six required regression tests
# ===========================================================================

class TestTranslateV2Regression:
    """
    Six mandatory regression cases from the feature specification.
    Each asserts the minimum expected output; extra fields are fine.
    """

    def test_regression_1_sequoia_50k_miles_30k_dollars(self):
        """
        "toyota sequoia less than 50k miles and less than 30k dollars"
        Expected: vertical=vehicles, toyota+sequoia, max_price=30000, max_miles=50000,
                  vehicle parts exclusions present.
        """
        t = translate_v2(
            "toyota sequoia less than 50k miles and less than 30k dollars"
        )
        assert t.vertical == "vehicles", f"vertical={t.vertical}"
        assert t.max_price == 30_000, f"max_price={t.max_price}"
        assert t.adapter_options.get("max_miles") == 50_000, \
            f"max_miles={t.adapter_options.get('max_miles')}"
        excl_lower = {kw.lower() for kw in t.exclude_keywords}
        assert _REQUIRED_VEHICLE_EXCL.issubset(excl_lower), \
            f"Missing exclusions: {_REQUIRED_VEHICLE_EXCL - excl_lower}"
        # Toyota Sequoia recognised
        terms_combined = " ".join(t.search_terms).lower()
        assert "toyota" in terms_combined or "sequoia" in terms_combined, \
            f"Toyota/Sequoia not in search_terms: {t.search_terms}"

    def test_regression_2_sequoia_under_50k_miles_under_dollar30k(self):
        """
        "toyota sequoia under 50k miles under $30k"
        Expected: max_miles=50000, max_price=30000.
        """
        t = translate_v2("toyota sequoia under 50k miles under $30k")
        assert t.max_price == 30_000, f"max_price={t.max_price}"
        assert t.adapter_options.get("max_miles") == 50_000, \
            f"max_miles={t.adapter_options.get('max_miles')}"

    def test_regression_3_30000_dollars_50000_miles(self):
        """
        "toyota sequoia less than 30000 dollars under 50000 miles"
        Expected: max_price=30000, max_miles=50000.
        """
        t = translate_v2(
            "toyota sequoia less than 30000 dollars under 50000 miles"
        )
        assert t.max_price == 30_000, f"max_price={t.max_price}"
        assert t.adapter_options.get("max_miles") == 50_000, \
            f"max_miles={t.adapter_options.get('max_miles')}"

    def test_regression_4_sequoia_under_30k_no_miles(self):
        """
        "toyota sequoia under 30k"
        Expected: max_price=30000, no max_miles.
        """
        t = translate_v2("toyota sequoia under 30k")
        assert t.max_price == 30_000, f"max_price={t.max_price}"
        assert "max_miles" not in t.adapter_options, \
            f"max_miles should not be set: {t.adapter_options}"

    def test_regression_5_under_150k_miles_no_price(self):
        """
        "toyota sequoia under 150k miles"
        Expected: max_miles=150000, max_price NOT inferred from 150k.
        """
        t = translate_v2("toyota sequoia under 150k miles")
        assert t.adapter_options.get("max_miles") == 150_000, \
            f"max_miles={t.adapter_options.get('max_miles')}"
        assert t.max_price is None, \
            f"max_price should be None, got {t.max_price}"

    def test_regression_6_2018_newer_30k_100k_miles(self):
        """
        "2018 or newer toyota sequoia under 30k with less than 100k miles"
        Expected: min_year=2018, max_price=30000, max_miles=100000.
        """
        t = translate_v2(
            "2018 or newer toyota sequoia under 30k with less than 100k miles"
        )
        assert t.adapter_options.get("min_year") == 2018, \
            f"min_year={t.adapter_options.get('min_year')}"
        assert t.max_price == 30_000, f"max_price={t.max_price}"
        assert t.adapter_options.get("max_miles") == 100_000, \
            f"max_miles={t.adapter_options.get('max_miles')}"


# ===========================================================================
# Additional integration tests
# ===========================================================================

class TestTranslateV2Integration:
    def test_vehicle_parts_exclusions_present(self):
        """All required parts exclusions must appear in every vehicle hunt."""
        t = translate_v2("ford f150 under 20k")
        excl_lower = {kw.lower() for kw in t.exclude_keywords}
        assert _REQUIRED_VEHICLE_EXCL.issubset(excl_lower)

    def test_vehicle_parts_exclusion_list_completeness(self):
        """The VEHICLE_PARTS_EXCLUDE list must contain all spec-required items."""
        required_by_spec = {
            "roof rack", "wheels", "wheel", "rims", "rim",
            "tires", "tire", "engine", "transmission",
            "headlight", "taillight", "bumper", "fender", "door", "hood",
            "part out", "partout", "oem", "parts",
        }
        excl_lower = {kw.lower() for kw in VEHICLE_PARTS_EXCLUDE}
        missing = required_by_spec - excl_lower
        assert not missing, f"VEHICLE_PARTS_EXCLUDE is missing: {missing}"

    def test_max_miles_in_adapter_options(self):
        """max_miles must live in adapter_options, never in max_price."""
        t = translate_v2("honda civic under 80k miles under $15k")
        assert "max_miles" in t.adapter_options
        assert t.max_price != t.adapter_options["max_miles"]

    def test_min_price_filter_set_for_vehicles(self):
        """Vehicle hunts auto-set min_price=200 to filter placeholder ads."""
        t = translate_v2("toyota camry under 25k")
        assert t.adapter_options.get("min_price") == 200

    def test_non_vehicle_no_max_miles(self):
        """max_miles must not be set for non-vehicle verticals."""
        t = translate_v2("RTX 3080 under $300")
        assert "max_miles" not in t.adapter_options

    def test_location_sanitized(self):
        t = translate_v2("toyota sequoia under 30k", location="houston")
        assert t.location == "houston"

    def test_multiword_location_rejected(self):
        t = translate_v2("toyota sequoia under 30k", location="los angeles")
        assert t.location is None

    def test_max_price_override(self):
        """Caller-supplied max_price wins over extracted value."""
        t = translate_v2("toyota sequoia under 30k", max_price=25_000)
        assert t.max_price == 25_000

    def test_vertical_key_valid(self):
        """vertical field on HuntTranslation must be a known VERTICALS key."""
        from engine.llm_translator import VERTICALS
        t = translate_v2("toyota sequoia under 30k miles under $30k")
        assert t.vertical in VERTICALS, f"Unknown vertical: {t.vertical}"

    def test_source_sites_non_empty(self):
        t = translate_v2("toyota sequoia under 30k")
        assert t.source_sites, "source_sites must not be empty"

    def test_search_terms_non_empty(self):
        t = translate_v2("toyota sequoia under 30k")
        assert t.search_terms, "search_terms must not be empty"

    def test_translated_by_label(self):
        t = translate_v2("toyota sequoia under 30k")
        assert t.translated_by == "rules-v2"

    def test_empty_intent_raises(self):
        from engine.llm_translator import TranslationError
        with pytest.raises(TranslationError):
            translate_v2("")

    def test_whitespace_only_intent_raises(self):
        from engine.llm_translator import TranslationError
        with pytest.raises(TranslationError):
            translate_v2("   ")
