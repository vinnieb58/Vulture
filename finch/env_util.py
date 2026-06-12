"""Load repo-root .env for Finch CLIs."""

from __future__ import annotations


def load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
