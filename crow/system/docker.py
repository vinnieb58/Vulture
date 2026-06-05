"""
Docker health visibility for Raven (read-only).
"""

from __future__ import annotations

from dataclasses import dataclass

from crow.checks._subprocess import run_command
from crow.system._status import StatusItem, StatusLevel
from crow.system.services import check_service


@dataclass(frozen=True)
class DockerStatus:
    active: bool
    state: str
    running: list[str]
    stopped: list[str]


def _parse_container_names(text: str) -> list[str]:
    names = []
    for line in text.splitlines():
        name = line.strip()
        if name:
            names.append(name)
    return sorted(names)


def get_docker_status() -> DockerStatus:
    docker_svc = check_service("Docker", "docker")
    running: list[str] = []
    stopped: list[str] = []

    ok_running, out_running = run_command(
        ["docker", "ps", "--format", "{{.Names}}"],
        timeout=15.0,
    )
    if ok_running:
        running = _parse_container_names(out_running)

    ok_stopped, out_stopped = run_command(
        ["docker", "ps", "-a", "--filter", "status=exited", "--format", "{{.Names}}"],
        timeout=15.0,
    )
    if ok_stopped:
        stopped = _parse_container_names(out_stopped)

    return DockerStatus(
        active=docker_svc.active,
        state=docker_svc.state,
        running=running,
        stopped=stopped,
    )


def docker_level(status: DockerStatus) -> StatusLevel:
    if not status.active:
        return "fail"
    return "ok"


def docker_to_status_item(status: DockerStatus) -> StatusItem:
    level = docker_level(status)
    count = len(status.running)
    detail = f"{count} container{'s' if count != 1 else ''} running"
    if not status.active:
        detail = status.state.upper()
    return StatusItem(label="Docker", level=level, detail=detail)
