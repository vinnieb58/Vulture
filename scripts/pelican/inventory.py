"""Required versus optional backup source inventory."""

from __future__ import annotations

import fnmatch
import glob
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import (
    INSTALLED_SYSTEMD_GLOBS,
    OPTIONAL_HOST_PATHS,
    OPTIONAL_SYSTEMD_DIR,
    RECOVERY_DOC_GLOBS,
    RECOVERY_DOC_PATTERNS,
    REPO_DOCKER_COMPOSE_FILES,
    REPO_EXCLUDE_DIR_NAMES,
    REPO_EXCLUDE_GLOBS,
)


@dataclass
class InventoryResult:
    included: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)
    required_failures: list[str] = field(default_factory=list)


def discover_recovery_docs(repo_root: Path) -> list[Path]:
    found: dict[str, Path] = {}
    for rel in RECOVERY_DOC_PATTERNS:
        candidate = repo_root / rel
        if candidate.is_file():
            found[str(candidate.resolve())] = candidate

    for pattern in RECOVERY_DOC_GLOBS:
        for match in repo_root.glob(pattern):
            if match.is_file():
                found[str(match.resolve())] = match

    return sorted(found.values(), key=lambda p: str(p))


def discover_installed_systemd_units(systemd_dir: Path = OPTIONAL_SYSTEMD_DIR) -> list[Path]:
    if not systemd_dir.is_dir():
        return []
    matches: dict[str, Path] = {}
    for pattern in INSTALLED_SYSTEMD_GLOBS:
        for path in systemd_dir.glob(pattern):
            if path.is_file():
                matches[str(path.resolve())] = path
    return sorted(matches.values(), key=lambda p: p.name)


def discover_samba_configs(smb_conf: Path = Path("/etc/samba/smb.conf")) -> list[Path]:
    if not smb_conf.is_file():
        return []
    configs = [smb_conf]
    try:
        text = smb_conf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return configs

    include_re = re.compile(r"^\s*include\s*=\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
    base = smb_conf.parent
    for match in include_re.finditer(text):
        pattern = match.group(1).strip()
        for included in glob.glob(str(base / pattern)):
            path = Path(included)
            if path.is_file() and path not in configs:
                configs.append(path)
    return configs


def _path_matches_glob(rel_posix: str, pattern: str) -> bool:
    return fnmatch.fnmatch(rel_posix, pattern)


def should_exclude_repo_path(rel_path: Path) -> bool:
    if rel_path.name == ".env":
        return True
    parts = rel_path.parts
    if any(part in REPO_EXCLUDE_DIR_NAMES for part in parts):
        return True
    rel_posix = rel_path.as_posix()
    return any(_path_matches_glob(rel_posix, pattern) for pattern in REPO_EXCLUDE_GLOBS)


def classify_required_paths(
    *,
    repo_root: Path,
    db_path: Path,
    env_path: Path,
) -> InventoryResult:
    result = InventoryResult()
    if not repo_root.is_dir():
        result.required_failures.append(f"Repository root missing: {repo_root}")
    if not db_path.is_file():
        result.required_failures.append(f"SQLite database missing: {db_path}")
    if not env_path.is_file():
        result.required_failures.append(f"Secrets file missing: {env_path}")
    return result


def ensure_parent_directory(path: Path) -> None:
    """Create all parent directories for a destination file path."""
    path.parent.mkdir(parents=True, exist_ok=True)


def ensure_directory(path: Path) -> None:
    """Create a destination directory and any missing parents."""
    path.mkdir(parents=True, exist_ok=True)


def copy_file_to_dest(source: Path, dest: Path, result: InventoryResult) -> None:
    """Copy a file after ensuring nested destination directories exist."""
    ensure_parent_directory(dest)
    shutil.copy2(source, dest)
    result.included.append(str(dest))


def copy_optional_file(source: Path, dest: Path, result: InventoryResult) -> None:
    label = str(source)
    if not source.is_file():
        result.missing_optional.append(label)
        return
    copy_file_to_dest(source, dest, result)


def copy_repo_docker_compose(repo_root: Path, dest_dir: Path, result: InventoryResult) -> None:
    ensure_directory(dest_dir)
    for name in REPO_DOCKER_COMPOSE_FILES:
        source = repo_root / name
        dest = dest_dir / name
        copy_optional_file(source, dest, result)


def copy_repo_systemd_defs(repo_root: Path, dest_dir: Path, result: InventoryResult) -> None:
    src_dir = repo_root / "deploy" / "systemd"
    if not src_dir.is_dir():
        result.missing_optional.append(str(src_dir))
        return
    ensure_directory(dest_dir)
    for unit in sorted(src_dir.iterdir()):
        if unit.is_file():
            copy_file_to_dest(unit, dest_dir / unit.name, result)


def copy_recovery_docs(repo_root: Path, dest_dir: Path, result: InventoryResult) -> None:
    docs = discover_recovery_docs(repo_root)
    if not docs:
        result.missing_optional.append("recovery documentation (none discovered)")
        return
    ensure_directory(dest_dir)
    for doc in docs:
        rel = doc.relative_to(repo_root)
        copy_file_to_dest(doc, dest_dir / rel, result)


def copy_optional_host_config(dest_root: Path, result: InventoryResult) -> None:
    ensure_directory(dest_root / "host")
    for source in OPTIONAL_HOST_PATHS:
        dest = dest_root / "host" / source.relative_to(source.anchor)
        copy_optional_file(source, dest, result)

    systemd_dest = dest_root / "systemd-installed"
    installed = discover_installed_systemd_units()
    if not installed:
        result.missing_optional.append(f"{OPTIONAL_SYSTEMD_DIR}/<aviary-units>")
    else:
        ensure_directory(systemd_dest)
    for unit in installed:
        copy_optional_file(unit, systemd_dest / unit.name, result)

    samba_dest = dest_root / "samba"
    samba_files = discover_samba_configs()
    if not samba_files:
        result.missing_optional.append("/etc/samba/smb.conf")
    else:
        ensure_directory(samba_dest)
    for conf in samba_files:
        copy_optional_file(conf, samba_dest / conf.name, result)
