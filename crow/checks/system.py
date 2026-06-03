"""
Host system checks — Raven status, disk, memory (read-only).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from crow.checks._subprocess import run_command
from crow.config import (
    DISK_CRITICAL_PERCENT,
    DISK_WARN_PERCENT,
    EXTRA_DISK_PATHS,
    MOUNT_SCAN_ROOTS,
)
from crow.formatting import disk_level, format_bytes, format_percent


@dataclass
class DiskEntry:
    mount: str
    total_bytes: int | None
    used_bytes: int | None
    avail_bytes: int | None
    percent_used: float | None
    level: str


@dataclass
class MemoryInfo:
    total_bytes: int | None
    used_bytes: int | None
    available_bytes: int | None
    percent_used: float | None


def get_hostname() -> str:
    ok, out = run_command(["hostname"])
    if ok and out:
        return out.splitlines()[0].strip()
    return Path("/etc/hostname").read_text(encoding="utf-8").strip() if Path(
        "/etc/hostname"
    ).is_file() else "unknown"


def get_uptime() -> str:
    ok, out = run_command(["uptime", "-p"])
    if ok and out:
        return out
    # Fallback: /proc/uptime seconds
    try:
        secs = float(Path("/proc/uptime").read_text().split()[0])
        mins, s = divmod(int(secs), 60)
        hours, mins = divmod(mins, 60)
        days, hours = divmod(hours, 24)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if mins:
            parts.append(f"{mins}m")
        return "up " + " ".join(parts) if parts else "up <1m"
    except (OSError, ValueError, IndexError):
        return "unknown"


def get_load_average() -> str | None:
    try:
        one, five, fifteen = os.getloadavg()
        return f"{one:.2f} / {five:.2f} / {fifteen:.2f} (1/5/15 min)"
    except (AttributeError, OSError):
        pass
    try:
        parts = Path("/proc/loadavg").read_text().split()[:3]
        if len(parts) == 3:
            return f"{parts[0]} / {parts[1]} / {parts[2]} (1/5/15 min)"
    except OSError:
        pass
    return None


def get_current_timestamp() -> str:
    from crow.formatting import format_timestamp

    return format_timestamp()


def parse_df_output(text: str) -> list[DiskEntry]:
    """Parse `df -P -B1` output into DiskEntry list."""
    entries: list[DiskEntry] = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return entries

    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        filesystem = parts[0]
        try:
            total_b = int(parts[1])
            used_b = int(parts[2])
            avail_b = int(parts[3])
        except ValueError:
            continue
        mount = parts[5]
        pct_str = parts[4].rstrip("%")
        try:
            pct = float(pct_str)
        except ValueError:
            pct = None
            if total_b > 0:
                pct = 100.0 * used_b / total_b

        entries.append(
            DiskEntry(
                mount=mount,
                total_bytes=total_b,
                used_bytes=used_b,
                avail_bytes=avail_b,
                percent_used=pct,
                level=disk_level(pct),
            )
        )
        # avoid duplicate pseudo filesystem noise in summaries
        _ = filesystem

    return entries


def get_disk_entries_for_paths(paths: list[str]) -> list[DiskEntry]:
    """Run df for specific mount paths."""
    if not paths:
        return []
    args = ["df", "-P", "-B1", *paths]
    ok, out = run_command(args)
    if not ok:
        return []
    return parse_df_output(out)


def discover_extra_mount_paths() -> list[str]:
    """Discover mounted paths under /mnt, /media, and configured extras."""
    found: list[str] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path)
        if key in seen:
            return
        if path.is_dir():
            seen.add(key)
            found.append(key)

    add(Path("/"))
    for root in MOUNT_SCAN_ROOTS:
        if not root.is_dir():
            continue
        try:
            for child in sorted(root.iterdir()):
                if child.is_dir():
                    add(child)
        except OSError:
            continue

    for p in EXTRA_DISK_PATHS:
        add(p)

    return found


def get_disk_summary(paths: list[str] | None = None) -> list[DiskEntry]:
    if paths is None:
        paths = discover_extra_mount_paths()
    return get_disk_entries_for_paths(paths)


def parse_free_output(text: str) -> MemoryInfo:
    """Parse `free -b` output (Mem row)."""
    for line in text.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 7:
                try:
                    total = int(parts[1])
                    used = int(parts[2])
                    avail = int(parts[6])
                    pct = 100.0 * used / total if total > 0 else None
                    return MemoryInfo(total, used, avail, pct)
                except ValueError:
                    break
            if len(parts) >= 4:
                try:
                    total = int(parts[1])
                    used = int(parts[2])
                    avail = int(parts[3])
                    pct = 100.0 * used / total if total > 0 else None
                    return MemoryInfo(total, used, avail, pct)
                except ValueError:
                    break
    return MemoryInfo(None, None, None, None)


def parse_meminfo(text: str) -> MemoryInfo:
    """Parse /proc/meminfo."""
    data: dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)", line)
        if m:
            data[m.group(1)] = int(m.group(2)) * 1024  # kB -> bytes

    total = data.get("MemTotal")
    avail = data.get("MemAvailable") or data.get("MemFree")
    if total is None:
        return MemoryInfo(None, None, None, None)
    used = total - avail if avail is not None else None
    pct = 100.0 * used / total if used is not None and total > 0 else None
    return MemoryInfo(total, used, avail, pct)


def get_memory_info() -> MemoryInfo:
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        try:
            return parse_meminfo(meminfo.read_text(encoding="utf-8"))
        except OSError:
            pass

    ok, out = run_command(["free", "-b"])
    if ok:
        return parse_free_output(out)
    return MemoryInfo(None, None, None, None)


def get_raven_status_summary() -> dict[str, str]:
    """Concise Raven host status fields."""
    mem = get_memory_info()
    root_entries = get_disk_entries_for_paths(["/"])
    root = root_entries[0] if root_entries else None

    disk_line = "n/a"
    if root and root.percent_used is not None:
        disk_line = (
            f"{root.percent_used:.0f}% used "
            f"({format_bytes(root.used_bytes)} / {format_bytes(root.total_bytes)})"
        )

    return {
        "hostname": get_hostname(),
        "uptime": get_uptime(),
        "memory": (
            f"{format_percent(mem.used_bytes, mem.total_bytes)} "
            f"({format_bytes(mem.used_bytes)} / {format_bytes(mem.total_bytes)}, "
            f"avail {format_bytes(mem.available_bytes)})"
            if mem.total_bytes
            else "n/a"
        ),
        "disk_root": disk_line,
        "load_average": get_load_average() or "n/a",
        "timestamp": get_current_timestamp(),
    }


def format_disk_check_message(entries: list[DiskEntry]) -> str:
    from crow.formatting import disk_level_icon, join_lines

    if not entries:
        return "**Disk check**\nNo filesystem data available."

    lines = ["**Disk check** (read-only)"]
    for e in entries:
        icon = disk_level_icon(e.level)
        pct = f"{e.percent_used:.0f}%" if e.percent_used is not None else "n/a"
        lines.append(
            f"{icon} `{e.mount}` — {pct} used "
            f"({format_bytes(e.used_bytes)} / {format_bytes(e.total_bytes)}, "
            f"avail {format_bytes(e.avail_bytes)})"
        )
    lines.append("")
    lines.append(f"Warn ≥{DISK_WARN_PERCENT}% · Critical ≥{DISK_CRITICAL_PERCENT}%")
    return join_lines(lines)


def format_memory_check_message(mem: MemoryInfo) -> str:
    from crow.formatting import join_lines

    lines = [
        "**Memory check** (read-only)",
        f"Total: {format_bytes(mem.total_bytes)}",
        f"Used: {format_bytes(mem.used_bytes)} ({format_percent(mem.used_bytes, mem.total_bytes)})",
        f"Available: {format_bytes(mem.available_bytes)}",
    ]
    return join_lines(lines)


def format_raven_status_message(summary: dict[str, str]) -> str:
    from crow.formatting import join_lines

    return join_lines(
        [
            "**Raven status** (read-only)",
            f"Host: `{summary['hostname']}`",
            f"Uptime: {summary['uptime']}",
            f"Memory: {summary['memory']}",
            f"Disk `/`: {summary['disk_root']}",
            f"Load: {summary['load_average']}",
            f"Time: {summary['timestamp']}",
        ]
    )
