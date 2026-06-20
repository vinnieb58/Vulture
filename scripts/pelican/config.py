"""Pelican backup configuration and discovery constants."""

from __future__ import annotations

import os
import re
from pathlib import Path

SCRIPT_VERSION = "1.0.0"

DEFAULT_REPO_ROOT = Path(os.environ.get("PELICAN_REPO_ROOT", "/home/vinnieb58/projects/vulture"))
DEFAULT_BACKUP_TARGET = Path(
    os.environ.get("PELICAN_BACKUP_TARGET", "/mnt/storage/pelican_backup")
)
DEFAULT_DB_PATH = Path(
    os.environ.get("PELICAN_DB_PATH", str(DEFAULT_REPO_ROOT / "data" / "vulture.db"))
)
DEFAULT_ENV_PATH = Path(os.environ.get("PELICAN_ENV_PATH", str(DEFAULT_REPO_ROOT / ".env")))
DEFAULT_RETENTION_COUNT = int(os.environ.get("PELICAN_RETENTION_COUNT", "14"))
DEFAULT_MOUNT_ACCESS_TIMEOUT = float(os.environ.get("PELICAN_MOUNT_TIMEOUT", "5.0"))

STAGING_DIR_NAME = ".pelican-staging"
INCOMPLETE_SUFFIX = ".incomplete"
COMPLETE_SUFFIX = ".tar.zst"  # preferred when zstd(1) is available; falls back to .tar.gz

# Completed bundles: raven-recovery-YYYYMMDDTHHMMSSZ.tar.zst|.tar.gz
BACKUP_BUNDLE_RE = re.compile(
    r"^raven-recovery-(?P<stamp>\d{8}T\d{6}Z)\.tar\.(?:zst|gz)$"
)
INCOMPLETE_DIR_RE = re.compile(
    r"^raven-recovery-(?P<stamp>\d{8}T\d{6}Z)\.incomplete$"
)

AUTOFS_SOURCES = frozenset({"systemd-1", "autofs", "none"})

# Installed systemd units to capture when present (Aviary / Vulture stack).
INSTALLED_SYSTEMD_GLOBS = (
    "vulture-*.service",
    "vulture-*.timer",
    "finch-*.service",
    "finch-*.timer",
    "kestrel-*.service",
    "kestrel-*.timer",
    "crow-*.service",
    "crow-*.timer",
    "canary-*.service",
    "canary-*.timer",
    "dashboard-*.service",
    "dashboard-*.timer",
    "pelican-*.service",
    "pelican-*.timer",
)

OPTIONAL_HOST_PATHS = (
    Path("/etc/fstab"),
    Path("/etc/samba/smb.conf"),
)

OPTIONAL_SYSTEMD_DIR = Path("/etc/systemd/system")

REPO_DOCKER_COMPOSE_FILES = (
    "docker-compose.dashboard.yml",
    "docker-compose.canary.yml",
)

# Recovery documentation — discovered if present under repo docs/.
RECOVERY_DOC_PATTERNS = (
    "docs/current/AVIARY_PROJECT_CONTEXT.md",
    "docs/current/OPERATING_MODEL.md",
    "docs/current/PROJECT_STATUS.md",
    "docs/current/CODEBASE_STATUS.md",
    "docs/current/RAVEN_SYSTEMD_RUNTIME.md",
    "docs/current/RAVEN_RESTART_SURVIVAL_PLAN.md",
    "docs/current/RAVEN_BOOT_WARNINGS.md",
    "docs/current/VULTURE_2_0_ROADMAP.md",
    "docs/current/VULTURE_2_0_CURRENT_STATUS.md",
    "docs/current/KESTREL_OPERATIONS.md",
    "docs/current/SESSION_LOG.md",
    "docs/CROW_V0_1.md",
    "docs/CROW_V0_2.md",
    "docs/current/PELiCAN_BACKUP.md",
)

# Also pick up any Pelican-specific docs by filename.
RECOVERY_DOC_GLOBS = (
    "docs/**/*pelican*.md",
    "docs/**/*Pelican*.md",
    "docs/**/*PELiCAN*.md",
    "docs/**/*recovery*.md",
    "docs/**/*RECOVERY*.md",
    "docs/**/*reboot*.md",
    "docs/**/*REBOOT*.md",
)

# Paths excluded from repository working-tree capture (untracked/operational bulk).
REPO_EXCLUDE_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        "logs",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)

REPO_EXCLUDE_GLOBS = (
    "**/.venv/**",
    "**/__pycache__/**",
    "**/*.pyc",
    "**/logs/**",
    "**/experiments/debug/**",
    "**/.env",
    "**/data/*.db",
    "**/data/*.sqlite",
    "**/data/*.sqlite3",
    "**/*.trace.zip",
    "**/experiments/simplyfresh_probe/.auth/**",
    "**/experiments/simplyfresh_probe/artifacts/**",
)

# Secret-like keys that must never appear in manifests or logs.
SECRET_KEY_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key|"
    r"discord[_-]?token|auth[_-]?token|bearer|credential|client[_-]?secret)"
)
