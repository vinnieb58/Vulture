"""
Listening port summary for Raven (read-only, summarized — no full socket dump).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from crow.checks._subprocess import run_command
from crow.config import KNOWN_SERVICE_PORTS


@dataclass(frozen=True)
class OpenService:
    port: int
    label: str
    listening: bool


def _parse_listening_ports(text: str) -> set[int]:
    ports: set[int] = set()
    for line in text.splitlines():
        # ss format: Local Address:Port in column 4 typically
        for match in re.finditer(r":(\d+)\s", line):
            try:
                ports.add(int(match.group(1)))
            except ValueError:
                continue
        # Also match *:PORT patterns
        for match in re.finditer(r"\*:(\d+)", line):
            try:
                ports.add(int(match.group(1)))
            except ValueError:
                continue
    return ports


def _scan_listening_ports() -> set[int]:
    for args in (
        ["ss", "-tulpn"],
        ["ss", "-tuln"],
    ):
        ok, out = run_command(args, timeout=12.0)
        if ok and out.strip():
            return _parse_listening_ports(out)
    return set()


def get_open_services_summary(
    known_ports: list[tuple[int, str]] | None = None,
) -> list[OpenService]:
    catalog = known_ports or KNOWN_SERVICE_PORTS
    listening = _scan_listening_ports()
    return [
        OpenService(port=port, label=label, listening=port in listening)
        for port, label in catalog
    ]


def format_open_service_line(service: OpenService) -> str:
    if service.listening:
        return f"{service.port:<4} {service.label}"
    return f"{service.port:<4} {service.label} (not listening)"
