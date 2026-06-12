"""
tests/test_action_center.py

Action Center v1 — whitelisted actions, audit logging, async status API.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))

import action_runner  # noqa: E402
import app as dashboard_app  # noqa: E402
from action_runner import (  # noqa: E402
    ACTION_REGISTRY,
    ActionRun,
    get_audit_entries,
    get_last_run_for_action,
    reset_state_for_tests,
    start_action,
)
from host_actions import (  # noqa: E402
    ALLOWED_SYSTEMCTL_RESTART,
    ALLOWED_SYSTEMCTL_START,
    run_allowlisted_systemctl,
)


@pytest.fixture(autouse=True)
def _clean_action_state(tmp_path, monkeypatch):
    reset_state_for_tests()
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(action_runner, "AUDIT_PATH", audit_path)
    yield
    reset_state_for_tests()


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "missing.db"
    log_path = tmp_path / "missing.log"
    monkeypatch.setattr(dashboard_app, "DB_PATH", db_path)
    monkeypatch.setattr(dashboard_app, "LOG_PATH", log_path)
    monkeypatch.setattr("db_readers.DB_PATH", db_path)
    monkeypatch.setattr("log_readers.LOG_PATH", log_path)
    monkeypatch.setattr("vulture_runtime.LOG_PATH", log_path)
    return TestClient(dashboard_app.app)


class TestActionRegistry:
    def test_all_six_initial_actions_registered(self):
        expected = {
            "update_raven",
            "hunt_cycle",
            "restart_vulture_bot",
            "restart_scheduler_timer",
            "health_check",
            "refresh_canary",
        }
        assert set(ACTION_REGISTRY.keys()) == expected

    def test_systemctl_allowlists_are_restricted(self):
        assert "vulture-bot.service" in ALLOWED_SYSTEMCTL_RESTART
        assert "vulture-scheduler.timer" in ALLOWED_SYSTEMCTL_RESTART
        assert "vulture-scheduler.service" in ALLOWED_SYSTEMCTL_START
        assert "docker.service" not in ALLOWED_SYSTEMCTL_RESTART


class TestUnknownActionRejection:
    def test_post_unknown_action_returns_404(self, client):
        response = client.post(
            "/api/actions/not_a_real_action/run",
            json={"confirm": True},
        )
        assert response.status_code == 404
        assert "Unknown action" in response.json()["detail"]

    def test_get_unknown_action_returns_404(self, client):
        response = client.get("/api/actions/self_destruct")
        assert response.status_code == 404

    def test_run_without_confirm_returns_400(self, client):
        response = client.post("/api/actions/health_check/run", json={"confirm": False})
        assert response.status_code == 400


class TestAllowedActionExecution:
    def test_health_check_action_completes(self, client):
        def fake_script(path, extra_args=(), *, timeout=900, on_line=None):
            lines = ["OVERALL: OK", "WARNINGS", "- disk 85%"]
            for ln in lines:
                if on_line:
                    on_line(ln)
            return 0, "\n".join(lines)

        with patch("action_runner.run_host_script", side_effect=fake_script):
            response = client.post(
                "/api/actions/health_check/run",
                json={"confirm": True},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in ("running", "success", "failed")
        run_id = data["run_id"]

        for _ in range(30):
            poll = client.get(f"/api/actions/runs/{run_id}")
            assert poll.status_code == 200
            body = poll.json()
            if body["status"] != "running":
                break
            time.sleep(0.05)

        assert body["status"] == "success"
        assert body["exit_code"] == 0
        assert "summary" in body["result"]

    def test_restart_bot_records_service_status(self, client):
        states = iter(["active", "active"])

        def fake_is_active(unit, **kwargs):
            return True, next(states)

        def fake_restart(subcommand, unit, **kwargs):
            if on_line := kwargs.get("on_line"):
                on_line(f"restarted {unit}")
            return 0, f"restarted {unit}"

        with patch("action_runner.systemctl_is_active", side_effect=fake_is_active):
            with patch("action_runner.run_allowlisted_systemctl", side_effect=fake_restart):
                response = client.post(
                    "/api/actions/restart_vulture_bot/run",
                    json={"confirm": True},
                )
        run_id = response.json()["run_id"]

        for _ in range(30):
            poll = client.get(f"/api/actions/runs/{run_id}")
            body = poll.json()
            if body["status"] != "running":
                break
            time.sleep(0.05)

        assert body["status"] == "success"
        assert body["result"]["status_before"] == "active"
        assert body["result"]["status_after"] == "active"


class TestFailedCommandHandling:
    def test_failed_script_marks_run_failed(self, client):
        with patch(
            "action_runner.run_host_script",
            return_value=(1, "script error"),
        ):
            run = start_action("health_check", user_ip="127.0.0.1")

        for _ in range(30):
            if run.status != "running":
                break
            time.sleep(0.05)

        assert run.status == "failed"
        assert run.exit_code == 1

    def test_disallowed_systemctl_unit_rejected(self):
        rc, msg = run_allowlisted_systemctl("restart", "docker.service")
        assert rc == 126
        assert "not allowlisted" in msg

    def test_handler_exception_fails_safely(self):
        def boom(_run: ActionRun) -> None:
            raise RuntimeError("simulated failure")

        action_runner.ACTION_REGISTRY["test_boom"] = action_runner.ActionDefinition(
            action_id="test_boom",
            name="Test",
            description="test",
            handler=boom,
        )
        try:
            run = start_action("test_boom")
            for _ in range(30):
                if run.status != "running":
                    break
                time.sleep(0.05)
            assert run.status == "failed"
            assert run.error == "simulated failure"
        finally:
            action_runner.ACTION_REGISTRY.pop("test_boom", None)


class TestActionStatusUpdates:
    def test_last_run_updated_after_completion(self, client):
        with patch("action_runner.run_host_script", return_value=(0, "ok")):
            client.post("/api/actions/health_check/run", json={"confirm": True})

        for _ in range(40):
            last = get_last_run_for_action("health_check")
            if last and last["status"] != "running":
                break
            time.sleep(0.05)

        assert last is not None
        assert last["status"] == "success"
        assert last["finished_at"] is not None

    def test_output_streamed_during_run(self):
        lines_seen: list[str] = []

        def fake_script(path, extra_args=(), *, timeout=900, on_line=None):
            if on_line:
                on_line("line-1")
                on_line("line-2")
            return 0, "line-1\nline-2"

        with patch("action_runner.run_host_script", side_effect=fake_script):
            run = start_action("update_raven")
            for _ in range(40):
                if run.status != "running":
                    break
                time.sleep(0.05)

        assert "line-1" in run.output_lines
        assert "line-2" in run.output_lines

    def test_get_unknown_run_returns_404(self, client):
        response = client.get("/api/actions/runs/does-not-exist")
        assert response.status_code == 404


class TestAuditLog:
    def test_audit_entry_written_on_completion(self, client):
        with patch("action_runner.run_host_script", return_value=(0, "done")):
            response = client.post(
                "/api/actions/health_check/run",
                json={"confirm": True},
            )
        run_id = response.json()["run_id"]

        for _ in range(40):
            poll = client.get(f"/api/actions/runs/{run_id}")
            if poll.json()["status"] != "running":
                break
            time.sleep(0.05)

        entries = get_audit_entries()
        assert entries
        last = entries[-1]
        assert last["action"] == "health_check"
        assert last["run_id"] == run_id
        assert last["result"] in ("success", "failed")
        assert "duration_seconds" in last
        assert last["user_ip"] == "testclient"

    def test_audit_persisted_to_file(self, client, tmp_path):
        audit_file = tmp_path / "persist.jsonl"
        with patch.object(action_runner, "AUDIT_PATH", audit_file):
            with patch("action_runner.run_host_script", return_value=(0, "ok")):
                client.post("/api/actions/health_check/run", json={"confirm": True})

        for _ in range(40):
            if get_audit_entries():
                break
            time.sleep(0.05)

        assert audit_file.is_file()
        content = audit_file.read_text(encoding="utf-8")
        assert "health_check" in content


class TestActionCenterPage:
    def test_actions_page_returns_200(self, client):
        response = client.get("/actions")
        assert response.status_code == 200
        assert "Action Center" in response.text
        assert "Update Raven" in response.text
        assert "Run Hunt Cycle Now" in response.text
        assert "Refresh Canary Data" in response.text

    def test_actions_page_has_navigation(self, client):
        response = client.get("/actions")
        assert "/actions" in response.text
        assert "Confirm" in response.text

    def test_api_list_actions(self, client):
        response = client.get("/api/actions")
        assert response.status_code == 200
        actions = response.json()["actions"]
        assert len(actions) == 6
        ids = {a["action_id"] for a in actions}
        assert "update_raven" in ids
