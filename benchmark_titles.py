"""
benchmark_titles.py

Manual review harness for the title-intelligence translator + rule engine.

Usage:
    python benchmark_titles.py

Shows, for each example prompt:
  - Extracted attributes (vertical, search terms, include/exclude keywords,
    max_price, adapter_options with all structured constraints)
  - Per-title pass/fail for a set of representative Craigslist-style listing titles

This is a dev tool — not part of the production runtime.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("VULTURE_TRANSLATOR", "rules")

from engine.llm_translator import translate, TranslationError
from engine.rules import matches_rules
from models.listing import Listing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sim_rules(t) -> dict:
    """Build the same rules dict that hunt_to_execution_dict would produce."""
    rules: dict = {}
    ao = t.adapter_options or {}
    if ao.get("min_price") is not None:
        rules["min_price"]           = int(ao["min_price"])
    if t.max_price is not None:
        rules["max_price"]           = t.max_price
    if t.include_keywords:
        rules["include_keywords"]    = t.include_keywords
    if t.exclude_keywords:
        rules["exclude_keywords"]    = t.exclude_keywords
    if ao.get("require_all_keywords"):
        rules["require_all_keywords"] = list(ao["require_all_keywords"])
    if ao.get("max_miles") is not None:
        rules["max_miles"]           = int(ao["max_miles"])
    if ao.get("min_capacity_gb") is not None:
        rules["min_capacity_gb"]     = int(ao["min_capacity_gb"])
    if ao.get("min_year") is not None:
        rules["min_year"]            = int(ao["min_year"])
    if ao.get("max_year") is not None:
        rules["max_year"]            = int(ao["max_year"])
    if ao.get("min_vram_gb") is not None:
        rules["min_vram_gb"]         = int(ao["min_vram_gb"])
    if ao.get("min_speed_mhz") is not None:
        rules["min_speed_mhz"]       = int(ao["min_speed_mhz"])
    return rules


def _fmt_rules(rules: dict) -> str:
    parts = []
    for k, v in sorted(rules.items()):
        if isinstance(v, list):
            parts.append(f"{k}=[{', '.join(str(x) for x in v[:3])}{'...' if len(v) > 3 else ''}]")
        else:
            parts.append(f"{k}={v}")
    return "  " + "\n  ".join(parts) if parts else "  (none)"


def run_case(label: str, intent: str, titles: list[tuple[str, int, bool]]):
    """
    intent  : natural-language hunt intent
    titles  : list of (title_text, price, expected_pass)
    """
    print(f"\n{'='*70}")
    print(f"PROMPT : {intent!r}")
    print(f"{'='*70}")

    try:
        t = translate(intent, location=None, max_price=None)
    except TranslationError as exc:
        print(f"  TRANSLATION ERROR: {exc}")
        return

    print(f"  Vertical     : {t.vertical}")
    print(f"  Search terms : {t.search_terms}")
    print(f"  Include kw   : {t.include_keywords}")
    excl_preview = t.exclude_keywords[:5]
    if len(t.exclude_keywords) > 5:
        excl_preview_str = str(excl_preview)[:-1] + f", ...+{len(t.exclude_keywords)-5} more]"
    else:
        excl_preview_str = str(excl_preview)
    print(f"  Exclude kw   : {excl_preview_str}")
    print(f"  Max price    : {t.max_price}")
    print(f"  Adapter opts : {t.adapter_options}")
    print(f"  Hunt name    : {t.name}")

    rules = _sim_rules(t)
    print(f"\n  Active rules:")
    print(_fmt_rules(rules))

    print(f"\n  Title filter results:")
    all_ok = True
    for title_text, price, expected_pass in titles:
        listing = Listing("craigslist", title_text, price, "Houston", "http://example.com")
        actual  = matches_rules(listing, rules)
        ok      = (actual == expected_pass)
        all_ok  = all_ok and ok
        symbol  = "OK" if ok else "!!"
        verdict = "PASS" if actual else "FAIL"
        exp_str = "PASS" if expected_pass else "FAIL"
        note    = "" if ok else f"  *** expected {exp_str} ***"
        print(f"    [{symbol}] {verdict}  ${price:<6}  {title_text[:65]}{note}")

    print(f"\n  {'ALL CORRECT' if all_ok else 'SOME MISMATCHES'}")


# ---------------------------------------------------------------------------
# Benchmark cases
# ---------------------------------------------------------------------------

CASES = [

    # ------------------------------------------------------------------
    # TV — brand + panel + size + resolution
    # ------------------------------------------------------------------
    (
        "Samsung 75 inch OLED 4K TV under $1500",
        [
            # PASS: brand, size, panel all present; price ok
            ("Samsung 75 inch OLED 4K Smart TV 2023 model", 1200, True),
            # FAIL: wrong brand
            ("LG 75 inch OLED 4K TV C3 series",             1100, False),
            # FAIL: missing "oled" (just LED)
            ("Samsung 75 inch LED 4K TV",                   900,  False),
            # FAIL: wrong size
            ("Samsung 65 inch OLED 4K TV",                  800,  False),
            # FAIL: TV stand (excluded)
            ("Samsung 75 TV stand / bracket",               50,   False),
            # PASS: price at limit
            ("Samsung 75 OLED TV 4K UHD Smart",             1500, True),
            # FAIL: price over limit
            ("Samsung 75 OLED TV 4K brand new",             1600, False),
            # FAIL: no resolution keyword in title — resolution enforcement (new behaviour)
            ("Samsung 75 OLED Smart TV 2022 model",         900,  False),
        ],
    ),

    (
        "55 inch LG TV under $300",
        [
            ("LG 55 inch 4K Smart TV",    280, True),
            ("LG 65 inch 4K Smart TV",    250, False),   # wrong size digit
            ("Samsung 55 inch 4K TV",     250, False),   # wrong brand
            ("LG 55 inch TV remote only", 30,  False),   # 'remote' excluded
            ("LG 55 TV",                  300, True),
            ("LG 55 TV",                  310, False),   # over price
        ],
    ),

    # ------------------------------------------------------------------
    # TV — size + resolution only (no brand)
    # ------------------------------------------------------------------
    (
        "75 inch 4K TV under $500",
        [
            ("Samsung 75 inch 4K UHD Smart TV", 450, True),
            ("LG 75 OLED 4K TV",                480, True),
            ("65 inch 4K TV",                   400, False),  # wrong size digit
            ("75 inch 4K TV mount bracket",     30,  False),  # excluded
            ("75 inch 4K TV",                   500, True),
            ("75 inch 4K TV",                   501, False),  # over price
            # Resolution-enforcement tests (new behaviour):
            ("75 inch Smart TV",                400, False),  # no 4K/UHD/2160p → FAIL
            ("75 inch UHD Smart TV",            400, True),   # UHD is a 4K alias → PASS
            ("75 inch 2160p TV",                400, True),   # 2160p is a 4K alias → PASS
        ],
    ),

    # ------------------------------------------------------------------
    # Vehicles — make + model + year + mileage + price
    # ------------------------------------------------------------------
    (
        "Toyota RAV4 newer than 2016 under $20k and less than 100k miles",
        [
            # PASS: 2019 >= 2016, 85k < 100k, price ok
            ("2019 Toyota RAV4 85k miles excellent",        18000, True),
            # FAIL: year too old
            ("2013 Toyota RAV4 60k miles one owner",        12000, False),
            # FAIL: mileage too high
            ("2018 Toyota RAV4 115k miles runs great",      15000, False),
            # FAIL: price over limit
            ("2020 Toyota RAV4 50k miles loaded",           22000, False),
            # PASS: no mileage in title → conservative pass
            ("2017 Toyota RAV4 clean title",                17000, True),
            # FAIL: no year in title but model missing → fails include keyword
            ("Toyota Highlander 2018",                      16000, False),
            # FAIL: parts exclusion
            ("Toyota RAV4 part out 2017",                   500,   False),
            # PASS: year exactly at boundary (2016 == min_year)
            ("2016 Toyota RAV4 80k miles",                  14000, True),
        ],
    ),

    (
        "Porsche 911 under $30k newer than 2005 less than 80k miles",
        [
            ("2010 Porsche 911 65k miles",  28000, True),
            ("2003 Porsche 911 50k miles",  25000, False),  # year too old
            ("2012 Porsche 911 90k miles",  27000, False),  # mileage too high
            ("2008 Porsche 911 70k miles",  32000, False),  # price too high
            ("Porsche 911 mini sculpture",  250,   False),  # collectible excluded
            ("2007 Porsche 911 Carrera",    26000, True),   # no mileage → pass
        ],
    ),

    # ------------------------------------------------------------------
    # Vehicles — make misspelling + parts rejection
    # ------------------------------------------------------------------
    (
        "hyndai elantra under 15000",   # intentional typo: hyndai → hyundai
        [
            ("2019 Hyundai Elantra 60k miles",       14000, True),
            ("2017 Hyundai Elantra SE 80k miles",    11000, True),
            # FAIL: year mismatch is NOT tested here; focus is on vertical + include
            ("2020 Kia Soul",                        13000, False),  # wrong make/model
            ("Hyundai Elantra headlight assembly",    120,  False),  # parts excluded
            ("Hyundai Elantra part out 2020",         500,  False),  # parts out excluded
        ],
    ),

    (
        "kia telluride under 40000",
        [
            ("2021 Kia Telluride EX AWD 45k miles",  38000, True),
            ("2020 Kia Telluride SX Limited",         39000, True),
            # FAIL: parts listings that previously slipped through
            ("2020/2022 Kia Telluride Front Right Passenger Headlight Led",
                                                       150,  False),  # headlight excluded
            ("Kia Telluride Catalytic Converter OEM",  280,  False),  # cat conv excluded
            ("Kia Telluride Alternator 2021",          95,   False),  # alternator excluded
            ("Kia Telluride Tailgate Handle",          45,   False),  # tailgate excluded
        ],
    ),

    # ------------------------------------------------------------------
    # GPU — model + VRAM
    # ------------------------------------------------------------------
    (
        "RTX 4080 GPU with at least 16gb vram under $700",
        [
            ("EVGA RTX 4080 16GB GDDR6X",        680, True),
            ("Gigabyte RTX 4080 12GB Gaming OC", 650, False),  # 12 < 16 VRAM
            ("MSI RTX 4080 16GB VRAM",           690, True),
            ("RTX 4080 GPU for parts",            500, False), # excluded
            ("RTX 4080 16GB",                    710, False),  # over price
        ],
    ),

    (
        "RTX 3080 GPU under $400",
        [
            # No VRAM constraint (user didn't say "vram") → only price + include_kw
            ("EVGA RTX 3080 10GB",  380, True),
            ("MSI RTX 3080 Ti 12GB", 390, True),   # Ti still has "3080" in title
            ("RTX 3070 GPU",         350, False),  # 3070 ≠ 3080
            ("RTX 3080 GPU",         410, False),  # over price
            ("RTX 3080 GPU",         400, True),
        ],
    ),

    # ------------------------------------------------------------------
    # RAM — type + capacity
    # ------------------------------------------------------------------
    (
        "DDR5 desktop RAM at least 32GB under $100",
        [
            ("32GB DDR5 5600MHz Corsair",     90,  True),
            ("16GB DDR5 6000MHz Kingston",    50,  False),  # 16 < 32
            ("64GB DDR5 kit",                 95,  True),
            ("2x16GB DDR5 6400",              80,  True),   # 2×16=32 >= 32
            ("32GB DDR5",                     105, False),  # over price
            ("32GB DDR4 3200MHz",             70,  False),  # wrong type (no ddr5)
            ("DDR5 32GB SODIMM laptop",       60,  False),  # sodimm excluded
        ],
    ),

    (
        "ddr4 ram more than 8gb under $40",
        [
            ("16GB DDR4 2400MHz desktop",    35,  True),
            ("4GB DDR4",                     10,  False),  # 4 < 8
            ("8GB DDR4 3200",                30,  True),   # 8 == 8 (not strictly less)
            ("2x8GB DDR4 Corsair Vengeance", 38,  True),   # 2×8=16 >= 8
            ("32GB DDR4 kit",                45,  False),  # over price
            ("16GB DDR4 SODIMM laptop",      25,  False),  # sodimm excluded
        ],
    ),

]


if __name__ == "__main__":
    total_cases  = 0
    total_pass   = 0

    for prompt, title_cases in CASES:
        run_case(prompt, prompt, title_cases)
        # Count correctness
        try:
            t = translate(prompt, location=None, max_price=None)
            rules = _sim_rules(t)
            for title_text, price, expected in title_cases:
                listing = Listing("craigslist", title_text, price, "Houston", "http://x")
                actual  = matches_rules(listing, rules)
                total_cases += 1
                if actual == expected:
                    total_pass += 1
        except TranslationError:
            pass

    print(f"\n{'='*70}")
    print(f"OVERALL: {total_pass}/{total_cases} correct")
    if total_pass == total_cases:
        print("ALL PASS")
    else:
        print(f"FAILURES: {total_cases - total_pass}")
