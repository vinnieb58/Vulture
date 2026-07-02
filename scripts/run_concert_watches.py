#!/usr/bin/env python3
"""One-shot concert watch cycle — run via systemd timer or cron on Raven."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Repo root on sys.path — required when invoked as scripts/run_concert_watches.py
# (Python puts the script directory on sys.path, not WorkingDirectory).
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv()

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/concert_watches.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

from engine.concerts.repository import init_concert_tables  # noqa: E402
from engine.concerts.watch_runner import run_concert_watches  # noqa: E402


def main() -> int:
    init_concert_tables()
    summary = run_concert_watches()
    print(summary)
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
