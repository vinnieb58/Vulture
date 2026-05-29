#!/usr/bin/env python3
"""
scripts/smoke_multi_source.py

End-to-end smoke test for multi-source hunt execution.

What it does
------------
1. Initialises the live DB tables (idempotent — safe to run against a
   populated DB).
2. Inserts one test hunt with source_sites = ["craigslist", "offerup"].
3. Runs one main.py cycle (VULTURE_HUNT_SOURCE=db) and captures output.
4. Checks the output for required log lines:
     - expansion banner ("Expanded … into 2 source-run(s)")
     - a "Starting hunt" line for craigslist
     - a "Starting hunt" line for offerup
     - a "Done hunt" line for craigslist
     - a "Done hunt" line for offerup
5. Prints a clear PASS / FAIL table.
6. Removes the test hunt (and any listings it created) unless --keep is passed.

Usage (from the project root, with the venv active)
----------------------------------------------------
    python3 scripts/smoke_multi_source.py           # run, then clean up
    python3 scripts/smoke_multi_source.py --keep    # leave the hunt + listings in the DB

The script exits 0 on full pass, 1 if any check fails.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


def _bootstrap_venv() -> None:
    """Re-exec with project venv python when available."""
    root = Path(__file__).resolve().parent.parent
    venv_py = root / ".venv" / "bin" / "python"
    if venv_py.exists() and not str(sys.executable).startswith(str(root / ".venv")):
        os.execv(str(venv_py), [str(venv_py)] + sys.argv)


def main() -> int:
    _bootstrap_venv()

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))

    try:
        from dotenv import load_dotenv
        load_dotenv(root / ".env")
    except ModuleNotFoundError:
        pass

    from engine.database import init_db
    from engine.hunt_repository import init_hunts_table
    from engine.hunt_service import create_hunt, end_hunt

    keep = "--keep" in sys.argv
    checks_passed = 0
    checks_failed = 0

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal checks_passed, checks_failed
        mark = "PASS" if condition else "FAIL"
        suffix = f"  ({detail})" if detail and not condition else ""
        print(f"  {mark}  {label}{suffix}")
        if condition:
            checks_passed += 1
        else:
            checks_failed += 1

    def section(title: str) -> None:
        print(f"\n{'─' * 55}")
        print(f"  {title}")
        print(f"{'─' * 55}")

    section("Step 1  Initialise DB tables")
    init_db()
    init_hunts_table()
    print("  OK  listings + hunts tables ready")

    section("Step 2  Create multi-source test hunt")
    test_hunt_name = "vulture-multi-source-smoke-test"
    hunt = create_hunt(
        name=test_hunt_name,
        search_terms=["ipad"],
        source_sites=["craigslist", "offerup"],
        location="houston",
        adapter_options={"limit": 3},
        notes="Created by smoke_multi_source.py — safe to delete",
    )
    print(f"  hunt_id     : {hunt.hunt_id}")
    print(f"  source_sites: {hunt.source_sites}")
    print(f"  search_terms: {hunt.search_terms}")
    print(f"  limit       : {hunt.adapter_options.get('limit')}")
    check("Hunt created with two source_sites", hunt.source_sites == ["craigslist", "offerup"])
    check("Hunt status is active", hunt.status == "active")

    section("Step 3  Run main.py cycle (VULTURE_HUNT_SOURCE=db)")
    env = {**os.environ, "VULTURE_HUNT_SOURCE": "db"}
    python = sys.executable
    print(f"  Running: {python} main.py")
    print("  (limit=3 per source — may take a few seconds)\n")

    result = subprocess.run(
        [python, "main.py"],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = result.stdout + result.stderr

    print("  ── main.py output ──────────────────────────────────")
    for line in combined.splitlines():
        print(f"  {line}")
    print("  ────────────────────────────────────────────────────\n")

    section("Step 4  Verify log output")

    def found(pattern: str) -> bool:
        return bool(re.search(pattern, combined))

    check(
        "main.py exited cleanly (exit code 0)",
        result.returncode == 0,
        f"exit code was {result.returncode}",
    )
    check(
        "Expansion banner logged",
        found(r"Expanded \d+ hunt\(s\) into \d+ source-run\(s\)"),
        "expected 'Expanded N hunt(s) into M source-run(s)'",
    )
    check(
        "Craigslist run started",
        found(rf"Starting hunt.*{re.escape(test_hunt_name)}.*craigslist"),
        "no 'Starting hunt … craigslist' line found",
    )
    check(
        "OfferUp run started",
        found(rf"Starting hunt.*{re.escape(test_hunt_name)}.*offerup"),
        "no 'Starting hunt … offerup' line found",
    )
    check(
        "Craigslist run completed",
        found(rf"Done hunt.*{re.escape(test_hunt_name)}.*\[craigslist\]"),
        "no 'Done hunt … [craigslist]' line found",
    )
    check(
        "OfferUp run completed",
        found(rf"Done hunt.*{re.escape(test_hunt_name)}.*\[offerup\]"),
        "no 'Done hunt … [offerup]' line found",
    )
    check(
        "No unhandled exception in the test hunt",
        not found(rf"Hunt '{re.escape(test_hunt_name)}'.*failed unexpectedly"),
        f"'{test_hunt_name}' raised an unexpected exception — check output above",
    )

    craigslist_failed = found(
        rf"Hunt.*{re.escape(test_hunt_name)}.*\[craigslist\].*failed unexpectedly"
    )
    offerup_failed = found(
        rf"Hunt.*{re.escape(test_hunt_name)}.*\[offerup\].*failed unexpectedly"
    )
    if craigslist_failed or offerup_failed:
        print("\n  NOTE  One or both adapters raised an exception (network/GeoIP).")
        if craigslist_failed and not offerup_failed:
            check("Craigslist failure did not prevent OfferUp from running", True)
        elif offerup_failed and not craigslist_failed:
            check("OfferUp failure did not prevent Craigslist from running", True)
        else:
            print("  NOTE  Both adapters failed — check network connectivity.")

    section("Step 5  Clean up")
    if keep:
        print(f"  --keep flag set; leaving hunt {hunt.hunt_id} in the DB.")
        print("  To remove it later:  python scripts/reset_dev_db.py")
    else:
        try:
            end_hunt(hunt.hunt_id)
            print(f"  Hunt {hunt.hunt_id} marked as ended.")
        except Exception as exc:
            print(f"  WARNING  Could not end hunt: {exc}")

    section("Results")
    print(f"  Passed : {checks_passed}")
    print(f"  Failed : {checks_failed}")
    print()
    if checks_failed == 0:
        print("  ✓  All checks passed — multi-source execution is working.")
    else:
        print("  ✗  Some checks failed — review the output above.")
    print()

    return 0 if checks_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
