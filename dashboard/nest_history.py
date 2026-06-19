"""Dashboard import shim for Nest history helpers.

Canonical implementation: ``kestrel.nest_history``. Docker build copies that
module here so the dashboard container does not need the full kestrel package.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kestrel.nest_history import *  # noqa: F403
