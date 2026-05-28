"""
engine/verticals.py

Lightweight vertical key constants and GPU tier list for Vulture.

Verticals represent broad marketplace categories used by the translator to
classify user intent and by the rules engine to apply category-specific
deterministic checks.

Design principles:
  - Simple string constants only.  No classes, no framework, no external deps.
  - Constants match the keys used in VERTICALS in llm_translator.py exactly.
  - GPU_TIER is an approximate performance ordering used for min_gpu_class
    enforcement.  When a card cannot be identified, the conservative default
    is to pass the listing through (never false-reject on ambiguous data).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Vertical key constants
# ---------------------------------------------------------------------------

VERTICAL_TV         = "tv_home_theater"
VERTICAL_COMPUTER   = "computer_parts"   # covers GPU, RAM, CPU, storage
VERTICAL_LAPTOPS    = "laptops_computers"
VERTICAL_VEHICLES   = "vehicles"
VERTICAL_FURNITURE  = "furniture_home"
VERTICAL_GENERAL    = "general"

# Convenience aliases for GPU and RAM sub-verticals.
# Both share the "computer_parts" vertical key; the sub-type is tracked in
# adapter_options (e.g. adapter_options["ddr_generation"] for RAM,
# adapter_options["min_gpu_class"] for GPU).
VERTICAL_GPU = VERTICAL_COMPUTER
VERTICAL_RAM = VERTICAL_COMPUTER

# All valid vertical keys (for validation in translator and tests).
ALL_VERTICALS: frozenset[str] = frozenset({
    VERTICAL_TV,
    VERTICAL_COMPUTER,
    VERTICAL_LAPTOPS,
    VERTICAL_VEHICLES,
    VERTICAL_FURNITURE,
    VERTICAL_GENERAL,
})


# ---------------------------------------------------------------------------
# GPU tier list — approximate performance order, lowest (index 0) to highest
# ---------------------------------------------------------------------------
#
# Used by rules.py for min_gpu_class enforcement:
#   - Extract GPU model from listing title (longest match wins).
#   - Look up both the title GPU and the min_gpu_class in GPU_TIER_RANK.
#   - If title rank < min rank → reject.
#   - If either model is unknown → conservative pass (never false-reject).
#
# Mixed AMD + NVIDIA on a single scale is inherently approximate near
# tier boundaries.  Cards at the same general performance level are placed
# conservatively (AMD card placed slightly higher when uncertain) to
# minimise false rejections.
# ---------------------------------------------------------------------------

GPU_TIER: tuple[str, ...] = (
    # --- NVIDIA GTX 10xx ---
    "GTX 1050",
    "GTX 1050 TI",
    "GTX 1060",
    "GTX 1070",
    "GTX 1070 TI",
    "GTX 1080",
    "GTX 1080 TI",
    # --- NVIDIA RTX 20xx ---
    "RTX 2060",
    "RTX 2060 SUPER",
    "RTX 2070",
    "RTX 2070 SUPER",
    "RTX 2080",
    "RTX 2080 SUPER",
    "RTX 2080 TI",
    # --- AMD RX 5xxx ---
    "RX 5600 XT",
    "RX 5700",
    "RX 5700 XT",
    # --- NVIDIA RTX 30xx (lower) ---
    "RTX 3060",
    "RTX 3060 TI",
    # --- AMD RX 6xxx (lower) ---
    "RX 6600",
    "RX 6600 XT",
    "RX 6650 XT",
    # --- NVIDIA RTX 30xx (mid) ---
    "RTX 3070",
    "RTX 3070 TI",
    # --- AMD RX 6xxx (mid) ---
    "RX 6700",
    "RX 6700 XT",
    "RX 6750 XT",
    # --- NVIDIA RTX 30xx (high) ---
    "RTX 3080",
    "RTX 3080 TI",
    # --- AMD RX 6xxx (high) ---
    "RX 6800",
    "RX 6800 XT",
    "RX 6900 XT",
    "RX 6950 XT",
    # --- NVIDIA RTX 30xx (top) ---
    "RTX 3090",
    "RTX 3090 TI",
    # --- AMD RX 7xxx (lower/mid) ---
    "RX 7600",
    "RX 7700 XT",
    "RX 7800 XT",
    # --- NVIDIA RTX 40xx (lower/mid) ---
    "RTX 4060",
    "RTX 4060 TI",
    "RTX 4070",
    "RTX 4070 SUPER",
    # --- AMD RX 7xxx (high) ---
    "RX 7900 GRE",
    "RX 7900 XT",
    # --- NVIDIA RTX 40xx (high) ---
    "RTX 4070 TI",
    "RTX 4070 TI SUPER",
    # --- AMD RX 7xxx (top) ---
    "RX 7900 XTX",
    # --- NVIDIA RTX 40xx (top) ---
    "RTX 4080",
    "RTX 4080 SUPER",
    "RTX 4090",
)

# Rank lookup: uppercase model string → integer rank (0 = lowest).
# Built once at import time so rule evaluation is O(1) per card lookup.
GPU_TIER_RANK: dict[str, int] = {
    model.upper(): rank for rank, model in enumerate(GPU_TIER)
}
