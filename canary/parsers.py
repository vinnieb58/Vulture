"""
Pure parsing helpers for Canary checks (unit-test friendly).
"""

from __future__ import annotations

import re
from typing import Any

from canary.config import DISK_CRITICAL_PERCENT, DISK_WARN_PERCENT

OverallStatus = str  # "ok" | "warning" | "critical"
CheckStatus = str  # "ok" | "warning" | "critical" | "not_configured" | "degraded"


def combine_status(*levels: str) -> OverallStatus:
    normalized = {level.lower() for level in levels if level}
    if "critical" in normalized or "fail" in normalized:
        return "critical"
    if "warning" in normalized or "warn" in normalized or "degraded" in normalized:
        return "warning"
    return "ok"


def storage_use_status(percent: float | None, *, mounted: bool, is_root: bool) -> CheckStatus:
    if not mounted:
        return "critical" if is_root else "warning"
    if percent is None:
        return "ok"
    if percent >= DISK_CRITICAL_PERCENT:
        return "critical"
    if percent >= DISK_WARN_PERCENT:
        return "warning"
    return "ok"


def parse_df_output(text: str) -> dict[str, dict[str, Any]]:
    """Parse `df -P -B1` output keyed by mount point."""
    entries: dict[str, dict[str, Any]] = {}
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return entries

    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        mount = parts[5]
        try:
            total_b = int(parts[1])
            used_b = int(parts[2])
            avail_b = int(parts[3])
        except ValueError:
            continue
        pct_str = parts[4].rstrip("%")
        try:
            pct = float(pct_str)
        except ValueError:
            pct = 100.0 * used_b / total_b if total_b > 0 else None

        entries[mount] = {
            "size": total_b,
            "used": used_b,
            "available": avail_b,
            "use_percent": pct,
        }
    return entries


def parse_lan_ipv4_from_ip_br(text: str) -> str | None:
    """Extract first non-loopback IPv4 from `ip -br addr` output."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        for token in parts[2:]:
            if "/" not in token or ":" in token:
                continue
            addr = token.split("/", 1)[0]
            if addr.startswith("127.") or addr.startswith("169.254."):
                continue
            return addr
    return None


def parse_systemctl_failed(text: str) -> tuple[int, list[str]]:
    """
    Parse `systemctl --failed --no-pager` output.
    Returns (count, unit_names).
    """
    if not text.strip():
        return 0, []

    count = 0
    names: list[str] = []

    zero_match = re.search(r"0\s+loaded units listed", text)
    if zero_match:
        return 0, []

    loaded_match = re.search(r"(\d+)\s+loaded units listed", text)
    if loaded_match:
        count = int(loaded_match.group(1))

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("UNIT") or stripped.startswith("●"):
            continue
        if "loaded units listed" in stripped:
            continue
        parts = stripped.split()
        if not parts:
            continue
        unit = parts[0]
        if unit.endswith(".service") or unit.endswith(".socket") or unit.endswith(".mount"):
            names.append(unit)

    if count == 0 and names:
        count = len(names)
    return count, names


def parse_docker_ps_lines(text: str) -> list[dict[str, str]]:
    """Parse docker ps tab-separated name/status/ports lines."""
    containers: list[dict[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split("\t")
        if len(parts) >= 3:
            containers.append(
                {"name": parts[0], "status": parts[1], "ports": parts[2]}
            )
        elif len(parts) == 2:
            containers.append({"name": parts[0], "status": parts[1], "ports": ""})
        else:
            containers.append({"name": stripped, "status": "unknown", "ports": ""})
    return containers


def parse_tmux_sessions(text: str) -> list[str]:
    sessions: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("no server running"):
            continue
        name = stripped.split(":", 1)[0].strip()
        if name:
            sessions.append(name)
    return sessions
