"""Static validation for Raven deploy scripts and Finch service restarts."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_RAVEN = REPO_ROOT / "scripts" / "update_raven.sh"
UPDATE_RAVEN_QUICK = REPO_ROOT / "scripts" / "update_raven_quick.sh"
FINCH_HELPER = REPO_ROOT / "scripts" / "raven_finch_services.sh"


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
