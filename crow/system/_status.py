"""
Shared status types and evaluation helpers for Raven health checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

StatusLevel = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class StatusItem:
    label: str
    level: StatusLevel
    detail: str | None = None


def status_icon(level: StatusLevel) -> str:
    return {"ok": "✅", "warn": "⚠", "fail": "❌"}.get(level, "?")


def combine_levels(*levels: StatusLevel) -> StatusLevel:
    if "fail" in levels:
        return "fail"
    if "warn" in levels:
        return "warn"
    return "ok"


def overall_from_items(items: list[StatusItem]) -> StatusLevel:
    return combine_levels(*(item.level for item in items))


def format_item_line(item: StatusItem) -> str:
    icon = status_icon(item.level)
    if item.detail:
        return f"{icon} {item.label} — {item.detail}"
    return f"{icon} {item.label}"
