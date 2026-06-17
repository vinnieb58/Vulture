"""Robin configuration — paths and thresholds only at import time; secrets loaded on demand."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = Path("data/robin")
DEFAULT_OUTPUT_DIR = DEFAULT_DATA_DIR
DEFAULT_SESSION_DIR = DEFAULT_DATA_DIR / "session"
DEFAULT_MANIFEST_PATH = DEFAULT_DATA_DIR / "manifest.db"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_PORTAL_SOURCE = "daycare_portal"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def load_dotenv_if_available() -> None:
    """Load repo-root .env when running Robin probes (does not affect Vulture startup)."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


@dataclass(frozen=True)
class RobinConfig:
    username: str | None
    password: str | None
    portal_url: str | None
    session_dir: Path
    output_dir: Path
    manifest_path: Path
    headless: bool
    log_level: str
    portal_source: str

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password)

    @property
    def has_portal_url(self) -> bool:
        return bool(self.portal_url)


class RobinConfigError(Exception):
    """Raised when Robin configuration is invalid for the requested operation."""


def load_config(*, require_portal_url: bool = False) -> RobinConfig:
    """Load Robin settings from environment (after optional dotenv)."""
    load_dotenv_if_available()

    username = (os.getenv("ROBIN_DAYCARE_USERNAME") or "").strip() or None
    password = (os.getenv("ROBIN_DAYCARE_PASSWORD") or "").strip() or None
    portal_url = (os.getenv("ROBIN_DAYCARE_PORTAL_URL") or "").strip() or None

    output_dir = Path(os.getenv("ROBIN_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR))).resolve()
    session_dir = Path(os.getenv("ROBIN_SESSION_DIR", str(DEFAULT_SESSION_DIR))).resolve()
    manifest_path = Path(
        os.getenv("ROBIN_MANIFEST_PATH", str(output_dir / "manifest.db"))
    ).resolve()

    headless = not _truthy(os.getenv("ROBIN_HEADFUL", "false"))
    log_level = (os.getenv("ROBIN_LOG_LEVEL", DEFAULT_LOG_LEVEL) or DEFAULT_LOG_LEVEL).upper()
    portal_source = (
        os.getenv("ROBIN_PORTAL_SOURCE", DEFAULT_PORTAL_SOURCE) or DEFAULT_PORTAL_SOURCE
    ).strip()

    config = RobinConfig(
        username=username,
        password=password,
        portal_url=portal_url,
        session_dir=session_dir,
        output_dir=output_dir,
        manifest_path=manifest_path,
        headless=headless,
        log_level=log_level,
        portal_source=portal_source,
    )

    if require_portal_url and not config.has_portal_url:
        raise RobinConfigError(
            "Daycare portal URL missing. Set ROBIN_DAYCARE_PORTAL_URL in .env."
        )

    return config


def setup_logging(level: str = DEFAULT_LOG_LEVEL) -> logging.Logger:
    """Configure stdout logging for Robin probes."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
    return logging.getLogger("robin")
