"""
Finch configuration — paths and operational settings only.

Secrets (Kroger client ID/secret, tokens) belong in the repo-root .env file
and are loaded only by finch.kroger_client at runtime.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(os.getenv("FINCH_PROJECT_ROOT", ".")).resolve()
DATA_DIR = Path(os.getenv("FINCH_DATA_DIR", str(PROJECT_ROOT / "data")))
LOGS_DIR = Path(os.getenv("FINCH_LOGS_DIR", str(PROJECT_ROOT / "logs")))

ALIASES_DB_PATH = Path(
    os.getenv("FINCH_ALIASES_DB_PATH", str(DATA_DIR / "finch_aliases.db"))
)
DEFAULT_ALIASES_YAML = Path(
    os.getenv(
        "FINCH_DEFAULT_ALIASES_YAML",
        str(PROJECT_ROOT / "finch" / "data" / "default_aliases.yaml"),
    )
)

KROGER_BASE_URL = os.getenv("FINCH_KROGER_BASE_URL", "https://api.kroger.com").rstrip("/")
KROGER_LOCATION_ID = os.getenv("FINCH_KROGER_LOCATION_ID", "").strip()
KROGER_CART_MODALITY = os.getenv("FINCH_KROGER_CART_MODALITY", "pickup").strip() or "pickup"

# When true, add_to_cart calls are allowed (still no checkout). Default false.
FINCH_LIVE_CART = os.getenv("FINCH_LIVE_CART", "").strip().lower() in ("1", "true", "yes")
