#!/usr/bin/env python3
"""
Idempotently seed the macbook_a2338_screen manual hunt into data/vulture.db.

Usage (from repo root, venv active):
    python scripts/seed_macbook_a2338_screen.py
    python scripts/seed_macbook_a2338_screen.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.database import init_db
from engine.hunt_repository import init_hunts_table
from engine.hunt_service import create_hunt, edit_hunt, list_hunts
from engine.manual_hunt_specs import MACBOOK_A2338_SCREEN_NAME, MACBOOK_A2338_SCREEN_SPEC


def _find_by_name(name: str):
    for hunt in list_hunts():
        if hunt.name == name:
            return hunt
    return None


def seed_macbook_a2338_screen(*, dry_run: bool = False, created_by: str = "seed_macbook_a2338_screen"):
    init_db()
    init_hunts_table()

    spec = dict(MACBOOK_A2338_SCREEN_SPEC)
    spec["created_by"] = created_by

    existing = _find_by_name(MACBOOK_A2338_SCREEN_NAME)
    if existing:
        if dry_run:
            print(f"DRY RUN: would update hunt '{MACBOOK_A2338_SCREEN_NAME}' ({existing.hunt_id})")
            return existing
        hunt = edit_hunt(existing.hunt_id, **{k: v for k, v in spec.items() if k != "name"})
        print(f"Updated hunt '{hunt.name}' ({hunt.hunt_id})")
        return hunt

    if dry_run:
        print(f"DRY RUN: would create hunt '{MACBOOK_A2338_SCREEN_NAME}'")
        return None

    hunt = create_hunt(**spec)
    print(f"Created hunt '{hunt.name}' ({hunt.hunt_id})")
    return hunt


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed macbook_a2338_screen hunt")
    parser.add_argument("--dry-run", action="store_true", help="Print action without writing")
    args = parser.parse_args()
    seed_macbook_a2338_screen(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
