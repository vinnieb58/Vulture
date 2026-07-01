"""Load shared probe helpers from experiments/concerts/probe_common.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PROBE_DIR = Path(__file__).resolve().parents[2] / "experiments" / "concerts"
_PROBE_PATH = _PROBE_DIR / "probe_common.py"

if str(_PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(_PROBE_DIR))

if "probe_common" not in sys.modules:
    spec = importlib.util.spec_from_file_location("probe_common", _PROBE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_common"] = module
    spec.loader.exec_module(module)

from probe_common import (  # noqa: E402
    BROWSER_HEADERS,
    NormalizedEvent,
    build_normalized_event,
    classify_genre_signal,
    classify_seatgeek_taxonomies,
    http_get_json,
    make_event_dedupe_key,
    resolve_date_window,
    ticketmaster_datetime,
)

__all__ = [
    "BROWSER_HEADERS",
    "NormalizedEvent",
    "build_normalized_event",
    "classify_genre_signal",
    "classify_seatgeek_taxonomies",
    "http_get_json",
    "make_event_dedupe_key",
    "resolve_date_window",
    "ticketmaster_datetime",
]
