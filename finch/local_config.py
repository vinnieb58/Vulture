"""Finch local config — non-secret settings stored under data/."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from finch.config import DATA_DIR

FINCH_CONFIG_PATH = Path(
    os.getenv("FINCH_CONFIG_PATH", str(DATA_DIR / "finch_config.json"))
)


def load_finch_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or FINCH_CONFIG_PATH
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def get_saved_location_id(config_path: Path | None = None) -> str | None:
    cfg = load_finch_config(config_path)
    value = str(cfg.get("kroger_location_id", "")).strip()
    return value or None


def resolve_location_id(config_path: Path | None = None) -> str | None:
    """Return location ID from .env first, then data/finch_config.json."""
    env_val = os.getenv("FINCH_KROGER_LOCATION_ID", "").strip()
    if env_val:
        return env_val
    return get_saved_location_id(config_path)


def save_location_config(
    location_id: str,
    *,
    store_name: str | None = None,
    store_address: str | None = None,
    saved_from_zip: str | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Persist preferred Kroger store (non-secret) to data/finch_config.json."""
    path = config_path or FINCH_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = load_finch_config(path)
    payload["kroger_location_id"] = location_id.strip()
    if store_name:
        payload["store_name"] = store_name
    if store_address:
        payload["store_address"] = store_address
    if saved_from_zip:
        payload["saved_from_zip"] = saved_from_zip
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    return payload
