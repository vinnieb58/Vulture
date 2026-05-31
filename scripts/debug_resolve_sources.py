#!/usr/bin/env python3
"""Print resolved source_sites for sample intents (no adapter fetches)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("VULTURE_TRANSLATOR", "rules")

from engine.llm_translator import translate

_SAMPLES = (
    "rtx 4070 under 500",
    "2tb nvme ssd",
    "gaming laptop under 800",
    "macbook air",
)


def main() -> int:
    print("Resolved source_sites for sample intents\n")
    for intent in _SAMPLES:
        t = translate(intent)
        print(f"  intent : {intent!r}")
        print(f"  vertical: {t.vertical}")
        print(f"  sources : {t.source_sites}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
