"""Safe read-only subprocess helpers for the Vulture Dashboard."""

from __future__ import annotations

import subprocess
from typing import Sequence

DEFAULT_TIMEOUT = 10.0


def run_command(
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Run a command without shell=True. Returns (success, stdout or error snippet)."""
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
