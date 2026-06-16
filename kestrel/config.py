"""Kestrel configuration — paths and thresholds only at import time; secrets loaded on demand."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

PROVIDER_SMART_METER_TEXAS = "smart_meter_texas"

DEFAULT_DATA_DIR = Path("data/kestrel")
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "kestrel.db"
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_TIMEZONE = "America/Chicago"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def load_dotenv_if_available() -> None:
    """Load repo-root .env when running Kestrel probes (does not affect Vulture startup)."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


@dataclass(frozen=True)
class KestrelConfig:
    enabled: bool
    smt_username: str | None
    smt_password: str | None
    smt_account_id: str | None
    data_dir: Path
    db_path: Path
    lookback_days: int
    headless: bool
    log_level: str
    timezone: str

    @property
    def has_smt_credentials(self) -> bool:
        return bool(self.smt_username and self.smt_password)


class KestrelConfigError(Exception):
    """Raised when Kestrel configuration is invalid for the requested operation."""


def load_config(*, require_enabled: bool = False, require_credentials: bool = False) -> KestrelConfig:
    """Load Kestrel settings from environment (after optional dotenv)."""
    load_dotenv_if_available()

    enabled = _truthy(os.getenv("KESTREL_ENABLED", "false"))
    username = (os.getenv("KESTREL_SMT_USERNAME") or "").strip() or None
    password = (os.getenv("KESTREL_SMT_PASSWORD") or "").strip() or None
    account_id = (os.getenv("KESTREL_SMT_ACCOUNT_ID") or "").strip() or None

    data_dir = Path(os.getenv("KESTREL_DATA_DIR", str(DEFAULT_DATA_DIR))).resolve()
    db_path = Path(os.getenv("KESTREL_DB_PATH", str(data_dir / "kestrel.db"))).resolve()

    try:
        lookback_days = int(os.getenv("KESTREL_LOOKBACK_DAYS", str(DEFAULT_LOOKBACK_DAYS)))
    except ValueError as exc:
        raise KestrelConfigError("KESTREL_LOOKBACK_DAYS must be an integer") from exc

    if lookback_days < 1:
        raise KestrelConfigError("KESTREL_LOOKBACK_DAYS must be at least 1")

    headless = _truthy(os.getenv("KESTREL_HEADLESS", "true"))

    log_level = (os.getenv("KESTREL_LOG_LEVEL", DEFAULT_LOG_LEVEL) or DEFAULT_LOG_LEVEL).upper()
    timezone = (os.getenv("KESTREL_TIMEZONE", DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE).strip()

    config = KestrelConfig(
        enabled=enabled,
        smt_username=username,
        smt_password=password,
        smt_account_id=account_id,
        data_dir=data_dir,
        db_path=db_path,
        lookback_days=lookback_days,
        headless=headless,
        log_level=log_level,
        timezone=timezone,
    )

    if require_enabled and not config.enabled:
        raise KestrelConfigError(
            "Kestrel is disabled. Set KESTREL_ENABLED=true in .env to run live probes."
        )
    if require_credentials and not config.has_smt_credentials:
        raise KestrelConfigError(
            "Smart Meter Texas credentials missing. Set KESTREL_SMT_USERNAME and "
            "KESTREL_SMT_PASSWORD in .env, or use --import-csv for manual import."
        )

    return config


def setup_logging(level: str = DEFAULT_LOG_LEVEL) -> logging.Logger:
    """Configure stdout logging for Kestrel probes."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
    return logging.getLogger("kestrel")
