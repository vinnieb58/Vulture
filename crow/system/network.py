"""
Network and Tailscale visibility for Raven (read-only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from crow.checks._subprocess import run_command
from crow.system._status import StatusItem, StatusLevel


@dataclass(frozen=True)
class TailscaleStatus:
    connected: bool
    ipv4: str | None
    hostname: str | None


@dataclass(frozen=True)
class NetworkSummary:
    internet_reachable: bool
    lan_ipv4: str | None
    tailscale_ipv4: str | None


def check_internet_reachable() -> bool:
    ok, _ = run_command(["ping", "-c", "1", "-W", "3", "1.1.1.1"], timeout=8.0)
    return ok


def get_lan_ipv4() -> str | None:
    ok, out = run_command(["hostname", "-I"])
    if ok and out.strip():
        for addr in out.split():
            if "." in addr and not addr.startswith("127."):
                return addr
    ok, out = run_command(["ip", "-4", "addr", "show"])
    if not ok:
        return None
    for line in out.splitlines():
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", line)
        if m:
            addr = m.group(1)
            if not addr.startswith("127.") and not addr.startswith("100."):
                return addr
    return None


def get_tailscale_ipv4() -> str | None:
    ok, out = run_command(["tailscale", "ip", "-4"], timeout=8.0)
    if ok and out.strip():
        return out.splitlines()[0].strip()
    return None


def get_tailscale_hostname() -> str | None:
    ok, out = run_command(["tailscale", "status", "--json"], timeout=10.0)
    if ok:
        m = re.search(r'"HostName"\s*:\s*"([^"]+)"', out)
        if m:
            return m.group(1)
    ok, out = run_command(["hostname"])
    if ok and out.strip():
        return out.splitlines()[0].strip()
    return None


def get_tailscale_status() -> TailscaleStatus:
    ipv4 = get_tailscale_ipv4()
    hostname = get_tailscale_hostname()
    return TailscaleStatus(
        connected=ipv4 is not None,
        ipv4=ipv4,
        hostname=hostname,
    )


def get_network_summary() -> NetworkSummary:
    return NetworkSummary(
        internet_reachable=check_internet_reachable(),
        lan_ipv4=get_lan_ipv4(),
        tailscale_ipv4=get_tailscale_ipv4(),
    )


def internet_to_status_item(reachable: bool) -> StatusItem:
    return StatusItem(
        label="Internet",
        level="ok" if reachable else "warn",
        detail="Reachable" if reachable else "Unreachable",
    )


def tailscale_to_status_item(status: TailscaleStatus) -> StatusItem:
    return StatusItem(
        label="Tailscale",
        level="ok" if status.connected else "warn",
        detail=status.ipv4 or "not connected",
    )
