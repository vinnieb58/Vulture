"""Pure parsing helpers for host command output (unit-testable)."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DiskEntry:
    mount: str
    filesystem: str
    size: str
    used: str
    available: str
    percent_used: float | None


@dataclass(frozen=True)
class ContainerRow:
    name: str
    status: str
    ports: str


@dataclass(frozen=True)
class MemoryInfo:
    total: str
    used: str
    available: str
    percent_used: float | None


def parse_df_human(text: str) -> list[DiskEntry]:
    """Parse `df -h` output into disk entries."""
    entries: list[DiskEntry] = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        filesystem = parts[0]
        size, used, avail = parts[1], parts[2], parts[3]
        pct_str = parts[4].rstrip("%")
        mount = parts[5]
        try:
            pct = float(pct_str)
        except ValueError:
            pct = None
        entries.append(
            DiskEntry(
                mount=mount,
                filesystem=filesystem,
                size=size,
                used=used,
                available=avail,
                percent_used=pct,
            )
        )
    return entries


def parse_docker_ps_format(text: str) -> list[ContainerRow]:
    """Parse docker ps --format '{{.Names}}\\t{{.Status}}\\t{{.Ports}}' lines."""
    rows: list[ContainerRow] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 2)
        if len(parts) == 3:
            rows.append(ContainerRow(name=parts[0], status=parts[1], ports=parts[2]))
        elif len(parts) == 1:
            rows.append(ContainerRow(name=parts[0], status="unknown", ports=""))
    return rows


def parse_container_names(text: str) -> list[str]:
    names = [line.strip() for line in text.splitlines() if line.strip()]
    return sorted(names)


def parse_systemctl_failed(text: str) -> list[str]:
    """Parse `systemctl --failed --no-pager` unit names."""
    units: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("UNIT ") or stripped.startswith("0 loaded"):
            continue
        if stripped.startswith("●"):
            stripped = stripped[1:].strip()
        parts = stripped.split()
        if parts and parts[0].endswith(".service"):
            units.append(parts[0])
    return units


def parse_free_human(text: str) -> MemoryInfo | None:
    """Parse `free -h` Mem row."""
    for line in text.splitlines():
        if not line.startswith("Mem:"):
            continue
        parts = line.split()
        if len(parts) < 4:
            return None
        total, used, avail = parts[1], parts[2], parts[3]
        pct = _percent_from_human_pair(used, total)
        return MemoryInfo(total=total, used=used, available=avail, percent_used=pct)
    return None


def parse_loadavg(text: str) -> str | None:
    parts = text.split()
    if len(parts) >= 3:
        return f"{parts[0]} / {parts[1]} / {parts[2]} (1/5/15 min)"
    return None


def parse_meminfo(text: str) -> MemoryInfo | None:
    data: dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)", line)
        if m:
            data[m.group(1)] = int(m.group(2))
    total_kb = data.get("MemTotal")
    avail_kb = data.get("MemAvailable") or data.get("MemFree")
    if total_kb is None:
        return None
    used_kb = total_kb - avail_kb if avail_kb is not None else None
    pct = 100.0 * used_kb / total_kb if used_kb is not None and total_kb > 0 else None
    return MemoryInfo(
        total=_format_kib(total_kb),
        used=_format_kib(used_kb) if used_kb is not None else "n/a",
        available=_format_kib(avail_kb) if avail_kb is not None else "n/a",
        percent_used=pct,
    )


def parse_ip_br_addr(text: str) -> list[tuple[str, str]]:
    """Return (interface, ipv4) pairs from `ip -br addr` output."""
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        iface = parts[0]
        for token in parts[2:]:
            if "/" in token and "." in token.split("/")[0]:
                addr = token.split("/")[0]
                if not addr.startswith("127."):
                    pairs.append((iface, addr))
                    break
    return pairs


def pick_lan_ipv4(text: str) -> str | None:
    for _iface, addr in parse_ip_br_addr(text):
        if not addr.startswith("100."):
            return addr
    for _iface, addr in parse_ip_br_addr(text):
        return addr
    return None


def _format_kib(kib: int) -> str:
    gib = kib / (1024 * 1024)
    if gib >= 1:
        return f"{gib:.1f}Gi"
    mib = kib / 1024
    if mib >= 1:
        return f"{mib:.0f}Mi"
    return f"{kib}Ki"


def _percent_from_human_pair(used: str, total: str) -> float | None:
    used_b = _human_to_bytes(used)
    total_b = _human_to_bytes(total)
    if used_b is None or total_b is None or total_b == 0:
        return None
    return 100.0 * used_b / total_b


def _human_to_bytes(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    multipliers = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
        "T": 1000**4,
    }
    for suffix, mult in multipliers.items():
        if value.endswith(suffix):
            try:
                return float(value[: -len(suffix)]) * mult
            except ValueError:
                return None
    try:
        return float(value)
    except ValueError:
        return None
