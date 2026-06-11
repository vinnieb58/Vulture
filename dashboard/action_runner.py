"""Action Center — whitelisted operational actions for the Nest dashboard.

Actions are registered in ACTION_REGISTRY. Dashboard routes invoke action IDs;
no arbitrary shell execution is exposed.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from host_actions import (
    CANARY_CONTAINER,
    HEALTHCHECK_SCRIPT,
    UPDATE_SCRIPT,
    run_allowlisted_systemctl,
    run_docker_exec,
    run_host_script,
    wait_for_unit_inactive,
)
from host_commands import systemctl_is_active
from vulture_runtime import _list_timer_next_run

ActionHandler = Callable[["ActionRun"], None]

AUDIT_PATH = Path(
    os.environ.get("DASHBOARD_ACTION_AUDIT_PATH", "/app/data/action_center_audit.jsonl")
)
MAX_OUTPUT_LINES = int(os.environ.get("DASHBOARD_ACTION_MAX_OUTPUT_LINES", "2000"))

_lock = threading.Lock()
_runs: dict[str, "ActionRun"] = {}
_last_by_action: dict[str, str] = {}
_audit_entries: list[dict[str, Any]] = []


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass
class ActionDefinition:
    action_id: str
    name: str
    description: str
    handler: ActionHandler
    confirm_message: str = ""


@dataclass
class ActionRun:
    run_id: str
    action_id: str
    status: str = "idle"  # idle | running | success | failed
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    exit_code: int | None = None
    user_ip: str = ""
    output_lines: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def append_output(self, line: str) -> None:
        with _lock:
            self.output_lines.append(line)
            if len(self.output_lines) > MAX_OUTPUT_LINES:
                self.output_lines = self.output_lines[-MAX_OUTPUT_LINES:]

    def to_dict(self, *, include_output: bool = True) -> dict[str, Any]:
        with _lock:
            data: dict[str, Any] = {
                "run_id": self.run_id,
                "action_id": self.action_id,
                "status": self.status,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "duration_seconds": self.duration_seconds,
                "exit_code": self.exit_code,
                "result": dict(self.result),
                "error": self.error,
                "output_line_count": len(self.output_lines),
            }
            if include_output:
                data["output"] = list(self.output_lines)
            return data


def _append_audit(entry: dict[str, Any]) -> None:
    with _lock:
        _audit_entries.append(entry)
        if len(_audit_entries) > 500:
            _audit_entries[:] = _audit_entries[-500:]

    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass


def _finish_run(run: ActionRun, *, exit_code: int, success: bool) -> None:
    run.finished_at = _utc_now()
    run.exit_code = exit_code
    run.status = "success" if success else "failed"
    if run.started_at:
        try:
            started = datetime.strptime(run.started_at, "%Y-%m-%d %H:%M:%S UTC").replace(
                tzinfo=timezone.utc
            )
            finished = datetime.strptime(run.finished_at, "%Y-%m-%d %H:%M:%S UTC").replace(
                tzinfo=timezone.utc
            )
            run.duration_seconds = round((finished - started).total_seconds(), 2)
        except ValueError:
            run.duration_seconds = None

    _append_audit(
        {
            "timestamp": run.finished_at,
            "action": run.action_id,
            "run_id": run.run_id,
            "user_ip": run.user_ip,
            "result": run.status,
            "exit_code": exit_code,
            "duration_seconds": run.duration_seconds,
            "summary": run.result.get("summary"),
        }
    )


def _run_handler(run: ActionRun, handler: ActionHandler) -> None:
    run.status = "running"
    run.started_at = _utc_now()
    try:
        handler(run)
        if run.status == "running":
            code = run.exit_code if run.exit_code is not None else 0
            _finish_run(run, exit_code=code, success=code == 0)
    except Exception as exc:  # noqa: BLE001 — action must fail safely
        run.error = str(exc)
        run.append_output(f"ERROR: {exc}")
        _finish_run(run, exit_code=1, success=False)


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


def _handle_update_raven(run: ActionRun) -> None:
    run.append_output(f"Started: {run.started_at}")
    run.append_output(f"Running: {UPDATE_SCRIPT}")

    def on_line(line: str) -> None:
        run.append_output(line)

    rc, _ = run_host_script(UPDATE_SCRIPT, on_line=on_line)
    run.exit_code = rc
    run.result = {
        "summary": "Update completed" if rc == 0 else f"Update failed (exit {rc})",
        "exit_code": rc,
    }
    _finish_run(run, exit_code=rc, success=rc == 0)


def _handle_hunt_cycle(run: ActionRun) -> None:
    unit = "vulture-scheduler.service"
    run.append_output(f"Starting hunt cycle via systemctl start {unit}")

    rc, out = run_allowlisted_systemctl("start", unit, on_line=run.append_output)
    if out:
        for line in out.splitlines():
            if line not in run.output_lines:
                run.append_output(line)

    if rc != 0:
        run.result = {"summary": f"Failed to start scheduler (exit {rc})", "exit_code": rc}
        _finish_run(run, exit_code=rc, success=False)
        return

    run.append_output("Waiting for hunt cycle to complete…")
    completed, final_state = wait_for_unit_inactive(unit)
    end_time = _utc_now()
    hunts_processed = _count_hunts_in_output("\n".join(run.output_lines))

    run.result = {
        "summary": "Hunt cycle completed" if completed and final_state != "failed" else "Hunt cycle finished with issues",
        "start_time": run.started_at,
        "end_time": end_time,
        "hunts_processed": hunts_processed,
        "service_final_state": final_state,
        "exit_code": 0 if completed and final_state != "failed" else 1,
    }
    rc_final = 0 if completed and final_state != "failed" else 1
    run.exit_code = rc_final
    _finish_run(run, exit_code=rc_final, success=rc_final == 0)


def _count_hunts_in_output(text: str) -> int:
    return len(re.findall(r"Done hunt ", text, flags=re.IGNORECASE))


def _handle_restart_vulture_bot(run: ActionRun) -> None:
    unit = "vulture-bot.service"
    before_ok, before = systemctl_is_active(unit)
    run.result["status_before"] = before if before_ok else "unknown"
    run.append_output(f"Service status before: {run.result['status_before']}")

    rc, out = run_allowlisted_systemctl("restart", unit, on_line=run.append_output)
    if out:
        for line in out.splitlines():
            if line not in run.output_lines:
                run.append_output(line)

    after_ok, after = systemctl_is_active(unit)
    run.result["status_after"] = after if after_ok else "unknown"
    run.append_output(f"Service status after: {run.result['status_after']}")

    success = rc == 0
    run.result["summary"] = "Restart succeeded" if success else f"Restart failed (exit {rc})"
    run.exit_code = rc
    _finish_run(run, exit_code=rc, success=success)


def _handle_restart_scheduler_timer(run: ActionRun) -> None:
    unit = "vulture-scheduler.timer"
    before_ok, before = systemctl_is_active(unit)
    run.result["status_before"] = before if before_ok else "unknown"
    run.append_output(f"Timer status before: {run.result['status_before']}")

    rc, out = run_allowlisted_systemctl("restart", unit, on_line=run.append_output)
    if out:
        for line in out.splitlines():
            if line not in run.output_lines:
                run.append_output(line)

    after_ok, after = systemctl_is_active(unit)
    run.result["status_after"] = after if after_ok else "unknown"
    run.append_output(f"Timer status after: {run.result['status_after']}")

    next_run = _list_timer_next_run(unit)
    run.result["next_scheduled_execution"] = next_run
    if next_run:
        run.append_output(f"Next scheduled execution: {next_run}")

    success = rc == 0
    run.result["summary"] = "Timer restart succeeded" if success else f"Timer restart failed (exit {rc})"
    run.exit_code = rc
    _finish_run(run, exit_code=rc, success=success)


def _parse_healthcheck_output(text: str) -> dict[str, Any]:
    warnings: list[str] = []
    failures: list[str] = []
    summary = "Health check completed"

    for line in text.splitlines():
        lower = line.lower()
        if "warn" in lower and ("items" in lower or "—" in line or "-" in line):
            warnings.append(line.strip())
        if "fail" in lower and ("items" in lower or "—" in line or "-" in line):
            failures.append(line.strip())
        if line.strip().startswith("OVERALL:"):
            summary = line.strip()

    in_warn = False
    in_fail = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("WARNINGS") or stripped.startswith("Warnings"):
            in_warn, in_fail = True, False
            continue
        if stripped.startswith("FAILURES") or stripped.startswith("Failures"):
            in_warn, in_fail = False, True
            continue
        if stripped.startswith("===") or stripped.startswith("---"):
            in_warn = in_fail = False
            continue
        if in_warn and stripped.startswith("-"):
            warnings.append(stripped.lstrip("- ").strip())
        if in_fail and stripped.startswith("-"):
            failures.append(stripped.lstrip("- ").strip())

    return {
        "summary": summary,
        "warnings": warnings[:20],
        "failures": failures[:20],
        "timestamp": _utc_now(),
    }


def _handle_health_check(run: ActionRun) -> None:
    run.append_output(f"Running: {HEALTHCHECK_SCRIPT}")

    def on_line(line: str) -> None:
        run.append_output(line)

    rc, out = run_host_script(HEALTHCHECK_SCRIPT, on_line=on_line)
    if out:
        parsed = _parse_healthcheck_output(out)
        run.result.update(parsed)
    else:
        run.result = {"summary": "No output", "warnings": [], "failures": [], "timestamp": _utc_now()}

    run.exit_code = rc
    success = rc == 0 and not run.result.get("failures")
    if not success and rc == 0 and run.result.get("failures"):
        rc = 1
    run.result["exit_code"] = rc
    _finish_run(run, exit_code=rc, success=success)


def _handle_refresh_canary(run: ActionRun) -> None:
    run.append_output(f"Triggering Canary refresh via docker exec {CANARY_CONTAINER}")
    py_snippet = (
        "from canary.app import setup_logging, run_once; "
        "import json; "
        "payload = run_once(setup_logging()); "
        "print(json.dumps({'overall_status': payload.get('overall_status'), "
        "'warnings': len(payload.get('warnings', [])), "
        "'critical': len(payload.get('critical', []))}))"
    )
    rc, out = run_docker_exec(
        CANARY_CONTAINER,
        ["python", "-c", py_snippet],
        on_line=run.append_output,
    )
    refresh_time = _utc_now()
    summary = "Canary refresh completed"
    if rc != 0:
        summary = f"Canary refresh failed (exit {rc})"
    else:
        try:
            last_line = [ln for ln in out.splitlines() if ln.strip()][-1]
            payload = json.loads(last_line)
            summary = (
                f"overall={payload.get('overall_status')} "
                f"warnings={payload.get('warnings')} critical={payload.get('critical')}"
            )
            run.result["canary"] = payload
        except (json.JSONDecodeError, IndexError):
            run.result["canary"] = {"raw": out[-500:] if out else ""}

    run.result["summary"] = summary
    run.result["refresh_time"] = refresh_time
    run.exit_code = rc
    _finish_run(run, exit_code=rc, success=rc == 0)


# ---------------------------------------------------------------------------
# Registry — single location for approved actions
# ---------------------------------------------------------------------------

ACTION_REGISTRY: dict[str, ActionDefinition] = {
    "update_raven": ActionDefinition(
        action_id="update_raven",
        name="Update Raven",
        description="Run the existing deployment/update workflow (update_raven_quick.sh).",
        confirm_message="Pull latest code, sync dependencies, refresh systemd units, and rebuild Docker stacks?",
        handler=_handle_update_raven,
    ),
    "hunt_cycle": ActionDefinition(
        action_id="hunt_cycle",
        name="Run Hunt Cycle Now",
        description="Immediately execute one Vulture hunt cycle via the scheduler service.",
        confirm_message="Start one Vulture hunt cycle now?",
        handler=_handle_hunt_cycle,
    ),
    "restart_vulture_bot": ActionDefinition(
        action_id="restart_vulture_bot",
        name="Restart Vulture Bot",
        description="Restart the Discord bot service (vulture-bot.service).",
        confirm_message="Restart the Vulture Discord bot?",
        handler=_handle_restart_vulture_bot,
    ),
    "restart_scheduler_timer": ActionDefinition(
        action_id="restart_scheduler_timer",
        name="Restart Scheduler Timer",
        description="Restart the scheduler timer (vulture-scheduler.timer).",
        confirm_message="Restart the Vulture scheduler timer?",
        handler=_handle_restart_scheduler_timer,
    ),
    "health_check": ActionDefinition(
        action_id="health_check",
        name="Run Raven Health Check",
        description="Execute the existing raven_healthcheck.sh script.",
        confirm_message="Run a full Raven health check?",
        handler=_handle_health_check,
    ),
    "refresh_canary": ActionDefinition(
        action_id="refresh_canary",
        name="Refresh Canary Data",
        description="Force an immediate Canary health refresh.",
        confirm_message="Run a Canary health refresh now?",
        handler=_handle_refresh_canary,
    ),
}


def list_action_definitions() -> list[dict[str, Any]]:
    """Return public metadata for all registered actions."""
    items: list[dict[str, Any]] = []
    for action_id, definition in ACTION_REGISTRY.items():
        last_run = get_last_run_for_action(action_id)
        items.append(
            {
                "action_id": action_id,
                "name": definition.name,
                "description": definition.description,
                "confirm_message": definition.confirm_message,
                "last_run": last_run,
            }
        )
    return items


def get_action_definition(action_id: str) -> ActionDefinition | None:
    return ACTION_REGISTRY.get(action_id)


def get_run(run_id: str) -> ActionRun | None:
    with _lock:
        return _runs.get(run_id)


def get_last_run_for_action(action_id: str) -> dict[str, Any] | None:
    with _lock:
        run_id = _last_by_action.get(action_id)
        if not run_id:
            return None
        run = _runs.get(run_id)
        if not run:
            return None
        return {
            "run_id": run.run_id,
            "status": run.status,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "duration_seconds": run.duration_seconds,
            "result": dict(run.result),
            "exit_code": run.exit_code,
        }


def get_audit_entries(*, limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        return list(_audit_entries[-limit:])


def start_action(action_id: str, *, user_ip: str = "") -> ActionRun:
    """Start an allowlisted action asynchronously. Raises KeyError if unknown."""
    definition = ACTION_REGISTRY.get(action_id)
    if definition is None:
        raise KeyError(action_id)

    run = ActionRun(
        run_id=str(uuid.uuid4()),
        action_id=action_id,
        status="running",
        user_ip=user_ip,
    )
    with _lock:
        _runs[run.run_id] = run
        _last_by_action[action_id] = run.run_id

    thread = threading.Thread(
        target=_run_handler,
        args=(run, definition.handler),
        name=f"action-{action_id}-{run.run_id[:8]}",
        daemon=True,
    )
    thread.start()
    return run


def reset_state_for_tests() -> None:
    """Clear in-memory runs and audit (tests only)."""
    with _lock:
        _runs.clear()
        _last_by_action.clear()
        _audit_entries.clear()
