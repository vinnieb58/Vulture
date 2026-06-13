"""Find nearby Kroger stores and save a preferred location."""

from __future__ import annotations

import argparse
import json
import re
import sys

from finch.env_check import search_ready
from finch.env_util import load_env
from finch.kroger_client import KrogerError, KrogerLocation, load_kroger_client_from_env
from finch.local_config import save_location_config


_ZIP_RE = re.compile(r"^\d{5}$")


def normalize_zip(raw: str) -> str:
    digits = raw.strip()
    if not _ZIP_RE.match(digits):
        raise ValueError(f"Invalid ZIP code: {raw!r} (expected 5 digits)")
    return digits


def format_location_line(index: int, location: KrogerLocation) -> str:
    pickup = "yes" if location.has_pickup else "no"
    lines = [
        f"  [{index}] {location.name}",
        f"      address: {location.address_line1}",
        f"      city/state/zip: {location.city_state_zip}",
        f"      locationId: {location.location_id}",
        f"      pickup (dept 94): {pickup}",
    ]
    if location.phone:
        lines.append(f"      phone: {location.phone}")
    return "\n".join(lines)


def format_locations_header(zip_code: str, count: int, radius_miles: int) -> str:
    return (
        f"Stores near ZIP {zip_code} (radius {radius_miles} mi)\n"
        f"Found {count} result(s):\n"
    )


def confirm_save_location(
    location: KrogerLocation,
    *,
    confirm: bool = False,
    input_fn=input,
) -> bool:
    if confirm:
        return True
    answer = input_fn(
        f"Save locationId {location.location_id} ({location.name}, {location.city_state_zip})? [y/N] "
    ).strip().lower()
    return answer in ("y", "yes")


def save_selected_location(
    location: KrogerLocation,
    zip_code: str,
    *,
    confirm: bool = False,
    input_fn=input,
    config_path=None,
) -> dict | None:
    if not confirm_save_location(location, confirm=confirm, input_fn=input_fn):
        print("Location not saved.")
        return None
    address = f"{location.address_line1}, {location.city_state_zip}"
    payload = save_location_config(
        location.location_id,
        store_name=location.name,
        store_address=address,
        saved_from_zip=zip_code,
        config_path=config_path,
    )
    print(f"Saved locationId {location.location_id} to data/finch_config.json")
    print(f"  store: {location.name} — {address}")
    return payload


def run_location_search(
    zip_code: str,
    *,
    radius_miles: int = 20,
    limit: int = 10,
    client=None,
) -> list[KrogerLocation]:
    if client is None:
        if not search_ready():
            raise RuntimeError(
                "Kroger location search not configured. Run: python -m finch.setup\n"
                "Set FINCH_KROGER_CLIENT_ID and FINCH_KROGER_CLIENT_SECRET in .env"
            )
        client = load_kroger_client_from_env()
    return client.search_locations(zip_code, radius_miles=radius_miles, limit=limit)


def print_location_results(
    zip_code: str,
    locations: list[KrogerLocation],
    *,
    radius_miles: int = 20,
    as_json: bool = False,
) -> None:
    if as_json:
        payload = [
            {
                "name": loc.name,
                "address": loc.address_line1,
                "city": loc.city,
                "state": loc.state,
                "zip": loc.zip_code,
                "location_id": loc.location_id,
                "has_pickup": loc.has_pickup,
                "phone": loc.phone,
            }
            for loc in locations
        ]
        print(json.dumps({"zip": zip_code, "results": payload}, indent=2))
        return

    if not locations:
        print(f"No stores found near ZIP {zip_code}.")
        return

    print(format_locations_header(zip_code, len(locations), radius_miles))
    for i, location in enumerate(locations, start=1):
        print(format_location_line(i, location))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Find nearby Kroger stores by ZIP code (client credentials — no cart access).",
    )
    parser.add_argument("zip", nargs="?", help="5-digit ZIP code, e.g. 77406")
    parser.add_argument("--zip", dest="zip_flag", metavar="ZIP", help="ZIP code (alternate flag)")
    parser.add_argument("--radius", type=int, default=20, help="Search radius in miles (default 20)")
    parser.add_argument("--limit", type=int, default=10, help="Max results (default 10)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--save", action="store_true", help="Save a selected store as preferred location")
    parser.add_argument("--pick", type=int, metavar="N", help="Result number to save (1-based)")
    parser.add_argument("--confirm", action="store_true", help="Skip confirmation prompt when saving")
    args = parser.parse_args(argv)

    load_env()

    raw_zip = args.zip_flag or args.zip
    if not raw_zip:
        parser.error("provide a ZIP code as an argument or --zip")
    try:
        zip_code = normalize_zip(raw_zip)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        locations = run_location_search(
            zip_code,
            radius_miles=args.radius,
            limit=args.limit,
        )
    except (RuntimeError, KrogerError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print_location_results(
        zip_code,
        locations,
        radius_miles=args.radius,
        as_json=args.json,
    )

    if not args.save:
        if locations and not args.json:
            print()
            print(f"Save a store: python -m finch.locations {zip_code} --save --pick 1 --confirm")
        return 0

    if not locations:
        print("No results to save.", file=sys.stderr)
        return 1

    pick = args.pick
    if pick is None:
        if args.confirm or not sys.stdin.isatty():
            print("Error: --pick N is required when stdin is not interactive.", file=sys.stderr)
            return 1
        raw = input(f"Pick store to save [1-{len(locations)}]: ").strip()
        try:
            pick = int(raw)
        except ValueError:
            print("Invalid pick.", file=sys.stderr)
            return 1

    if pick < 1 or pick > len(locations):
        print(f"Pick must be between 1 and {len(locations)}.", file=sys.stderr)
        return 1

    saved = save_selected_location(
        locations[pick - 1],
        zip_code,
        confirm=args.confirm,
    )
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
