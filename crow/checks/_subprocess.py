"""
Safe read-only subprocess helpers.
"""

from __future__ import annotations

import subprocess
from typing import Sequence

from crow.config import DEFAULT_SUBPROCESS_TIMEOUT


def run_command(
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_SUBPROCESS_TIMEOUT,
) -> tuple[bool, str]:
    """
    Run a command without shell=True. Returns (success, stdout or error snippet).
    Never includes environment or secrets in output.
    """
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
