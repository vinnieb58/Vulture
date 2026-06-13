"""Dry-run preview — resolve grocery intents against aliases without cart mutation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from finch.aliases import ensure_seeded, find_alias_matches
from finch.models import GroceryIntent, MatchStatus, PreviewLine
from finch.parser import parse_grocery_text


def resolve_intent(
    intent: GroceryIntent,
    *,
    db_path: Path | None = None,
) -> PreviewLine:
    matches = find_alias_matches(intent.normalized_name, db_path)

    if not matches:
        return PreviewLine(
            requested_item=intent.raw_text,
            normalized_name=intent.normalized_name,
            matched_alias=None,
            kroger_product_id=None,
            upc=None,
            quantity=intent.quantity,
            status=MatchStatus.MISSING,
            search_term=intent.normalized_name,
            notes="No alias configured; would search Kroger when live.",
        )

    if len(matches) > 1:
        names = ", ".join(m.display_name for m in matches)
        return PreviewLine(
            requested_item=intent.raw_text,
            normalized_name=intent.normalized_name,
            matched_alias=None,
            kroger_product_id=None,
            upc=None,
            quantity=intent.quantity,
            status=MatchStatus.AMBIGUOUS,
            search_term=intent.normalized_name,
            notes=f"Multiple aliases match: {names}",
        )

    alias = matches[0]
    has_product_pin = bool(alias.upc or alias.kroger_product_id)
    status = MatchStatus.EXACT_DEFAULT if has_product_pin else MatchStatus.NEEDS_SEARCH

    return PreviewLine(
        requested_item=intent.raw_text,
        normalized_name=intent.normalized_name,
        matched_alias=alias.display_name,
        kroger_product_id=alias.kroger_product_id,
        upc=alias.upc,
        quantity=intent.quantity,
        status=status,
        search_term=alias.search_term or intent.normalized_name,
        notes=alias.notes,
    )


def build_preview(
    text: str,
    *,
    db_path: Path | None = None,
) -> list[PreviewLine]:
    ensure_seeded(db_path)
    intents = parse_grocery_text(text)
    return [resolve_intent(intent, db_path=db_path) for intent in intents]


def format_preview_line(line: PreviewLine) -> str:
    parts = [
        f"requested: {line.requested_item!r}",
        f"status: {line.status.value}",
        f"qty: {line.quantity:g}",
    ]
    if line.matched_alias:
        parts.append(f"alias: {line.matched_alias!r}")
    if line.upc:
        parts.append(f"upc: {line.upc}")
    if line.kroger_product_id:
        parts.append(f"product_id: {line.kroger_product_id}")
    if line.search_term and line.status != MatchStatus.EXACT_DEFAULT:
        parts.append(f"search: {line.search_term!r}")
    if line.notes:
        parts.append(f"notes: {line.notes}")
    return " | ".join(parts)


def print_preview(lines: list[PreviewLine], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps([line.to_dict() for line in lines], indent=2))
        return
    if not lines:
        print("(no items parsed)")
        return
    for line in lines:
        print(format_preview_line(line))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Finch dry-run preview — map grocery text to preferred Kroger products.",
    )
    parser.add_argument(
        "grocery_text",
        nargs="?",
        help='Grocery list text, e.g. "eggs, milk, coffee pods"',
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable lines",
    )
    parser.add_argument(
        "--file",
        "-f",
        type=Path,
        help="Read grocery list from a file instead of argv",
    )
    args = parser.parse_args(argv)

    if args.file:
        text = args.file.read_text(encoding="utf-8")
    elif args.grocery_text:
        text = args.grocery_text
    else:
        text = sys.stdin.read()

    if not text.strip():
        parser.error("provide grocery text as an argument, --file, or stdin")

    lines = build_preview(text)
    print_preview(lines, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
