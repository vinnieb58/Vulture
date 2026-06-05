"""
Raven health summaries — mirrors scripts/raven_healthcheck.sh evaluation logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from crow.checks._subprocess import run_command
from crow.checks.system import get_hostname, get_uptime
from crow.formatting import format_timestamp
from crow.system._status import StatusItem, StatusLevel, overall_from_items
from crow.system.docker import DockerStatus, docker_level, docker_to_status_item, get_docker_status
from crow.system.network import (
    NetworkSummary,
    TailscaleStatus,
    check_internet_reachable,
    get_network_summary,
    get_tailscale_status,
    internet_to_status_item,
    tailscale_to_status_item,
)
from crow.system.services import (
    ServiceCheck,
    get_critical_service_statuses,
    service_level,
    service_to_status_item,
)
from crow.system.storage import StorageMount, get_storage_summary, storage_to_status_item


@dataclass(frozen=True)
class RavenHealthSummary:
    hostname: str
    uptime: str
    network: list[StatusItem]
    storage: list[StatusItem]
    services: list[StatusItem]
    vulture: list[StatusItem]
    docker: StatusItem
    docker_detail: DockerStatus
    overall: StatusLevel


@dataclass(frozen=True)
class PostRebootValidation:
    checks: list[StatusItem]
    overall: StatusLevel


@dataclass(frozen=True)
class UptimeInfo:
    host_uptime: str
    last_boot: str | None


def _format_uptime_human(uptime_text: str) -> str:
    text = uptime_text.strip()
    if text.startswith("up "):
        text = text[3:]
    return text or "unknown"


def get_uptime_info() -> UptimeInfo:
    uptime = _format_uptime_human(get_uptime())
    last_boot = _parse_last_boot()
    return UptimeInfo(host_uptime=uptime, last_boot=last_boot)


def _parse_last_boot() -> str | None:
    ok, out = run_command(["who", "-b"])
    if ok and out.strip():
        # who -b: "         system boot  2026-06-05 08:13"
        m = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", out)
        if m:
            try:
                dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
                return format_timestamp(dt.replace(tzinfo=timezone.utc))
            except ValueError:
                return m.group(1)
    ok, out = run_command(["uptime", "-s"])
    if ok and out.strip():
        try:
            dt = datetime.strptime(out.strip(), "%Y-%m-%d %H:%M:%S")
            return format_timestamp(dt.replace(tzinfo=timezone.utc))
        except ValueError:
            return out.strip()
    return None


def _vulture_status_items(services: list[ServiceCheck]) -> list[StatusItem]:
    by_label = {s.label: s for s in services}
    items: list[StatusItem] = []
    for key, display in (("Vulture Bot", "Bot running"), ("Vulture Scheduler", "Scheduler running")):
        svc = by_label.get(key)
        if svc is None:
            items.append(StatusItem(display, "warn", "unknown"))
            continue
        level = service_level(svc)
        detail = "running" if svc.active else svc.state
        items.append(StatusItem(display, level, detail))
    return items


def get_raven_health_summary(*, post_reboot: bool = False) -> RavenHealthSummary:
    """Aggregate Raven health. post_reboot aligns with --post-reboot script mode."""
    _ = post_reboot  # same evaluation today; flag reserved for future divergence

    hostname = get_hostname()
    uptime = _format_uptime_human(get_uptime())

    internet = check_internet_reachable()
    tailscale = get_tailscale_status()
    network_items = [
        internet_to_status_item(internet),
        tailscale_to_status_item(tailscale),
    ]

    storage_mounts = get_storage_summary()
    storage_items = [storage_to_status_item(m) for m in storage_mounts]

    service_checks = get_critical_service_statuses()
    service_items = [service_to_status_item(s) for s in service_checks]

    vulture_items = _vulture_status_items(service_checks)

    docker_detail = get_docker_status()
    docker_item = docker_to_status_item(docker_detail)

    all_items = network_items + storage_items + service_items + vulture_items + [docker_item]
    overall = overall_from_items(all_items)

    return RavenHealthSummary(
        hostname=hostname,
        uptime=uptime,
        network=network_items,
        storage=storage_items,
        services=service_items,
        vulture=vulture_items,
        docker=docker_item,
        docker_detail=docker_detail,
        overall=overall,
    )


def get_post_reboot_validation() -> PostRebootValidation:
    """Focused post-reboot checklist — equivalent to raven_post_reboot_check.sh."""
    checks: list[StatusItem] = []

    service_checks = get_critical_service_statuses()
    by_label = {s.label: s for s in service_checks}

    for label in ("SSH", "Tailscale", "Docker", "Samba"):
        svc = by_label.get(label)
        if svc is None:
            checks.append(StatusItem(label, "warn", "unknown"))
            continue
        level = service_level(svc)
        checks.append(StatusItem(label, level, "ok" if svc.active else svc.state))

    internet = check_internet_reachable()
    checks.append(internet_to_status_item(internet))

    for mount in get_storage_summary():
        if mount.path == "/":
            continue
        if not mount.mounted:
            checks.append(
                StatusItem(
                    mount.label,
                    "warn",
                    "missing",
                )
            )

    for key, display in (("Vulture Bot", "Vulture Bot"), ("Vulture Scheduler", "Scheduler")):
        svc = by_label.get(key)
        if svc is None:
            checks.append(StatusItem(display, "warn", "unknown"))
            continue
        level = service_level(svc)
        checks.append(StatusItem(display, level, "ok" if svc.active else svc.state))

    overall = overall_from_items(checks)
    return PostRebootValidation(checks=checks, overall=overall)


def parse_healthcheck_summary(text: str) -> StatusLevel:
    """Parse Overall: OK|WARN|FAIL from raven_healthcheck.sh output."""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("Overall:"):
            body = stripped.split(":", 1)[1].strip().upper()
            if body.startswith("FAIL"):
                return "fail"
            if body.startswith("WARN"):
                return "warn"
            if body.startswith("OK"):
                return "ok"
    return "warn"


def run_external_healthcheck(*, post_reboot: bool = False) -> tuple[bool, str]:
    """Optional: run installed ~/raven_healthcheck.sh when present."""
    from crow.config import RAVEN_HEALTHCHECK_SCRIPT

    script = Path(RAVEN_HEALTHCHECK_SCRIPT)
    if not script.is_file():
        return False, ""
    args = ["bash", str(script)]
    if post_reboot:
        args.append("--post-reboot")
    return run_command(args, timeout=120.0)
