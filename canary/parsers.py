"""
Pure parsing helpers for Canary checks (unit-test friendly).
"""

from __future__ import annotations

import re
from typing import Any

from canary.config import DISK_CRITICAL_PERCENT, DISK_WARN_PERCENT

OverallStatus = str  # "ok" | "warning" | "critical"
CheckStatus = str  # "ok" | "warning" | "critical" | "not_configured" | "degraded"

StorageVolumeStatus = str
# OK | MISSING_DEVICE | NOT_MOUNTED | AUTOMOUNT_INACTIVE | STALE_MOUNT | DF_TIMEOUT | ERROR

STORAGE_VOLUME_STATUSES = frozenset(
    {
        "OK",
        "MISSING_DEVICE",
        "NOT_MOUNTED",
        "AUTOMOUNT_INACTIVE",
        "STALE_MOUNT",
        "DF_TIMEOUT",
        "ERROR",
    }
)


def combine_status(*levels: str) -> OverallStatus:
    normalized = {level.lower() for level in levels if level}
    if "critical" in normalized or "fail" in normalized or "error" in normalized:
        return "critical"
    if (
        "warning" in normalized
        or "warn" in normalized
        or "degraded" in normalized
        or "stale_mount" in normalized
        or "df_timeout" in normalized
    ):
        return "warning"
    return "ok"


def storage_volume_to_overall(status: StorageVolumeStatus, *, required: bool = True) -> OverallStatus:
    if status == "OK":
        return "ok"
    if status in ("STALE_MOUNT", "DF_TIMEOUT", "ERROR"):
        return "critical"
    if not required and status in ("MISSING_DEVICE", "NOT_MOUNTED", "AUTOMOUNT_INACTIVE"):
        return "warning"
    return "warning"


def storage_use_level(percent: float | None) -> str | None:
    if percent is None:
        return None
    if percent >= DISK_CRITICAL_PERCENT:
        return "critical"
    if percent >= DISK_WARN_PERCENT:
        return "warning"
    return None


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


def parse_lsblk_uuid_map(text: str) -> dict[str, dict[str, str | None]]:
    """
    Parse `lsblk -o UUID,FSTYPE,LABEL,SIZE -n -P` lines keyed by UUID.
    Device NAME fields are intentionally ignored.
    """
    by_uuid: dict[str, dict[str, str | None]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        fields: dict[str, str] = {}
        for match in re.finditer(r'(\w+)="([^"]*)"', line):
            fields[match.group(1)] = match.group(2)
        uuid = fields.get("UUID", "").strip()
        if not uuid:
            continue
        by_uuid[uuid.lower()] = {
            "uuid": uuid,
            "fstype": fields.get("FSTYPE") or None,
            "label": fields.get("LABEL") or None,
            "size": fields.get("SIZE") or None,
        }
    return by_uuid


def parse_findmnt_target(text: str) -> dict[str, str | None]:
    """Parse a single findmnt line: TARGET SOURCE FSTYPE UUID."""
    line = text.strip().splitlines()[0] if text.strip() else ""
    if not line:
        return {}
    parts = line.split()
    if len(parts) < 3:
        return {}
    source = parts[1]
    uuid = None
    if source.startswith("UUID="):
        uuid = source[5:]
    elif source.startswith("/dev/disk/by-uuid/"):
        uuid = source.split("/")[-1]
    return {
        "target": parts[0],
        "source": source,
        "fstype": parts[2],
        "uuid": uuid,
    }


def parse_fstab_entries(text: str) -> dict[str, dict[str, Any]]:
    """Parse /etc/fstab keyed by mount path. Uses UUID= specs only (never /dev/sdX)."""
    entries: dict[str, dict[str, Any]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        spec, mountpoint = parts[0], parts[1]
        if not spec.startswith("UUID="):
            continue
        uuid = spec[5:]
        fstype = parts[2] if len(parts) > 2 else None
        options = parts[3] if len(parts) > 3 else ""
        entries[mountpoint] = {
            "uuid": uuid,
            "fstype": fstype,
            "options": options,
            "automount_expected": "x-systemd.automount" in options,
        }
    return entries


def derive_automount_unit(mount_path: str) -> str:
    """Derive systemd automount unit name from mount path (matches systemd-escape rules)."""
    escaped = mount_path.strip("/").replace("-", r"\x2d").replace("/", "-")
    return f"{escaped}.automount"


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
    """Parse `systemctl --failed --no-pager` output."""
    if not text.strip():
        return 0, []

    count = 0
    names: list[str] = []

    if re.search(r"0\s+loaded units listed", text):
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
        if unit.endswith((".service", ".socket", ".mount", ".timer", ".automount")):
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
            containers.append({"name": parts[0], "status": parts[1], "ports": parts[2]})
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
