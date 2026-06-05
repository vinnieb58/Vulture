"""Defensive Vulture log readers with error/warning split."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

LOG_PATH = Path(os.environ.get("VULTURE_LOG_PATH", "/app/logs/vulture.log"))
LOG_TAIL_LINES = int(os.environ.get("VULTURE_LOG_TAIL_LINES", "100"))

ERROR_PATTERNS = re.compile(
    r"(ERROR|WARNING|WARN|Traceback|Exception|failed|blocked|timeout)",
    re.IGNORECASE,
)


def _is_error_line(line: str) -> bool:
    return bool(ERROR_PATTERNS.search(line))


def read_log_snapshot() -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "warning": None,
        "lines": [],
        "error_lines": [],
        "general_lines": [],
    }

    if not LOG_PATH.exists():
        result["warning"] = f"Log file not found at {LOG_PATH}"
        return result

    try:
        with LOG_PATH.open("r", encoding="utf-8", errors="replace") as handle:
            lines = [line.rstrip("\n") for line in handle.readlines()]
        tail = lines[-LOG_TAIL_LINES:]
        result["lines"] = tail
        result["error_lines"] = [ln for ln in tail if _is_error_line(ln)]
        result["general_lines"] = [ln for ln in tail if not _is_error_line(ln)]
        result["available"] = bool(tail)
    except OSError as exc:
        result["warning"] = f"Could not read log file: {exc}"

    return result


def recent_errors_for_source(lines: list[str], source: str) -> list[str]:
    """Return recent log lines that mention a source and look like errors."""
    source_lower = source.lower()
    matches: list[str] = []
    for line in reversed(lines):
        if source_lower in line.lower() and _is_error_line(line):
            matches.append(line)
            if len(matches) >= 3:
                break
    return list(reversed(matches))
