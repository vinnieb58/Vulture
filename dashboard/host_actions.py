"""Allowlisted mutating host commands for the Action Center.

Separate from host_commands.py (read-only observability) so dashboard pages
that only need status never import write paths.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from host_commands import HOST_ROOT, SYSTEMD_BUS_SOCKET, _host_env, command_strategies
from subprocess_util import DEFAULT_TIMEOUT

# Units permitted for each systemctl subcommand (no arbitrary service management).
ALLOWED_SYSTEMCTL_RESTART = frozenset(
    {
        "vulture-bot.service",
        "vulture-scheduler.timer",
    }
)
ALLOWED_SYSTEMCTL_START = frozenset(
    {
        "vulture-scheduler.service",
    }
)

DEFAULT_APP_DIR = os.environ.get(
    "DASHBOARD_APP_DIR",
    "/host/root/home/vinnieb58/projects/vulture",
)
UPDATE_SCRIPT = os.environ.get(
    "DASHBOARD_UPDATE_SCRIPT",
    f"{DEFAULT_APP_DIR}/scripts/update_raven_quick.sh",
)
HEALTHCHECK_SCRIPT = os.environ.get(
    "DASHBOARD_HEALTHCHECK_SCRIPT",
    f"{DEFAULT_APP_DIR}/scripts/raven_healthcheck.sh",
)
CANARY_CONTAINER = os.environ.get("DASHBOARD_CANARY_CONTAINER", "canary")

LONG_TIMEOUT = float(os.environ.get("DASHBOARD_ACTION_LONG_TIMEOUT", "900"))


def _run_raw_capture(
    args: Sequence[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
    on_line: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """Run a command, optionally streaming each output line to on_line."""
    try:
        proc = subprocess.Popen(
            list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            bufsize=1,
        )
    except FileNotFoundError:
        return 127, f"command not found: {args[0]}"
    except OSError as exc:
        return 125, f"os error: {exc}"

    lines: list[str] = []
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait(timeout=5)
                return 124, "\n".join(lines) + ("\n" if lines else "") + "timed out"

            line = proc.stdout.readline()
            if line:
                stripped = line.rstrip("\n")
                lines.append(stripped)
                if on_line:
                    on_line(stripped)
                continue

            if proc.poll() is not None:
                rest = proc.stdout.read()
                if rest:
                    for extra in rest.splitlines():
                        lines.append(extra)
                        if on_line:
                            on_line(extra)
                break

            time.sleep(0.05)

        rc = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        return 124, "\n".join(lines) + ("\n" if lines else "") + "timed out"

    return rc, "\n".join(lines)


def run_allowlisted_systemctl(
    subcommand: str,
    unit: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    on_line: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """Run systemctl restart/start for an allowlisted unit only."""
    if subcommand == "restart":
        if unit not in ALLOWED_SYSTEMCTL_RESTART:
            return 126, f"unit not allowlisted for restart: {unit}"
    elif subcommand == "start":
        if unit not in ALLOWED_SYSTEMCTL_START:
            return 126, f"unit not allowlisted for start: {unit}"
    else:
        return 126, f"systemctl subcommand not allowlisted: {subcommand}"

    env = _host_env()
    last_output = "systemctl unavailable"
    for strategy in command_strategies(["systemctl", subcommand, unit]):
        rc, out = _run_raw_capture(strategy, timeout=timeout, env=env, on_line=on_line)
        if rc != 127:
            return rc, out
        last_output = out
    return 127, last_output


def run_host_script(
    script_path: str,
    extra_args: Sequence[str] = (),
    *,
    timeout: float = LONG_TIMEOUT,
    on_line: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """Execute a known script on the host via chroot/nsenter (bash, no shell=True)."""
    host_script = script_path
    if script_path.startswith("/") and HOST_ROOT.is_dir():
        host_script = str(HOST_ROOT / script_path.lstrip("/"))

    if not Path(host_script).is_file() and not Path(script_path).is_file():
        return 127, f"script not found: {script_path}"

    # Scripts live on the host filesystem; invoke through host bash.
    argv = ["bash", script_path, *extra_args]
    env = _host_env()
    last_output = "host bash unavailable"
    for strategy in command_strategies(argv):
        rc, out = _run_raw_capture(strategy, timeout=timeout, env=env, on_line=on_line)
        if rc != 127:
            return rc, out
        last_output = out
    return 127, last_output


def run_docker_exec(
    container: str,
    exec_argv: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    on_line: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """docker exec into an allowlisted container (Canary refresh)."""
    if container != CANARY_CONTAINER:
        return 126, f"container not allowlisted: {container}"

    argv = ["docker", "exec", container, *exec_argv]
    env = _host_env()
    last_output = "docker unavailable"
    for strategy in command_strategies(argv):
        rc, out = _run_raw_capture(strategy, timeout=timeout, env=env, on_line=on_line)
        if rc != 127:
            return rc, out
        last_output = out
    return 127, last_output


def wait_for_unit_inactive(
    unit: str,
    *,
    timeout: float = 600.0,
    poll_interval: float = 2.0,
) -> tuple[bool, str]:
    """Poll until a oneshot unit is no longer active (or timeout)."""
    from host_commands import systemctl_is_active

    deadline = time.monotonic() + timeout
    last_state = "unknown"
    while time.monotonic() < deadline:
        ok, state = systemctl_is_active(unit, timeout=5.0)
        last_state = state if ok else state
        if state in ("inactive", "failed", "dead", "not-found"):
            return True, state
        if state not in ("active", "activating", "reloading"):
            return True, state
        time.sleep(poll_interval)
    return False, last_state
