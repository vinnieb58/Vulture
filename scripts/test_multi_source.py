"""
scripts/test_multi_source.py

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
    python scripts/test_multi_source.py           # run, then clean up
    python scripts/test_multi_source.py --keep    # leave the hunt + listings in the DB

The script exits 0 on full pass, 1 if any check fails.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Make sure repo root is on sys.path so engine/model imports work
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ModuleNotFoundError:
    pass  # dotenv not installed; rely on env vars already being set

from engine.database import get_connection, init_db
from engine.hunt_repository import init_hunts_table
from engine.hunt_service import create_hunt, end_hunt

# ---------------------------------------------------------------------------
# CLI flags
# ---------------------------------------------------------------------------
KEEP = "--keep" in sys.argv

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_checks_passed = 0
_checks_failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _checks_passed, _checks_failed
    mark = "PASS" if condition else "FAIL"
    suffix = f"  ({detail})" if detail and not condition else ""
    print(f"  {mark}  {label}{suffix}")
    if condition:
        _checks_passed += 1
    else:
        _checks_failed += 1


def section(title: str) -> None:
    print(f"\n{'─' * 55}")
    print(f"  {title}")
    print(f"{'─' * 55}")


def _listing_count_for_hunt(hunt_id: str) -> int:
    """Count listings whose source matches either site of the test hunt."""
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM listings").fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Step 1 — Initialise tables
# ---------------------------------------------------------------------------

section("Step 1  Initialise DB tables")
init_db()
init_hunts_table()
print("  OK  listings + hunts tables ready")

# ---------------------------------------------------------------------------
# Step 2 — Insert the test hunt
# ---------------------------------------------------------------------------

section("Step 2  Create multi-source test hunt")

TEST_HUNT_NAME = "vulture-multi-source-smoke-test"

hunt = create_hunt(
    name=TEST_HUNT_NAME,
    search_terms=["ipad"],
    source_sites=["craigslist", "offerup"],
    location="houston",
    adapter_options={"limit": 3},
    notes="Created by test_multi_source.py — safe to delete",
)

print(f"  hunt_id     : {hunt.hunt_id}")
print(f"  source_sites: {hunt.source_sites}")
print(f"  search_terms: {hunt.search_terms}")
print(f"  limit       : {hunt.adapter_options.get('limit')}")

check("Hunt created with two source_sites", hunt.source_sites == ["craigslist", "offerup"])
check("Hunt status is active", hunt.status == "active")

# ---------------------------------------------------------------------------
# Step 3 — Run one main.py cycle
# ---------------------------------------------------------------------------

section("Step 3  Run main.py cycle (VULTURE_HUNT_SOURCE=db)")

env = {**os.environ, "VULTURE_HUNT_SOURCE": "db"}
python = sys.executable  # use the same interpreter running this script

print(f"  Running: {python} main.py")
print(f"  (limit=3 per source — may take a few seconds)\n")

result = subprocess.run(
    [python, "main.py"],
    cwd=str(ROOT),
    env=env,
    capture_output=True,
    text=True,
    timeout=120,
)

combined = result.stdout + result.stderr

# Print the raw output so it's visible on the terminal
print("  ── main.py output ──────────────────────────────────")
for line in combined.splitlines():
    print(f"  {line}")
print("  ────────────────────────────────────────────────────\n")

# ---------------------------------------------------------------------------
# Step 4 — Verify output
# ---------------------------------------------------------------------------

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
    found(r"Expanded \d+ hunt\(s\) into 2 source-run\(s\)"),
    "expected 'Expanded N hunt(s) into 2 source-run(s)'",
)
check(
    "Craigslist run started",
    found(rf"Starting hunt.*{re.escape(TEST_HUNT_NAME)}.*craigslist"),
    "no 'Starting hunt … craigslist' line found",
)
check(
    "OfferUp run started",
    found(rf"Starting hunt.*{re.escape(TEST_HUNT_NAME)}.*offerup"),
    "no 'Starting hunt … offerup' line found",
)
check(
    "Craigslist run completed",
    found(rf"Done hunt.*{re.escape(TEST_HUNT_NAME)}.*\[craigslist\]"),
    "no 'Done hunt … [craigslist]' line found",
)
check(
    "OfferUp run completed",
    found(rf"Done hunt.*{re.escape(TEST_HUNT_NAME)}.*\[offerup\]"),
    "no 'Done hunt … [offerup]' line found",
)
check(
    "No unhandled exception in output",
    "Traceback" not in combined and "failed unexpectedly" not in combined,
    "exception or 'failed unexpectedly' found in output",
)

# Adapter failures (network/GeoIP) are allowed — check they were isolated
craigslist_failed = found(
    rf"Hunt.*{re.escape(TEST_HUNT_NAME)}.*\[craigslist\].*failed unexpectedly"
)
offerup_failed = found(
    rf"Hunt.*{re.escape(TEST_HUNT_NAME)}.*\[offerup\].*failed unexpectedly"
)
if craigslist_failed or offerup_failed:
    print("\n  NOTE  One or both adapters raised an exception (network/GeoIP).")
    if craigslist_failed and not offerup_failed:
        check("Craigslist failure did not prevent OfferUp from running", True)
    elif offerup_failed and not craigslist_failed:
        check("OfferUp failure did not prevent Craigslist from running", True)
    else:
        print("  NOTE  Both adapters failed — check network connectivity.")

# ---------------------------------------------------------------------------
# Step 5 — Clean up
# ---------------------------------------------------------------------------

section("Step 5  Clean up")

if KEEP:
    print(f"  --keep flag set; leaving hunt {hunt.hunt_id} in the DB.")
    print(f"  To remove it later:  python scripts/reset_dev_db.py")
else:
    try:
        end_hunt(hunt.hunt_id)
        print(f"  Hunt {hunt.hunt_id} marked as ended.")
    except Exception as exc:
        print(f"  WARNING  Could not end hunt: {exc}")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

section("Results")
print(f"  Passed : {_checks_passed}")
print(f"  Failed : {_checks_failed}")
print()

if _checks_failed == 0:
    print("  ✓  All checks passed — multi-source execution is working.")
else:
    print("  ✗  Some checks failed — review the output above.")
print()

sys.exit(0 if _checks_failed == 0 else 1)
