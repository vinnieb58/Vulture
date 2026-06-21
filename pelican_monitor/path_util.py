"""Host path resolution and bounded path access probes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

from pelican_monitor import config

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pelican-monitor-path")


def host_path(path: str) -> str:
    if config.HOST_ROOT == Path("/"):
        return path
    if path == "/":
        return str(config.HOST_ROOT)
    return str(config.HOST_ROOT / path.lstrip("/"))


def path_access_check(path: str, *, timeout: float) -> tuple[bool, str | None]:
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
