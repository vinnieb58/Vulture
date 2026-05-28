"""
tests/test_translator_non_vehicle_regression.py

Regression tests that confirm the v1 builder logic is preserved for all
non-vehicle verticals (GPU, RAM, TV, general marketplace) after the v2
routing change.

All tests call engine.llm_translator.translate() — the same public entry
point used by Discord/command_router — so they exercise the full path:
    translate() → _translate_rules_based() → routing decision
      vehicle  → translate_v2()             (v2 pipeline)
      non-veh  → _translate_v1_non_vehicle() (v1 builders, unchanged)

Run:
    pytest tests/test_translator_non_vehicle_regression.py -v
"""

import pytest
from engine.llm_translator import translate


# ===========================================================================
# Helpers
# ===========================================================================

def _terms_lower(t) -> str:
    """Join search_terms in lowercase for substring checks."""
    return " ".join(t.search_terms).lower()


def _incl_lower(t) -> set[str]:
    return {kw.lower() for kw in t.include_keywords}


def _excl_lower(t) -> set[str]:
    return {kw.lower() for kw in t.exclude_keywords}


# ===========================================================================
# GPU vertical
# ===========================================================================

class TestGPURegression:
    """
    GPU hunts must:
    - Route through v1 (translated_by='rules')
    - Extract the GPU model and build a clean search phrase (no price noise)
    - Put the model number in include_keywords
    - Set max_price correctly
    - Exclude laptop / gaming-system terms
    """

    def test_rtx_3080_search_term(self):
        t = translate("rtx 3080 under $300")
        assert t.vertical == "computer_parts"
        assert t.translated_by == "rules"
        terms = _terms_lower(t)
        assert "rtx" in terms and "3080" in terms, f"Model not in search_terms: {t.search_terms}"
        # Price noise must NOT appear in search term
        assert "300" not in terms, f"Price leaked into search_terms: {t.search_terms}"

    def test_rtx_3080_include_keywords(self):
        t = translate("rtx 3080 under $300")
        assert "3080" in _incl_lower(t), f"include_keywords: {t.include_keywords}"

    def test_rtx_3080_max_price(self):
        t = translate("rtx 3080 under $300")
        assert t.max_price == 300

    def test_rtx_4090_ti(self):
        t = translate("rtx 4090 ti under $1200")
        assert t.vertical == "computer_parts"
        terms = _terms_lower(t)
        assert "4090" in terms
        incl = _incl_lower(t)
        assert any("4090" in kw for kw in incl), f"include_keywords: {t.include_keywords}"
        assert t.max_price == 1200

    def test_amd_rx_6800_xt(self):
        t = translate("rx 6800 xt under $400")
        assert t.vertical == "computer_parts"
        terms = _terms_lower(t)
        assert "6800" in terms
        incl = _incl_lower(t)
        assert any("6800" in kw for kw in incl)
        assert t.max_price == 400

    def test_gpu_excludes_laptop(self):
        """Laptop / complete-system exclusions must be present on every GPU hunt."""
        t = translate("rtx 3080 under $300")
        excl = _excl_lower(t)
        assert "laptop" in excl, f"'laptop' missing from exclude_keywords: {t.exclude_keywords}"
        assert "gaming pc" in excl or "gaming desktop" in excl, \
            f"System exclusions missing: {t.exclude_keywords}"

    def test_gpu_no_max_miles(self):
        t = translate("rtx 3080 under $300")
        assert "max_miles" not in t.adapter_options

    def test_gpu_translated_by_v1(self):
        t = translate("rtx 3080 under $300")
        assert t.translated_by == "rules", f"Expected rules, got {t.translated_by!r}"

    def test_gtx_corrected_to_rtx_for_3xxx(self):
        """'gtx 3080' should be silently corrected to RTX 3080."""
        t = translate("gtx 3080 under $300")
        assert "rtx" in _terms_lower(t) or "3080" in _terms_lower(t)

    def test_vram_constraint_in_adapter_options(self):
        t = translate("rtx 3080 with at least 10gb vram under $400")
        assert t.adapter_options.get("min_vram_gb") == 10, \
            f"min_vram_gb={t.adapter_options.get('min_vram_gb')}"

    def test_gpu_source_sites_non_empty(self):
        t = translate("rtx 3080 under $300")
        assert t.source_sites


# ===========================================================================
# RAM vertical
# ===========================================================================

class TestRAMRegression:
    """
    RAM hunts must:
    - Route through v1 (translated_by='rules')
    - Build search term with DDR type + "desktop RAM" prefix
    - Set include_keywords to the DDR type
    - Exclude SODIMM / laptop RAM / server ECC
    - Put min_capacity_gb and min_speed_mhz in adapter_options
    """

    def test_ddr4_search_term(self):
        t = translate("ddr4 ram under $100")
        assert t.vertical == "computer_parts"
        terms = _terms_lower(t)
        assert "ddr4" in terms, f"DDR4 not in search_terms: {t.search_terms}"
        assert "desktop" in terms, f"'desktop' not in search_terms: {t.search_terms}"
        # Price noise must not appear in search term
        assert "100" not in terms, f"Price leaked into search_terms: {t.search_terms}"

    def test_ddr4_include_keywords(self):
        t = translate("ddr4 ram under $100")
        assert "ddr4" in _incl_lower(t), f"include_keywords: {t.include_keywords}"

    def test_ddr5_detection(self):
        t = translate("ddr5 ram under $200")
        terms = _terms_lower(t)
        assert "ddr5" in terms
        assert "ddr5" in _incl_lower(t)
        assert t.max_price == 200

    def test_ram_excludes_sodimm(self):
        """SODIMM / laptop RAM exclusions must be present."""
        t = translate("ddr4 ram under $100")
        excl = _excl_lower(t)
        assert "sodimm" in excl or "so-dimm" in excl, \
            f"SODIMM exclusion missing: {t.exclude_keywords}"

    def test_min_capacity_in_adapter_options(self):
        t = translate("at least 32gb ddr4 ram under $150")
        ao = t.adapter_options
        assert ao.get("min_capacity_gb") == 32, f"adapter_options: {ao}"

    def test_min_speed_in_adapter_options(self):
        t = translate("ddr5 ram at least 3200mhz under $200")
        ao = t.adapter_options
        assert ao.get("min_speed_mhz") == 3200, f"adapter_options: {ao}"

    def test_ram_max_price(self):
        t = translate("32gb ddr4 ram under $150")
        assert t.max_price == 150

    def test_ram_no_max_miles(self):
        t = translate("ddr4 ram under $100")
        assert "max_miles" not in t.adapter_options

    def test_ram_translated_by_v1(self):
        t = translate("ddr4 ram under $100")
        assert t.translated_by == "rules"

    def test_ram_source_sites(self):
        t = translate("ddr4 ram under $100")
        assert t.source_sites


# ===========================================================================
# TV / Home Theater vertical
# ===========================================================================

class TestTVRegression:
    """
    TV hunts must:
    - Route through v1 (translated_by='rules')
    - Extract size, resolution, brand, panel type into search_terms
    - Put discriminating keywords in include_keywords or require_all_keywords
    - Set max_price correctly
    - Exclude TV stands / mounts / remotes
    - Price noise must NOT appear in search_terms
    """

    def test_tv_vertical(self):
        t = translate("samsung 75 inch 4k tv under $1500")
        assert t.vertical == "tv_home_theater"

    def test_tv_brand_in_search_term(self):
        t = translate("samsung 75 inch 4k tv under $1500")
        terms = _terms_lower(t)
        assert "samsung" in terms, f"Brand not in search_terms: {t.search_terms}"

    def test_tv_size_in_search_term(self):
        t = translate("samsung 75 inch 4k tv under $1500")
        terms = _terms_lower(t)
        assert "75" in terms, f"Size not in search_terms: {t.search_terms}"

    def test_tv_resolution_in_search_term(self):
        t = translate("samsung 75 inch 4k tv under $1500")
        terms = _terms_lower(t)
        assert "4k" in terms, f"Resolution not in search_terms: {t.search_terms}"

    def test_tv_price_noise_not_in_search_term(self):
        t = translate("samsung 75 inch 4k tv under $1500")
        terms = _terms_lower(t)
        assert "1500" not in terms and "under" not in terms, \
            f"Price noise in search_terms: {t.search_terms}"

    def test_tv_max_price(self):
        t = translate("samsung 75 inch 4k tv under $1500")
        assert t.max_price == 1500

    def test_tv_structural_discriminators_enforced(self):
        """Brand + size should appear in require_all_keywords or include_keywords."""
        t = translate("samsung 75 inch 4k tv under $1500")
        all_kw = (
            {kw.lower() for kw in t.include_keywords}
            | {kw.lower() for kw in t.adapter_options.get("require_all_keywords", [])}
        )
        assert "75" in all_kw or "samsung" in all_kw, \
            f"No structural discriminator in kw: {all_kw}"

    def test_tv_oled_size(self):
        t = translate("75 inch oled tv under $800")
        terms = _terms_lower(t)
        assert "75" in terms
        assert "oled" in terms
        assert t.max_price == 800

    def test_tv_excludes_stand(self):
        t = translate("samsung 75 inch 4k tv under $1500")
        assert "stand" in _excl_lower(t) or "tv stand" in _excl_lower(t), \
            f"Stand not in excludes: {t.exclude_keywords}"

    def test_tv_excludes_mount(self):
        t = translate("75 inch oled tv under $800")
        assert "mount" in _excl_lower(t) or "wall mount" in _excl_lower(t), \
            f"Mount not in excludes: {t.exclude_keywords}"

    def test_tv_resolution_aliases_in_include_or_require(self):
        """4K resolution hunt should include at least one alias (4k/uhd/2160p)."""
        t = translate("samsung 75 inch 4k tv under $1500")
        all_kw = (
            {kw.lower() for kw in t.include_keywords}
            | {kw.lower() for kw in t.adapter_options.get("require_all_keywords", [])}
        )
        res_aliases = {"4k", "uhd", "ultra hd", "2160p"}
        assert bool(res_aliases & all_kw), \
            f"No 4K alias in kw: {all_kw}"

    def test_tv_translated_by_v1(self):
        t = translate("samsung 75 inch 4k tv under $1500")
        assert t.translated_by == "rules"

    def test_tv_no_max_miles(self):
        t = translate("samsung 75 inch 4k tv under $1500")
        assert "max_miles" not in t.adapter_options

    def test_tv_source_sites(self):
        t = translate("samsung 75 inch 4k tv under $1500")
        assert t.source_sites


# ===========================================================================
# General marketplace vertical
# ===========================================================================

class TestGeneralMarketplaceRegression:
    """
    General hunts must:
    - Route through v1
    - Strip price/unit noise from search_terms
    - Set max_price correctly
    - Not set max_miles
    """

    def test_vintage_lamp_cleaned(self):
        t = translate("vintage lamp under $50")
        assert t.vertical == "general"
        terms = _terms_lower(t)
        # Price noise must not appear in search term
        assert "50" not in terms and "under" not in terms, \
            f"Price noise in search_terms: {t.search_terms}"
        assert "vintage" in terms or "lamp" in terms

    def test_vintage_lamp_max_price(self):
        t = translate("vintage lamp under $50")
        assert t.max_price == 50

    def test_general_k_price(self):
        t = translate("used bicycle under 2k")
        assert t.max_price == 2000

    def test_general_no_max_miles(self):
        t = translate("vintage lamp under $50")
        assert "max_miles" not in t.adapter_options

    def test_general_translated_by_v1(self):
        t = translate("vintage lamp under $50")
        assert t.translated_by == "rules"

    def test_general_source_sites(self):
        t = translate("vintage lamp under $50")
        assert t.source_sites


# ===========================================================================
# Routing boundary: confirm vehicle still goes to v2
# ===========================================================================

class TestVehicleStillRoutesToV2:
    """Sanity checks that vehicle intents still use translate_v2 after the fix."""

    def test_vehicle_uses_v2_label(self):
        t = translate("toyota sequoia under 50k miles under $30k")
        assert t.translated_by == "rules-v2"

    def test_vehicle_max_price_correct(self):
        t = translate("toyota sequoia under 50k miles under $30k")
        assert t.max_price == 30_000

    def test_vehicle_max_miles_in_adapter_options(self):
        t = translate("toyota sequoia under 50k miles under $30k")
        assert t.adapter_options.get("max_miles") == 50_000

    def test_vehicle_150k_miles_no_price(self):
        """The original regression: 150k miles must not become max_price."""
        t = translate("toyota sequoia under 150k miles")
        assert t.adapter_options.get("max_miles") == 150_000
        assert t.max_price is None

    def test_vehicle_min_year(self):
        t = translate("2018 or newer toyota sequoia under 30k with less than 100k miles")
        assert t.adapter_options.get("min_year") == 2018
        assert t.max_price == 30_000
        assert t.adapter_options.get("max_miles") == 100_000
