"""
Injectable, defensive subprocess helpers for read-only host checks.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from canary.config import DEFAULT_SUBPROCESS_TIMEOUT

CommandRunner = Callable[[Sequence[str], float], tuple[bool, str]]

_runner: CommandRunner | None = None


def set_command_runner(runner: CommandRunner | None) -> None:
    """Replace subprocess execution (used by unit tests)."""
    global _runner
    _runner = runner


def default_run_command(
    args: Sequence[str],
    timeout: float = DEFAULT_SUBPROCESS_TIMEOUT,
) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return False, f"command not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError as exc:
        return False, f"os error: {exc}"

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        if len(err) > 200:
            err = err[:200] + "…"
        return False, err or f"exit {result.returncode}"

    return True, (result.stdout or "").strip()


def run_command(
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_SUBPROCESS_TIMEOUT,
) -> tuple[bool, str]:
    if _runner is not None:
        return _runner(args, timeout)
    return default_run_command(args, timeout=timeout)
