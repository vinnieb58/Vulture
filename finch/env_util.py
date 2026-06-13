"""Load repo-root .env for Finch CLIs."""

from __future__ import annotations

import os
from pathlib import Path

_dotenv_loaded = False


def skip_dotenv() -> bool:
    return os.getenv("FINCH_SKIP_DOTENV", "").strip().lower() in ("1", "true", "yes")


def load_env(
    *,
    force: bool = False,
    dotenv_path: str | Path | None = None,
) -> None:
    """Load environment variables from .env unless FINCH_SKIP_DOTENV is set."""
    global _dotenv_loaded
    if skip_dotenv():
        return
    if _dotenv_loaded and not force and dotenv_path is None:
        return
    try:
        from dotenv import load_dotenv

        if dotenv_path is not None:
            load_dotenv(dotenv_path=dotenv_path, override=True)
        else:
            load_dotenv()
        _dotenv_loaded = True
    except ImportError:
        pass


def reset_env_load_state() -> None:
    """Reset dotenv load tracking (tests only)."""
    global _dotenv_loaded
    _dotenv_loaded = False
