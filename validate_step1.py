"""
validate_step1.py

Lightweight validation script for the Hunt model and hunt_repository.
Uses a temporary SQLite database so it never touches data/vulture.db.

Run from the project root with the venv active:
    python validate_step1.py
"""

import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Redirect engine.database to a temp DB *before* any repository imports
# ---------------------------------------------------------------------------
import engine.database as _db_module

_tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
_db_module.DB_PATH = Path(_tmp_dir.name) / "test_vulture.db"

# Now safe to import repository and model
from engine.database import init_db, save_listing
from engine.hunt_repository import (
    create_hunt,
    get_hunt_by_id,
    init_hunts_table,
    list_hunts,
    update_hunt,
    update_hunt_status,
)
from models.hunt import Hunt
from models.listing import Listing

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        print(f"  PASS  {label}")
        _passed += 1
    else:
        suffix = f": {detail}" if detail else ""
        print(f"  FAIL  {label}{suffix}")
        _failed += 1


def section(title: str) -> None:
    print(f"\n--- {title} ---")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

section("T01  init_hunts_table creates the hunts table")

init_hunts_table()

from engine.database import get_connection
with get_connection() as conn:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='hunts'"
    ).fetchone()
check("T01  hunts table exists after init_hunts_table()", row is not None)

# Idempotency — calling again must not raise
try:
    init_hunts_table()
    check("T01b init_hunts_table() is idempotent", True)
except Exception as e:
    check("T01b init_hunts_table() is idempotent", False, str(e))


section("T02  Hunt auto-generates hunt_id when not provided")

h1 = Hunt(name="auto_id_hunt")
check("T02  hunt_id is non-empty string", isinstance(h1.hunt_id, str) and len(h1.hunt_id) > 0)
check("T02b created_at is non-empty string", isinstance(h1.created_at, str) and len(h1.created_at) > 0)
check("T02c updated_at is non-empty string", isinstance(h1.updated_at, str) and len(h1.updated_at) > 0)


section("T03  create_hunt and get_hunt_by_id round-trip")

h2 = Hunt(
    name="gpu_hunt",
    category="gpu",
    source_sites=["craigslist"],
    search_terms=["gpu", "graphics card"],
    include_keywords=["3080", "4080"],
    exclude_keywords=["broken", "parts only"],
    max_price=450,
    location="houston",
    radius=50,
    status="active",
    created_by="test",
    notes="Validation test hunt",
    adapter_options={"limit": 20, "extra": True},
)
create_hunt(h2)

fetched = get_hunt_by_id(h2.hunt_id)
check("T03  fetched hunt is not None", fetched is not None)
check("T03b hunt_id matches", fetched.hunt_id == h2.hunt_id)
check("T03c name matches", fetched.name == "gpu_hunt")
check("T03d category matches", fetched.category == "gpu")
check("T03e max_price matches", fetched.max_price == 450)
check("T03f location matches", fetched.location == "houston")
check("T03g radius matches", fetched.radius == 50)
check("T03h status matches", fetched.status == "active")
check("T03i notes matches", fetched.notes == "Validation test hunt")
check("T03j created_at is preserved", fetched.created_at == h2.created_at)


section("T04  JSON-backed fields round-trip as Python types")

check("T04  source_sites is list", isinstance(fetched.source_sites, list))
check("T04b source_sites value", fetched.source_sites == ["craigslist"])
check("T04c search_terms value", fetched.search_terms == ["gpu", "graphics card"])
check("T04d include_keywords value", fetched.include_keywords == ["3080", "4080"])
check("T04e exclude_keywords value", fetched.exclude_keywords == ["broken", "parts only"])
check("T04f adapter_options is dict", isinstance(fetched.adapter_options, dict))
check("T04g adapter_options value", fetched.adapter_options == {"limit": 20, "extra": True})


section("T05  list_hunts — unfiltered and filtered by status")

# Insert a second hunt with different status
h3 = Hunt(name="monitor_hunt", status="paused")
create_hunt(h3)

all_hunts = list_hunts()
check("T05  list_hunts() returns all hunts", len(all_hunts) == 2)

active_hunts = list_hunts(status="active")
check("T05b list_hunts(status='active') returns only active", len(active_hunts) == 1)
check("T05c the active hunt is gpu_hunt", active_hunts[0].name == "gpu_hunt")

paused_hunts = list_hunts(status="paused")
check("T05d list_hunts(status='paused') returns only paused", len(paused_hunts) == 1)
check("T05e the paused hunt is monitor_hunt", paused_hunts[0].name == "monitor_hunt")

archived_hunts = list_hunts(status="archived")
check("T05f list_hunts(status='archived') returns empty list", archived_hunts == [])


section("T06  update_hunt_status")

original_created_at = fetched.created_at

result = update_hunt_status(h2.hunt_id, "paused")
check("T06  update_hunt_status returns True on valid id", result is True)

after_status = get_hunt_by_id(h2.hunt_id)
check("T06b status is now paused", after_status.status == "paused")
check("T06c created_at unchanged after status update", after_status.created_at == original_created_at)
check("T06d hunt_id unchanged after status update", after_status.hunt_id == h2.hunt_id)

result_missing = update_hunt_status("nonexistent-id", "archived")
check("T06e update_hunt_status returns False for unknown id", result_missing is False)


section("T07  update_hunt — mutable fields, created_at and hunt_id protected")

to_update = get_hunt_by_id(h2.hunt_id)
original_hunt_id = to_update.hunt_id
original_created_at = to_update.created_at

to_update.name = "gpu_hunt_v2"
to_update.max_price = 300
to_update.notes = "Updated in validation"
to_update.include_keywords = ["4090"]
to_update.adapter_options = {"limit": 5}

result = update_hunt(to_update)
check("T07  update_hunt returns True on valid id", result is True)

refreshed = get_hunt_by_id(h2.hunt_id)
check("T07b name updated", refreshed.name == "gpu_hunt_v2")
check("T07c max_price updated", refreshed.max_price == 300)
check("T07d notes updated", refreshed.notes == "Updated in validation")
check("T07e include_keywords updated", refreshed.include_keywords == ["4090"])
check("T07f adapter_options updated", refreshed.adapter_options == {"limit": 5})
check("T07g hunt_id not changed by update_hunt", refreshed.hunt_id == original_hunt_id)
check("T07h created_at not changed by update_hunt", refreshed.created_at == original_created_at)

result_missing = update_hunt(Hunt(name="ghost", hunt_id="nonexistent-id"))
check("T07i update_hunt returns False for unknown id", result_missing is False)


section("T08  Empty / default JSON fields on a minimal Hunt")

h_minimal = Hunt(name="minimal_hunt")
create_hunt(h_minimal)

minimal_fetched = get_hunt_by_id(h_minimal.hunt_id)
check("T08  source_sites defaults to []", minimal_fetched.source_sites == [])
check("T08b search_terms defaults to []", minimal_fetched.search_terms == [])
check("T08c include_keywords defaults to []", minimal_fetched.include_keywords == [])
check("T08d exclude_keywords defaults to []", minimal_fetched.exclude_keywords == [])
check("T08e adapter_options defaults to {}", minimal_fetched.adapter_options == {})
check("T08f max_price defaults to None", minimal_fetched.max_price is None)
check("T08g location defaults to None", minimal_fetched.location is None)
check("T08h status defaults to active", minimal_fetched.status == "active")


section("T09  Existing listings flow is untouched")

init_db()

with get_connection() as conn:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='listings'"
    ).fetchone()
check("T09  listings table created by init_db()", row is not None)

listing_a = Listing(
    source="craigslist",
    title="Dell 27 Monitor",
    price=150,
    location="Houston",
    link="https://houston.craigslist.org/test/1",
)
inserted = save_listing(listing_a)
check("T09b first insert returns True", inserted is True)

duplicate = save_listing(listing_a)
check("T09c duplicate insert returns False", duplicate is False)

listing_b = Listing(
    source="craigslist",
    title="HP Monitor",
    price=100,
    location="Houston",
    link="https://houston.craigslist.org/test/2",
)
save_listing(listing_b)

with get_connection() as conn:
    count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
check("T09d listings table has exactly 2 rows", count == 2)

with get_connection() as conn:
    hunt_count = conn.execute("SELECT COUNT(*) FROM hunts").fetchone()[0]
check("T09e hunts table unaffected by listings operations", hunt_count == 3)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print(f"\n{'=' * 40}")
print(f"  Results: {_passed} passed, {_failed} failed")
print(f"{'=' * 40}\n")

_tmp_dir.cleanup()

if _failed > 0:
    sys.exit(1)
