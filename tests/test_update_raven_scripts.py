"""Static validation for Raven deploy scripts and Finch service restarts."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_RAVEN = REPO_ROOT / "scripts" / "update_raven.sh"
UPDATE_RAVEN_QUICK = REPO_ROOT / "scripts" / "update_raven_quick.sh"
REBUILD_DOCKER = REPO_ROOT / "scripts" / "rebuild_docker.sh"
GIT_STATE = REPO_ROOT / "scripts" / "raven_git_state.sh"
FINCH_HELPER = REPO_ROOT / "scripts" / "raven_finch_services.sh"
PREUPDATE_BACKUP = REPO_ROOT / "scripts" / "raven_preupdate_backup.sh"
PREUPDATE_BACKUP_PY = REPO_ROOT / "scripts" / "raven_preupdate_backup.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_finch_helper_exists() -> None:
    assert FINCH_HELPER.is_file()


def test_finch_helper_restarts_api_before_telegram() -> None:
    text = _read(FINCH_HELPER)
    api_idx = text.index("Restarting Finch API")
    telegram_idx = text.index("Restarting Finch Telegram")
    assert api_idx < telegram_idx


def test_finch_helper_validates_active_state() -> None:
    text = _read(FINCH_HELPER)
    assert "Finch API active" in text
    assert "Finch Telegram active" in text
    assert "systemctl is-active" in text


def test_finch_helper_warns_when_units_missing() -> None:
    text = _read(FINCH_HELPER)
    assert "not installed; skipping Finch API restart" in text
    assert "not installed; skipping Finch Telegram restart" in text


def test_update_raven_quick_sources_finch_helper() -> None:
    text = _read(UPDATE_RAVEN_QUICK)
    assert "raven_finch_services.sh" in text
    assert "restart_finch_services" in text


def test_update_raven_quick_restarts_finch_after_vulture_services() -> None:
    text = _read(UPDATE_RAVEN_QUICK)
    vulture_restart_idx = text.index("Restarted: ${VULTURE_SCHEDULER_TIMER}")
    finch_restart_idx = text.index("restart_finch_services")
    assert vulture_restart_idx < finch_restart_idx


def test_update_raven_quick_no_services_skips_finch_restarts() -> None:
    text = _read(UPDATE_RAVEN_QUICK)
    skip_block_start = text.index("if [[ $SKIP_SERVICES -eq 0 ]]; then")
    skip_block_end = text.index("else", skip_block_start)
    skip_block = text[skip_block_start:skip_block_end]
    assert "restart_systemd_services" in skip_block
    restart_fn_start = text.index("restart_systemd_services() {")
    restart_fn_end = text.index("\n}\n", restart_fn_start)
    restart_fn = text[restart_fn_start:restart_fn_end]
    assert "restart_finch_services" in restart_fn


def test_update_raven_sources_finch_helper() -> None:
    text = _read(UPDATE_RAVEN)
    assert "raven_finch_services.sh" in text
    assert "restart_finch_services" in text


def test_update_raven_installs_finch_units() -> None:
    text = _read(UPDATE_RAVEN)
    assert "finch-api.service" in text
    assert "finch-telegram.service" in text


def test_update_raven_restarts_finch_after_vulture_services() -> None:
    text = _read(UPDATE_RAVEN)
    vulture_restart_idx = text.index('echo "  Restarted: $VULTURE_SCHEDULER_TIMER"')
    finch_restart_idx = text.index("restart_finch_services")
    assert vulture_restart_idx < finch_restart_idx


def test_update_raven_skip_systemd_restart_skips_finch() -> None:
    text = _read(UPDATE_RAVEN)
    skip_block = text[text.index('if [[ "${SKIP_SYSTEMD_RESTART:-0}" == "1" ]]; then'):]
    skip_block = skip_block[: skip_block.index("else")]
    assert "restart_finch_services" not in skip_block
    assert "restart_systemd_services" not in skip_block


def test_preupdate_backup_helper_exists() -> None:
    assert PREUPDATE_BACKUP.is_file()
    assert PREUPDATE_BACKUP_PY.is_file()


def test_preupdate_backup_targets_pelican_subdirectory() -> None:
    text = _read(PREUPDATE_BACKUP_PY)
    assert "raven-preupdate" in text
    assert "/mnt/storage/pelican_backup" in text


def test_preupdate_backup_excludes_metrics_history() -> None:
    text = _read(PREUPDATE_BACKUP_PY)
    assert "raven_metrics_history.jsonl" in text


def test_update_raven_runs_preupdate_backup_before_git_fetch() -> None:
    text = _read(UPDATE_RAVEN)
    backup_idx = text.index("run_raven_preupdate_backup")
    fetch_idx = text.index('section "Fetching origin"')
    assert backup_idx < fetch_idx


def test_update_raven_quick_runs_preupdate_backup_before_git_fetch() -> None:
    text = _read(UPDATE_RAVEN_QUICK)
    backup_idx = text.index("run_raven_preupdate_backup")
    fetch_idx = text.index('section "Fetching origin"')
    assert backup_idx < fetch_idx


def test_update_raven_supports_no_preupdate_backup_flag() -> None:
    text = _read(UPDATE_RAVEN)
    assert "--no-preupdate-backup" in text
    assert "SKIP_PREUPDATE_BACKUP" in text


def test_update_raven_quick_supports_no_preupdate_backup_flag() -> None:
    text = _read(UPDATE_RAVEN_QUICK)
    assert "--no-preupdate-backup" in text
    assert "SKIP_PREUPDATE_BACKUP" in text


def test_preupdate_backup_does_not_print_secret_values() -> None:
    text = _read(PREUPDATE_BACKUP)
    assert "cat .env" not in text
    assert "Files included" in text


def test_raven_git_state_helper_exists() -> None:
    assert GIT_STATE.is_file()


def test_rebuild_docker_sources_git_state_helper() -> None:
    text = _read(REBUILD_DOCKER)
    assert "raven_git_state.sh" in text
    assert "print_raven_git_state" in text


def test_rebuild_docker_prints_git_state_before_deploy() -> None:
    text = _read(REBUILD_DOCKER)
    assert 'section "Git state (deploy)"' in text
    git_section_idx = text.index('section "Git state (deploy)"')
    docker_check_idx = text.index('if ! command -v docker', git_section_idx)
    assert git_section_idx < docker_check_idx


def test_update_raven_quick_sources_git_state_helper() -> None:
    text = _read(UPDATE_RAVEN_QUICK)
    assert "raven_git_state.sh" in text
    assert "print_raven_git_state" in text


def test_update_raven_quick_prints_dashboard_logs_on_health_failure() -> None:
    text = _read(UPDATE_RAVEN_QUICK)
    assert "print_dashboard_logs_on_failure" in text
    assert "Dashboard health check failed" in text


def test_rebuild_docker_prints_dashboard_logs_on_health_failure() -> None:
    text = _read(REBUILD_DOCKER)
    assert "print_dashboard_logs_on_failure" in text
    assert "dashboard health check failed" in text
