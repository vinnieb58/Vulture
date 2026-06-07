"""
Path access with timeouts — stale mounts can hang naive stat/listdir calls.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="canary-path")


def path_access_check(path: str, *, timeout: float) -> tuple[bool, str | None]:
    """
    Verify a path is reachable (exists + stat or single dir list).
    Returns (ok, error_message).
    """

    def _probe() -> tuple[bool, str | None]:
        target = Path(path)
        try:
            if not target.exists():
                return False, "path not found"
            if target.is_dir():
                next(target.iterdir(), None)
            else:
                target.stat()
        except OSError as exc:
            return False, str(exc)
        return True, None

    future = _executor.submit(_probe)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        future.cancel()
        return False, "path access timed out"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
