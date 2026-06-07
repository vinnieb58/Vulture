"""Read-only Raven host health, services, storage, and Docker status."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from parsers import (
    ContainerRow,
    DiskEntry,
    MemoryInfo,
    parse_container_names,
    parse_df_human,
    parse_docker_ps_format,
    parse_free_human,
    parse_loadavg,
    parse_meminfo,
    parse_systemctl_failed,
    pick_lan_ipv4,
)
from host_commands import (
    run_host_command,
    run_systemctl,
    systemctl_is_active,
    systemctl_is_enabled,
    systemctl_unit_exists,
)
from subprocess_util import run_command

HOST_ROOT = Path(os.environ.get("DASHBOARD_HOST_ROOT", "/host/root"))
HOST_PROC = Path(os.environ.get("DASHBOARD_HOST_PROC", "/host/proc"))
DEFAULT_STORAGE_MOUNTS = (
    ("Root filesystem", str(HOST_ROOT)),
    ("MicroSD", "/mnt/storage/microsd"),
    ("portable_beast", "/mnt/storage/portable_beast"),
    ("toshiba_ext", "/mnt/storage/toshiba_ext"),
)

SERVICE_UNITS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SSH", ("ssh.service", "ssh.socket", "sshd.service")),
    ("tailscaled", ("tailscaled.service", "tailscaled")),
    ("smbd", ("smbd.service", "smbd")),
    ("docker", ("docker.service", "docker")),
    ("vulture-bot", ("vulture-bot.service", "vulture-bot")),
    ("vulture-scheduler", ("vulture-scheduler.timer", "vulture-scheduler.timer")),
)


@dataclass
class ServiceStatus:
    label: str
    unit: str | None
    active: str
    enabled: str
    warning: str | None = None


@dataclass
class StorageStatus:
    label: str
    path: str
    mounted: bool
    filesystem: str | None = None
    size: str | None = None
    used: str | None = None
    available: str | None = None
    percent_used: float | None = None
    warning: str | None = None


@dataclass
class DockerSnapshot:
    daemon_active: bool
    daemon_state: str
    warning: str | None
    running_count: int
    stopped_count: int
    containers: list[ContainerRow] = field(default_factory=list)


def _read_hostname() -> str:
    for path in (Path("/etc/hostname"), HOST_ROOT / "etc/hostname"):
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
            except OSError:
                continue
    ok, out = run_command(["hostname"])
    return out.splitlines()[0].strip() if ok and out else "unknown"


def _read_uptime() -> str:
    ok, out = run_command(["uptime", "-p"], timeout=5.0)
    if ok and out:
        text = out.strip()
        return text[3:].strip() if text.startswith("up ") else text
    proc_uptime = HOST_PROC / "uptime"
    if proc_uptime.is_file():
        try:
            secs = float(proc_uptime.read_text().split()[0])
            mins, s = divmod(int(secs), 60)
            hours, mins = divmod(mins, 60)
            days, hours = divmod(hours, 24)
            parts: list[str] = []
            if days:
                parts.append(f"{days} day{'s' if days != 1 else ''}")
            if hours:
                parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
            if mins:
                parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
            return ", ".join(parts) if parts else "< 1 minute"
        except (OSError, ValueError, IndexError):
            pass
    return "unknown"


def _read_boot_time() -> str | None:
    ok, out = run_command(["who", "-b"], timeout=5.0)
    if ok and out.strip():
        m = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", out)
        if m:
            return m.group(1)
    ok, out = run_command(["uptime", "-s"], timeout=5.0)
    if ok and out.strip():
        return out.strip()
    stat = HOST_PROC / "stat"
    if stat.is_file():
        try:
            for line in stat.read_text(encoding="utf-8").splitlines():
                if line.startswith("btime "):
                    ts = int(line.split()[1])
                    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                        "%Y-%m-%d %H:%M UTC"
                    )
        except (OSError, ValueError, IndexError):
            pass
    return None


def _read_lan_ip() -> tuple[str | None, str | None]:
    ok, out = run_command(["ip", "-br", "addr"], timeout=5.0)
    if ok and out.strip():
        return pick_lan_ipv4(out), None
    ok, out = run_command(["hostname", "-I"], timeout=5.0)
    if ok and out.strip():
        for addr in out.split():
            if "." in addr and not addr.startswith("127."):
                return addr, None
    return None, "Could not determine LAN IP"


def _read_tailscale_ip() -> tuple[str | None, str | None]:
    ok, out = run_host_command(["tailscale", "ip", "-4"], timeout=8.0)
    if ok and out.strip():
        return out.splitlines()[0].strip(), None
    return None, "Tailscale IP unavailable"


def _check_internet() -> tuple[bool, str | None]:
    ok, err = run_command(["ping", "-c", "1", "-W", "2", "1.1.1.1"], timeout=6.0)
    if ok:
        return True, None
    return False, err or "Unreachable"


def _read_failed_units() -> tuple[list[str], str | None]:
    ok, out = run_systemctl(["--failed", "--no-pager"], timeout=10.0)
    if not ok:
        return [], out or "systemctl unavailable"
    units = parse_systemctl_failed(out)
    return units, None


def _read_memory() -> tuple[MemoryInfo | None, str | None]:
    ok, out = run_command(["free", "-h"], timeout=5.0)
    if ok:
        mem = parse_free_human(out)
        if mem:
            return mem, None
    meminfo = HOST_PROC / "meminfo"
    if meminfo.is_file():
        try:
            mem = parse_meminfo(meminfo.read_text(encoding="utf-8"))
            if mem:
                return mem, None
        except OSError as exc:
            return None, str(exc)
    return None, "Memory info unavailable"


def _read_load() -> tuple[str | None, str | None]:
    try:
        one, five, fifteen = os.getloadavg()
        return f"{one:.2f} / {five:.2f} / {fifteen:.2f} (1/5/15 min)", None
    except (AttributeError, OSError):
        pass
    loadavg = HOST_PROC / "loadavg"
    if loadavg.is_file():
        try:
            parsed = parse_loadavg(loadavg.read_text(encoding="utf-8"))
            if parsed:
                return parsed, None
        except OSError as exc:
            return None, str(exc)
    return None, "Load average unavailable"


def _resolve_unit(candidates: tuple[str, ...]) -> str | None:
    for unit in candidates:
        if systemctl_unit_exists(unit):
            return unit
    return None


def _normalize_unit_state(raw: str, *, missing_label: str) -> str:
    state = (raw or "").strip().lower()
    if not state or state in ("unknown", "systemctl unavailable"):
        return "unknown"
    if "command not found" in state or "unavailable" in state:
        return "unknown"
    if state in ("not-found",) or "could not be found" in state:
        return missing_label
    return state


def _check_service(label: str, candidates: tuple[str, ...]) -> ServiceStatus:
    unit = _resolve_unit(candidates)
    if unit is None:
        return ServiceStatus(
            label=label,
            unit=None,
            active="not found",
            enabled="not configured",
            warning=None,
        )

    ok_active, active_out = systemctl_is_active(unit)
    ok_enabled, enabled_out = systemctl_is_enabled(unit)
    active = _normalize_unit_state(active_out if ok_active else "unknown", missing_label="unknown")
    enabled = _normalize_unit_state(
        enabled_out if ok_enabled else "unknown",
        missing_label="not configured",
    )

    warning = None
    if not ok_active and active == "unknown":
        warning = f"{label}: systemctl unavailable"
    elif active in ("failed", "inactive", "dead"):
        warning = f"{label} is {active}"

    return ServiceStatus(
        label=label,
        unit=unit,
        active=active,
        enabled=enabled,
        warning=warning,
    )


def _storage_mounts() -> list[tuple[str, str]]:
    raw = os.environ.get("DASHBOARD_STORAGE_MOUNTS", "").strip()
    if not raw:
        return list(DEFAULT_STORAGE_MOUNTS)
    mounts: list[tuple[str, str]] = []
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        if ":" in piece:
            label, path = piece.split(":", 1)
            mounts.append((label.strip(), path.strip()))
        else:
            mounts.append((piece, piece))
    return mounts or list(DEFAULT_STORAGE_MOUNTS)


def _path_is_mounted(path: str) -> bool:
    mount_path = Path(path)
    if not mount_path.exists():
        return False
    mounts_file = HOST_PROC / "mounts"
    if mounts_file.is_file():
        try:
            normalized = path.rstrip("/") or "/"
            for line in mounts_file.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1].rstrip("/") == normalized:
                    return True
        except OSError:
            pass
    return mount_path.is_dir()


def get_storage_status() -> list[StorageStatus]:
    mounts = _storage_mounts()
    paths = [path for _, path in mounts]
    ok, out = run_command(["df", "-h", *paths], timeout=10.0)
    entries_by_mount = {e.mount: e for e in parse_df_human(out)} if ok else {}

    result: list[StorageStatus] = []
    for label, path in mounts:
        entry: DiskEntry | None = entries_by_mount.get(path)
        if entry is None:
            for candidate in entries_by_mount.values():
                if candidate.mount == path:
                    entry = candidate
                    break
        mounted = _path_is_mounted(path)
        warning = None
        if label != "Root filesystem" and not mounted:
            warning = "Mount missing — USB drives may not be detected after reboot"
        elif entry and entry.percent_used is not None and entry.percent_used >= 90:
            warning = f"Disk usage high ({entry.percent_used:.0f}%)"

        result.append(
            StorageStatus(
                label=label,
                path=path,
                mounted=mounted,
                filesystem=entry.filesystem if entry else None,
                size=entry.size if entry else None,
                used=entry.used if entry else None,
                available=entry.available if entry else None,
                percent_used=entry.percent_used if entry else None,
                warning=warning,
            )
        )
    return result


def get_docker_snapshot() -> DockerSnapshot:
    docker_svc = _check_service("docker", ("docker.service", "docker"))
    warning: str | None = None
    containers: list[ContainerRow] = []
    running_names: list[str] = []
    stopped_names: list[str] = []

    ok_ps, out_ps = run_host_command(
        [
            "docker",
            "ps",
            "--format",
            "{{.Names}}\t{{.Status}}\t{{.Ports}}",
        ],
        timeout=15.0,
    )
    if ok_ps:
        containers = parse_docker_ps_format(out_ps)
        running_names = [c.name for c in containers]
    else:
        warning = out_ps or "docker ps unavailable"

    ok_all, out_all = run_host_command(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        timeout=15.0,
    )
    if ok_all:
        all_names = set(parse_container_names(out_all))
        running_set = set(running_names)
        stopped_names = sorted(all_names - running_set)
    elif warning is None:
        warning = out_all or "docker ps -a unavailable"

    return DockerSnapshot(
        daemon_active=docker_svc.active == "active",
        daemon_state=docker_svc.active,
        warning=warning,
        running_count=len(running_names),
        stopped_count=len(stopped_names),
        containers=containers,
    )


def get_raven_health() -> dict[str, Any]:
    """Aggregate Raven host health for the dashboard template."""
    lan_ip, lan_warn = _read_lan_ip()
    ts_ip, ts_warn = _read_tailscale_ip()
    internet_ok, internet_warn = _check_internet()
    failed_units, failed_warn = _read_failed_units()
    memory, memory_warn = _read_memory()
    load, load_warn = _read_load()

    warnings: list[str] = []
    for item in (lan_warn, ts_warn, internet_warn, failed_warn, memory_warn, load_warn):
        if item:
            warnings.append(item)

    return {
        "hostname": _read_hostname(),
        "uptime": _read_uptime(),
        "boot_time": _read_boot_time(),
        "lan_ip": lan_ip,
        "tailscale_ip": ts_ip,
        "internet_ok": internet_ok,
        "failed_units": failed_units,
        "failed_count": len(failed_units),
        "memory": memory,
        "load_average": load,
        "warnings": warnings,
    }


def get_service_statuses() -> list[ServiceStatus]:
    return [_check_service(label, candidates) for label, candidates in SERVICE_UNITS]
