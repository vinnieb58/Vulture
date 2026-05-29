#!/usr/bin/env python3
"""Audit active hunts for suspicious search/filter configuration."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.hunt_repository import init_hunts_table, list_hunts
from models.hunt import Hunt

_STRUCTURED_OPTION_KEYS = (
    "min_price", "max_miles", "min_year", "max_year", "min_capacity_gb",
    "min_speed_mhz", "min_vram_gb", "min_gpu_class", "min_size_inches",
    "max_size_inches", "ddr_generation", "card_only", "hunt_subtype",
    "make", "model", "limit",
)

_BROAD_COMPUTER_TERMS = frozenset({
    "computer", "electronics", "gaming", "pc", "parts", "hardware",
})

_PARTS_SALVAGE_HINTS = frozenset({
    "part", "parts", "salvage", "scrap", "engine", "transmission",
})


def _is_vehicle_hunt(hunt: Hunt) -> bool:
    category = (hunt.category or "").lower()
    return category.replace(" ", "_") == "vehicles" or "vehicle" in category


def _is_computer_parts_hunt(hunt: Hunt) -> bool:
    category = (hunt.category or "").lower()
    return "computer" in category or "parts" in category


def _is_parts_or_salvage_hunt(hunt: Hunt) -> bool:
    blob = " ".join(
        [hunt.name or ""] + list(hunt.search_terms or []) + list(hunt.include_keywords or [])
    ).lower()
    return any(h in blob for h in _PARTS_SALVAGE_HINTS)


def _structured_options(hunt: Hunt) -> dict:
    opts = hunt.adapter_options or {}
    return {k: opts[k] for k in _STRUCTURED_OPTION_KEYS if k in opts}


def _duplicate_key(hunt: Hunt) -> tuple:
    sites = tuple(sorted(str(s).lower() for s in (hunt.source_sites or [])))
    terms = tuple(sorted(str(t).lower() for t in (hunt.search_terms or [])))
    return (hunt.name.lower(), terms, sites)


def _audit_warnings(hunt: Hunt) -> list[str]:
    warnings: list[str] = []
    if not hunt.search_terms:
        warnings.append("empty search_terms")
    if _is_vehicle_hunt(hunt):
        if (
            hunt.max_price is not None
            and hunt.max_price < 10_000
            and not _is_parts_or_salvage_hunt(hunt)
        ):
            warnings.append(
                f"vehicle hunt max_price=${hunt.max_price} below $10,000 "
                "(likely misconfigured unless parts/salvage)"
            )
        sites = [str(s).lower() for s in (hunt.source_sites or [])]
        if len(sites) != len(set(sites)):
            warnings.append(f"duplicate source_sites entries: {hunt.source_sites}")
    if _is_computer_parts_hunt(hunt):
        for term in hunt.search_terms or []:
            if str(term).strip().lower() in _BROAD_COMPUTER_TERMS:
                warnings.append(f"overly broad computer_parts search term: {term!r}")
    return warnings


def _proposed_fixes(hunt: Hunt) -> list[tuple[str, Hunt]]:
    fixes: list[tuple[str, Hunt]] = []
    new_hunt = Hunt(**{**hunt.__dict__})
    sites = list(new_hunt.source_sites or [])
    deduped: list[str] = []
    seen: set[str] = set()
    for site in sites:
        key = str(site).strip().lower()
        if key and key not in seen:
            deduped.append(str(site).strip())
            seen.add(key)
    if deduped != sites:
        fixes.append((f"dedupe source_sites {sites!r} -> {deduped!r}",
                      Hunt(**{**new_hunt.__dict__, "source_sites": deduped})))
        new_hunt.source_sites = deduped
    if not new_hunt.search_terms and new_hunt.include_keywords:
        terms = [str(new_hunt.include_keywords[0])]
        fixes.append((f"populate search_terms from first include_keyword -> {terms!r}",
                      Hunt(**{**new_hunt.__dict__, "search_terms": terms})))
    return fixes


def _print_hunt(hunt: Hunt, warnings: list[str]) -> None:
    print("-" * 72)
    print(f"name          : {hunt.name}")
    print(f"hunt_id       : {hunt.hunt_id}")
    print(f"status        : {hunt.status}")
    print(f"category      : {hunt.category or '—'}")
    print(f"source_sites  : {hunt.source_sites}")
    print(f"search_terms  : {hunt.search_terms}")
    print(f"include_kw    : {hunt.include_keywords}")
    excl = hunt.exclude_keywords or []
    print(f"exclude_kw    : {excl[:12]}{' ...' if len(excl) > 12 else ''}")
    print(f"min_price     : {(hunt.adapter_options or {}).get('min_price', '—')}")
    print(f"max_price     : {hunt.max_price if hunt.max_price is not None else '—'}")
    structured = _structured_options(hunt)
    print(f"adapter_opts  : {json.dumps(structured, sort_keys=True) if structured else '{}'}")
    for w in warnings or ["no suspicious combinations detected"]:
        print(f"  [{'WARN' if warnings else 'OK'}] {w}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit active Vulture hunts")
    parser.add_argument("--fix", action="store_true", help="Apply conservative fixes")
    parser.add_argument("--all-statuses", action="store_true", help="Include non-active hunts")
    args = parser.parse_args()

    init_hunts_table()
    status = None if args.all_statuses else "active"
    hunts = list_hunts(status=status)
    if not hunts:
        print(f"No hunts found (status filter={status or 'any'}).")
        return 0

    print(f"Auditing {len(hunts)} hunt(s) (status filter={status or 'any'})")
    dup_index: dict[tuple, list[Hunt]] = defaultdict(list)
    warning_count = 0
    for hunt in hunts:
        warnings = _audit_warnings(hunt)
        dup_index[_duplicate_key(hunt)].append(hunt)
        _print_hunt(hunt, warnings)
        warning_count += len(warnings)

    for key, group in dup_index.items():
        if len(group) > 1:
            print("-" * 72)
            print(f"[WARN] duplicate active hunts for key {key!r}: {', '.join(h.name for h in group)}")
            warning_count += 1

    if args.fix:
        from engine.hunt_repository import update_hunt
        print("\n=== Proposed fixes ===")
        fix_count = 0
        for hunt in hunts:
            for desc, patched in _proposed_fixes(hunt):
                print(f"[FIX] {hunt.name}: {desc}")
                if update_hunt(patched):
                    fix_count += 1
                    print("      applied.")
        print(f"Applied {fix_count} fix(es).")
    elif warning_count:
        print(f"\n{warning_count} warning(s) found. Re-run with --fix to apply conservative fixes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
