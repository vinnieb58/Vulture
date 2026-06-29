"""Injectable subprocess helpers for Pelican monitor checks."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

CommandRunner = Callable[[Sequence[str], float], tuple[bool, str]]

_runner: CommandRunner | None = None
DEFAULT_TIMEOUT = 10.0


def set_command_runner(runner: CommandRunner | None) -> None:
    global _runner
    _runner = runner


def is_timeout(message: str | None) -> bool:
    if not message:
        return False
    lowered = message.lower()
    return "timed out" in lowered or lowered == "timeout"


def run_command(
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    if _runner is not None:
        return _runner(args, timeout)
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
