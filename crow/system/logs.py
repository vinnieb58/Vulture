"""
Read-only log summaries for Raven / Vulture / Crow (no user-provided paths).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from crow.checks.services import get_journal_excerpt
from crow.config import (
    VULTURE_BOT_SYSTEMD_UNIT,
    VULTURE_LOGS_DIR,
    VULTURE_MAIN_LOG,
    VULTURE_SCHEDULER_SYSTEMD_UNIT,
)
from crow.system._status import StatusLevel

LogSourceState = Literal["ok", "missing", "unavailable"]

_TAIL_MAX_LINES = 500
_TAIL_MAX_BYTES = 256_000
_MAX_ISSUE_LINES = 5
_JOURNAL_LINES = 50

_DISCORD_TOKEN = re.compile(r"[\w-]{24}\.[\w-]{6}\.[\w-]{27,}")
_ENV_SECRET = re.compile(
    r"(?i)\b(DISCORD_BOT_TOKEN|TOKEN|API_KEY|SECRET|PASSWORD)\s*=\s*\S+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_AUTH_HEADER = re.compile(r"(?i)\bauthorization\s*[:=]\s*\S+")
_URL_SECRET_PARAMS = re.compile(
    r"([?&])(token|api_key|apikey|secret|password|access_token|auth)=[^&\s\"']+",
    re.IGNORECASE,
)
_WEBHOOK_URL = re.compile(
    r"https://(?:discord(?:app)?\.com/api/webhooks|hooks\.slack\.com/services)/\S+",
    re.IGNORECASE,
)
_LEVEL_IN_BRACKETS = re.compile(r"\[(WARNING|WARN|ERROR|CRITICAL)\]", re.IGNORECASE)

_CYCLE_PATTERNS = (
    "hunt cycle completed",
    "starting vulture hunt cycle",
)
_BOT_STARTUP_PATTERNS = (
    "starting vulture discord bot",
    "vulture bot ready",
    "logged in as",
)


@dataclass(frozen=True)
class LogSource:
    name: str
    status: LogSourceState
    detail: str | None = None


@dataclass(frozen=True)
class LogsSummary:
    sources: list[LogSource]
    warning_count: int
    error_count: int
    recent_issues: list[str]
    last_cycle_line: str | None
    last_bot_startup_line: str | None
    overall: StatusLevel


def sanitize_log_line(line: str) -> str:
    """Redact common secret patterns from a single log line."""
    text = line.strip()
    if not text:
        return text

    text = _DISCORD_TOKEN.sub("[REDACTED_TOKEN]", text)
    text = _WEBHOOK_URL.sub("[REDACTED_WEBHOOK]", text)
    text = _ENV_SECRET.sub(r"\1=[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _AUTH_HEADER.sub("Authorization: [REDACTED]", text)
    text = _URL_SECRET_PARAMS.sub(r"\1\2=[REDACTED]", text)
    return text


def sanitize_log_text(text: str) -> str:
    """Sanitize multi-line log excerpts."""
    return "\n".join(sanitize_log_line(line) for line in text.splitlines())


def _read_tail_lines(path: Path) -> tuple[list[str] | None, LogSourceState]:
    if not path.exists():
        return None, "missing"
    if not path.is_file():
        return None, "unavailable"
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            read_size = min(size, _TAIL_MAX_BYTES)
            handle.seek(max(0, size - read_size))
            chunk = handle.read(read_size)
        text = chunk.decode("utf-8", errors="replace")
        return text.splitlines()[-_TAIL_MAX_LINES:], "ok"
    except OSError:
        return None, "unavailable"


def _classify_log_level(line: str) -> str | None:
    match = _LEVEL_IN_BRACKETS.search(line)
    if not match:
        return None
    level = match.group(1).upper()
    if level == "WARN":
        return "WARNING"
    return level


def _find_last_matching(lines: list[str], patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        for line in reversed(lines):
            if pattern in line.lower():
                return sanitize_log_line(line)
    return None


def _collect_issue_lines(lines: list[str]) -> tuple[int, int, list[str]]:
    warnings = 0
    errors = 0
    issues: list[str] = []

    for line in lines:
        level = _classify_log_level(line)
        if level == "WARNING":
            warnings += 1
            issues.append(sanitize_log_line(line))
        elif level in ("ERROR", "CRITICAL"):
            errors += 1
            issues.append(sanitize_log_line(line))

    return warnings, errors, issues[-_MAX_ISSUE_LINES:]


def _journal_source(unit: str) -> tuple[list[str], LogSource]:
    excerpt = get_journal_excerpt(unit, lines=_JOURNAL_LINES)
    if excerpt is None:
        return [], LogSource(f"journal:{unit}", "unavailable", "journal excerpt unavailable")
    sanitized = sanitize_log_text(excerpt)
    lines = sanitized.splitlines()
    return lines, LogSource(f"journal:{unit}", "ok", f"{len(lines)} recent line(s)")


def _logs_overall(
    *,
    warning_count: int,
    error_count: int,
    sources: list[LogSource],
) -> StatusLevel:
    if error_count > 0:
        return "fail"
    if warning_count > 0:
        return "warn"
    if any(source.status == "unavailable" for source in sources):
        return "warn"
    if any(source.status == "missing" for source in sources):
        return "warn"
    return "ok"


def get_logs_summary(
    *,
    main_log: Path | None = None,
    logs_dir: Path | None = None,
) -> LogsSummary:
    """
  Summarize recent Raven/Vulture/Crow logs from known safe locations only.
  """
    log_path = main_log or VULTURE_MAIN_LOG
    log_dir = logs_dir or VULTURE_LOGS_DIR

    sources: list[LogSource] = []
    all_lines: list[str] = []

    file_lines, file_status = _read_tail_lines(log_path)
    if file_status == "ok" and file_lines is not None:
        sources.append(
            LogSource(
                "vulture.log",
                "ok",
                f"{len(file_lines)} tail line(s) from {log_path.name}",
            )
        )
        all_lines.extend(file_lines)
    elif file_status == "missing":
        sources.append(LogSource("vulture.log", "missing", str(log_path)))
    else:
        sources.append(LogSource("vulture.log", "unavailable", str(log_path)))

    if log_dir.is_dir() and file_status == "missing":
        newest: Path | None = None
        newest_ts = -1.0
        try:
            for candidate in log_dir.glob("*.log"):
                if not candidate.is_file():
                    continue
                try:
                    ts = candidate.stat().st_mtime
                except OSError:
                    continue
                if ts > newest_ts:
                    newest_ts = ts
                    newest = candidate
        except OSError:
            newest = None

        if newest is not None:
            alt_lines, alt_status = _read_tail_lines(newest)
            if alt_status == "ok" and alt_lines is not None:
                sources.append(
                    LogSource(
                        newest.name,
                        "ok",
                        f"{len(alt_lines)} tail line(s)",
                    )
                )
                all_lines.extend(alt_lines)

    for unit in (VULTURE_BOT_SYSTEMD_UNIT, VULTURE_SCHEDULER_SYSTEMD_UNIT):
        journal_lines, journal_source = _journal_source(unit)
        sources.append(journal_source)
        all_lines.extend(journal_lines)

    warning_count, error_count, recent_issues = _collect_issue_lines(all_lines)
    last_cycle_line = _find_last_matching(all_lines, _CYCLE_PATTERNS)
    last_bot_startup_line = _find_last_matching(all_lines, _BOT_STARTUP_PATTERNS)
    overall = _logs_overall(
        warning_count=warning_count,
        error_count=error_count,
        sources=sources,
    )

    return LogsSummary(
        sources=sources,
        warning_count=warning_count,
        error_count=error_count,
        recent_issues=recent_issues,
        last_cycle_line=last_cycle_line,
        last_bot_startup_line=last_bot_startup_line,
        overall=overall,
    )


def format_log_source_line(source: LogSource) -> str:
    label = {
        "ok": "OK",
        "missing": "Missing",
        "unavailable": "Unavailable",
    }[source.status]
    if source.detail:
        return f"{source.name}: {label} — {source.detail}"
    return f"{source.name}: {label}"


def logs_level(summary: LogsSummary) -> StatusLevel:
    return summary.overall
