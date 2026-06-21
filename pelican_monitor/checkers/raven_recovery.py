"""
Raven recovery bundle backup checker — first Pelican-managed backup definition.

Validates pelican-backup timer/service, real mount, archive freshness, and checksum.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pelican_monitor import config
from pelican_monitor.path_util import host_path, path_access_check
from pelican_monitor.results import BackupCheckResult, combine_status
from pelican_monitor.subprocess_util import is_timeout, run_command

BACKUP_BUNDLE_RE = re.compile(
    r"^raven-recovery-(?P<stamp>\d{8}T\d{6}Z)\.tar\.(?:zst|gz)$"
)
AUTOFS_SOURCES = frozenset({"systemd-1", "autofs", "none"})


@dataclass(frozen=True)
class CompletedArchive:
    path: Path
    name: str
    stamp: str
    mtime: datetime


def _checked_at() -> str:
    tz = ZoneInfo(config.DISPLAY_TIMEZONE)
    return datetime.now(tz=timezone.utc).astimezone(tz).isoformat(timespec="seconds")


def _normalize_systemctl_value(raw: str, *, ok: bool) -> str:
    text = (raw or "").strip().lower()
    if ok and text:
        return text.splitlines()[0].strip()
    lowered = text
    if "not found" in lowered or "could not be found" in lowered:
        return "not-found"
    if is_timeout(raw) or "systemd" in lowered or "bus" in lowered:
        return "unavailable"
    if text:
        return text.splitlines()[0][:80]
    return "unknown"


def _systemctl_property(unit: str, prop: str) -> str:
    ok, out = run_command(
        ["systemctl", "show", unit, f"--property={prop}", "--value"],
        timeout=config.TIMEOUT_SYSTEMCTL,
    )
    return _normalize_systemctl_value(out, ok=ok)


def _timer_next_run(unit: str) -> tuple[bool, str | None]:
    raw = _systemctl_property(unit, "NextElapseUSecRealtime")
    if raw in ("unknown", "unavailable", "not-found", ""):
        return False, None
    try:
        usec = int(raw)
    except ValueError:
        return False, None
    if usec <= 0:
        return False, None
    try:
        ts = datetime.fromtimestamp(usec / 1_000_000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return False, None
    return True, ts.isoformat(timespec="seconds")


def evaluate_timer_health(timer_unit: str) -> dict[str, Any]:
    enabled = _systemctl_property(timer_unit, "UnitFileState")
    active = _systemctl_property(timer_unit, "ActiveState")
    has_future, next_run = _timer_next_run(timer_unit)

    enabled_ok = enabled in ("enabled", "static")
    active_ok = active == "active"

    issues: list[tuple[str, str, str]] = []
    if not enabled_ok:
        issues.append(("critical", "TIMER_DISABLED", f"{timer_unit} is {enabled}"))
    if not active_ok:
        issues.append(("critical", "TIMER_INACTIVE", f"{timer_unit} is {active}"))
    if active_ok and not has_future:
        issues.append(("critical", "TIMER_NO_FUTURE_RUN", f"{timer_unit} has no scheduled next run"))

    status = "ok" if not issues else "critical"
    return {
        "unit": timer_unit,
        "enabled": enabled,
        "active": active,
        "next_run": next_run,
        "has_future_run": has_future,
        "status": status,
        "issues": issues,
    }


def evaluate_service_result(service_unit: str) -> dict[str, Any]:
    active = _systemctl_property(service_unit, "ActiveState")
    result = _systemctl_property(service_unit, "Result")
    exec_status_raw = _systemctl_property(service_unit, "ExecMainStatus")
    started_raw = _systemctl_property(service_unit, "ExecMainStartTimestamp")

    exec_status = int(exec_status_raw) if exec_status_raw.isdigit() else None
    has_run = bool(started_raw and started_raw not in ("", "n/a", "unknown", "unavailable"))
    failed = False
    failure_reason: str | None = None

    if active == "failed":
        failed = True
        failure_reason = f"{service_unit} is failed"
    elif has_run and result not in ("success", "n/a"):
        failed = True
        failure_reason = f"last run result={result}"
    elif has_run and exec_status not in (None, 0):
        failed = True
        failure_reason = f"last run exit status={exec_status}"

    inactive_ok = active in ("inactive", "dead") and not failed
    issues: list[tuple[str, str, str]] = []
    if failed:
        issues.append(("critical", "SERVICE_LAST_RUN_FAILED", failure_reason or "backup service failed"))

    return {
        "unit": service_unit,
        "active": active,
        "result": result,
        "exec_main_status": exec_status,
        "last_run_started": started_raw if has_run else None,
        "has_run": has_run,
        "inactive_between_runs_ok": inactive_ok,
        "status": "critical" if failed else "ok",
        "issues": issues,
    }


def _is_autofs_placeholder(source: str | None, fstype: str | None) -> bool:
    source_l = (source or "").lower()
    fstype_l = (fstype or "").lower()
    return source_l in AUTOFS_SOURCES or fstype_l == "autofs"


def evaluate_backup_target_mount(target: str) -> dict[str, Any]:
    resolved = host_path(target)
    issues: list[tuple[str, str, str]] = []

    ok_access, access_err = path_access_check(resolved, timeout=config.TIMEOUT_PATH)
    if not ok_access:
        issues.append(("critical", "MOUNT_UNAVAILABLE", f"Backup target unavailable: {access_err}"))
        return {
            "path": target,
            "resolved_path": resolved,
            "mounted": False,
            "backing_source": None,
            "backing_fstype": None,
            "status": "critical",
            "issues": issues,
        }

    ok, out = run_command(
        ["findmnt", "--mountpoint", resolved, "-n", "-o", "SOURCE,FSTYPE"],
        timeout=config.TIMEOUT_FINDMNT,
    )
    if not ok or not out.strip():
        issues.append(("critical", "MOUNT_UNAVAILABLE", "Backup target is not a mountpoint with a backing device"))
        return {
            "path": target,
            "resolved_path": resolved,
            "mounted": False,
            "backing_source": None,
            "backing_fstype": None,
            "status": "critical",
            "issues": issues,
        }

    best_source: str | None = None
    best_fstype: str | None = None
    real_source: str | None = None
    real_fstype: str | None = None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        source, fstype = parts[0], parts[1]
        if not _is_autofs_placeholder(source, fstype):
            real_source, real_fstype = source, fstype
            break
        best_source, best_fstype = source, fstype

    if real_source is None:
        issues.append(
            (
                "critical",
                "MOUNT_AUTOFS_PLACEHOLDER",
                f"Automount placeholder detected (source={best_source}, fstype={best_fstype})",
            )
        )
        return {
            "path": target,
            "resolved_path": resolved,
            "mounted": False,
            "backing_source": best_source,
            "backing_fstype": best_fstype,
            "status": "critical",
            "issues": issues,
        }

    ok_root, root_out = run_command(
        ["findmnt", "--mountpoint", host_path("/"), "-n", "-o", "SOURCE"],
        timeout=config.TIMEOUT_FINDMNT,
    )
    root_source = root_out.split()[0] if ok_root and root_out.strip() else None
    if root_source and real_source == root_source:
        issues.append(
            (
                "critical",
                "MOUNT_UNAVAILABLE",
                "Backup target resolves to root filesystem (empty autofs directory)",
            )
        )
        return {
            "path": target,
            "resolved_path": resolved,
            "mounted": False,
            "backing_source": real_source,
            "backing_fstype": real_fstype,
            "status": "critical",
            "issues": issues,
        }

    return {
        "path": target,
        "resolved_path": resolved,
        "mounted": True,
        "backing_source": real_source,
        "backing_fstype": real_fstype,
        "status": "ok",
        "issues": issues,
    }


def find_latest_completed_archive(target_dir: Path) -> CompletedArchive | None:
    latest: CompletedArchive | None = None
    try:
        entries = list(target_dir.iterdir())
    except OSError:
        return None

    for entry in entries:
        if not entry.is_file():
            continue
        match = BACKUP_BUNDLE_RE.match(entry.name)
        if not match:
            continue
        stamp = match.group("stamp")
        try:
            mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        candidate = CompletedArchive(path=entry, name=entry.name, stamp=stamp, mtime=mtime)
        if latest is None or candidate.stamp > latest.stamp:
            latest = candidate
    return latest


def _stamp_to_datetime(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def _parse_sha256_sidecar(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        token = line.split()[0]
        if re.fullmatch(r"[0-9a-fA-F]{64}", token):
            return token.lower()
    return None


def _compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def evaluate_latest_archive(
    target_dir: Path,
    *,
    stale_hours: float,
    warn_hours: float,
) -> dict[str, Any]:
    issues: list[tuple[str, str, str]] = []
    archive = find_latest_completed_archive(target_dir)

    if archive is None:
        issues.append(("critical", "NO_COMPLETED_ARCHIVE", "No completed raven-recovery archive found"))
        return {
            "latest_name": None,
            "latest_stamp": None,
            "latest_mtime": None,
            "age_hours": None,
            "checksum_path": None,
            "checksum_valid": None,
            "readable": None,
            "checksum_status": "not_checked",
            "status": "critical",
            "issues": issues,
        }

    readable = True
    try:
        with archive.path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        readable = False
        issues.append(("critical", "ARCHIVE_UNREADABLE", f"Latest archive not readable: {exc}"))

    checksum_path = Path(f"{archive.path}.sha256")
    checksum_valid: bool | None = None
    checksum_status = "not_checked"
    if not checksum_path.is_file():
        checksum_status = "missing"
        issues.append(("critical", "CHECKSUM_MISSING", f"Missing checksum sidecar for {archive.name}"))
    else:
        try:
            sidecar_text = checksum_path.read_text(encoding="utf-8")
        except OSError as exc:
            checksum_status = "missing"
            issues.append(("critical", "CHECKSUM_MISSING", f"Cannot read checksum sidecar: {exc}"))
        else:
            expected = _parse_sha256_sidecar(sidecar_text)
            if expected is None:
                checksum_status = "invalid"
                issues.append(("critical", "CHECKSUM_INVALID", "Checksum sidecar has no valid sha256 digest"))
            else:
                actual = _compute_sha256(archive.path)
                checksum_valid = actual == expected
                checksum_status = "ok" if checksum_valid else "invalid"
                if not checksum_valid:
                    issues.append(("critical", "CHECKSUM_INVALID", f"Checksum mismatch for {archive.name}"))

    stamp_time = _stamp_to_datetime(archive.stamp)
    age_hours = (datetime.now(timezone.utc) - stamp_time).total_seconds() / 3600.0

    if age_hours >= stale_hours:
        issues.append(
            (
                "critical",
                "BACKUP_STALE",
                f"Latest recovery bundle is {age_hours:.0f} hours old (threshold {stale_hours:.0f}h)",
            )
        )
    elif age_hours >= warn_hours:
        issues.append(
            (
                "warning",
                "BACKUP_APPROACHING_STALE",
                f"Latest recovery bundle is {age_hours:.0f} hours old (warn at {warn_hours:.0f}h, stale at {stale_hours:.0f}h)",
            )
        )

    severities = [sev for sev, _, _ in issues]
    if "critical" in severities:
        status = "critical"
    elif "warning" in severities:
        status = "warning"
    elif not readable:
        status = "critical"
    else:
        status = "ok"

    return {
        "latest_name": archive.name,
        "latest_stamp": archive.stamp,
        "latest_mtime": archive.mtime.isoformat(timespec="seconds"),
        "age_hours": round(age_hours, 2),
        "checksum_path": str(checksum_path),
        "checksum_valid": checksum_valid,
        "checksum_status": checksum_status,
        "readable": readable,
        "status": status,
        "issues": issues,
    }


def _primary_reason(issues: list[tuple[str, str, str]]) -> str:
    if not issues:
        return "Backup healthy"
    for severity in ("critical", "warning"):
        for sev, _code, message in issues:
            if sev == severity:
                return message
    return issues[0][2]


def check_raven_recovery() -> BackupCheckResult:
    """Run the Raven recovery bundle backup check and return a standardized result."""
    timer = evaluate_timer_health(config.RAVEN_RECOVERY_TIMER_UNIT)
    service = evaluate_service_result(config.RAVEN_RECOVERY_SERVICE_UNIT)
    mount = evaluate_backup_target_mount(config.RAVEN_RECOVERY_TARGET)

    if mount["status"] == "ok":
        archive = evaluate_latest_archive(
            Path(mount["resolved_path"]),
            stale_hours=config.RAVEN_RECOVERY_CRITICAL_HOURS,
            warn_hours=config.RAVEN_RECOVERY_WARN_HOURS,
        )
    else:
        archive = {
            "latest_name": None,
            "latest_stamp": None,
            "latest_mtime": None,
            "age_hours": None,
            "checksum_path": None,
            "checksum_valid": None,
            "checksum_status": "not_checked",
            "readable": None,
            "status": "critical",
            "issues": [("critical", "MOUNT_UNAVAILABLE", "Skipping archive check: mount unavailable")],
        }

    all_issues: list[tuple[str, str, str]] = []
    for section in (timer, service, mount, archive):
        all_issues.extend(section.get("issues", []))

    status = combine_status(timer["status"], service["status"], mount["status"], archive["status"])
    issue_codes = sorted({code for _, code, _ in all_issues})

    return BackupCheckResult(
        backup_id="raven_recovery",
        display_name="Pelican backup",
        status=status,
        reason=_primary_reason(all_issues),
        checked_at=_checked_at(),
        newest_backup_timestamp=archive.get("latest_mtime"),
        backup_age_hours=archive.get("age_hours"),
        warn_threshold_hours=config.RAVEN_RECOVERY_WARN_HOURS,
        critical_threshold_hours=config.RAVEN_RECOVERY_CRITICAL_HOURS,
        target_available=bool(mount.get("mounted")),
        checksum_status=str(archive.get("checksum_status") or "not_checked"),
        timer=timer,
        service=service,
        issue_codes=issue_codes,
        details={
            "mount": mount,
            "archive": archive,
        },
    )
