"""Execute read-only commands against the Raven host from the dashboard container."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from subprocess_util import DEFAULT_TIMEOUT

HOST_ROOT = Path(os.environ.get("DASHBOARD_HOST_ROOT", "/host/root"))
SYSTEMD_BUS_SOCKET = os.environ.get(
    "DASHBOARD_SYSTEMD_BUS_SOCKET", "/run/dbus/system_bus_socket"
)

_SYSTEMCTL_STATES = frozenset(
    {
        "active",
        "inactive",
        "failed",
        "activating",
        "deactivating",
        "reloading",
        "refreshing",
        "maintenance",
        "dead",
        "unknown",
        "not-found",
        "masked",
        "enabled",
        "disabled",
        "static",
        "indirect",
        "generated",
        "transient",
    }
)


def _host_env() -> dict[str, str]:
    env = os.environ.copy()
    if Path(SYSTEMD_BUS_SOCKET).exists():
        env["SYSTEMD_BUS_SOCKET"] = SYSTEMD_BUS_SOCKET
    return env


def _tool_missing(message: str) -> bool:
    lower = message.lower()
    return (
        "command not found" in lower
        or "no such file or directory" in lower
        or lower.startswith("os error:")
    )


def _host_binary_exists(binary: str) -> bool:
    if binary.startswith("/"):
        host_path = HOST_ROOT / binary.lstrip("/")
        return host_path.is_file()
    return (HOST_ROOT / "usr/bin" / binary).is_file() or (
        HOST_ROOT / "bin" / binary
    ).is_file()


def command_strategies(args: Sequence[str]) -> list[list[str]]:
    """Build command strategies: chroot host, nsenter pid 1, then local binary."""
    argv = list(args)
    if not argv:
        return []

    binary = argv[0]
    strategies: list[list[str]] = []

    if HOST_ROOT.is_dir() and _host_binary_exists(binary):
        strategies.append(["chroot", str(HOST_ROOT), *argv])

    if shutil.which("nsenter"):
        strategies.append(["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", *argv])

    if shutil.which(binary):
        strategies.append(argv)

    return strategies


def _run_raw(
    args: Sequence[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    try:
        result = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        return 127, f"command not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as exc:
        return 125, f"os error: {exc}"

    out = (result.stdout or result.stderr or "").strip()
    if len(out) > 400:
        out = out[:400] + "…"
    return result.returncode, out


def run_host_command(
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """Run a host command; returns success only on exit code 0."""
    env = _host_env()
    last_error = "unavailable"
    for strategy in command_strategies(args):
        rc, out = _run_raw(strategy, timeout=timeout, env=env)
        if rc == 0:
            return True, out
        if out and not _tool_missing(out):
            last_error = out
        elif out:
            last_error = out
    return False, last_error


def run_systemctl(
    subargs: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """
    Run systemctl against the host.

    is-active / is-enabled may exit non-zero while still returning a useful state.
    """
    env = _host_env()
    subcmd = subargs[0] if subargs else ""
    last_error = "systemctl unavailable"

    for strategy in command_strategies(["systemctl", *subargs]):
        rc, out = _run_raw(strategy, timeout=timeout, env=env)
        if _tool_missing(out):
            last_error = out or last_error
            continue

        if subcmd in ("is-active", "is-enabled"):
            state = (out.splitlines()[0] if out else "").strip().lower()
            if state in _SYSTEMCTL_STATES or state:
                return True, state
            if rc in (0, 1, 3, 4):
                return True, state or "unknown"

        if rc == 0 and out:
            return True, out

        if out and not _tool_missing(out):
            last_error = out

    return False, last_error


def systemctl_is_active(unit: str, *, timeout: float = 5.0) -> tuple[bool, str]:
    ok, state = run_systemctl(["is-active", unit], timeout=timeout)
    if not ok:
        return False, state
    return True, state


def systemctl_is_enabled(unit: str, *, timeout: float = 5.0) -> tuple[bool, str]:
    ok, state = run_systemctl(["is-enabled", unit], timeout=timeout)
    if not ok:
        return False, state
    return True, state


def systemctl_unit_exists(unit: str, *, timeout: float = 5.0) -> bool:
    ok, state = systemctl_is_enabled(unit, timeout=timeout)
    if not ok:
        return False
    return state not in ("not-found", "", "unknown") and "could not be found" not in state
