"""Tests for Simply Fresh probe auth storage path resolution."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "simplyfresh_probe"))

from probe_common import PROBE_DIR, resolve_auth_storage_path


def test_resolve_auth_storage_path_default_is_absolute_and_probe_relative():
    path = resolve_auth_storage_path()
    assert path.is_absolute()
    expected = (PROBE_DIR / ".auth" / "simplyfresh_storage_state.json").resolve()
    assert path == expected


def test_resolve_auth_storage_path_env_override(tmp_path, monkeypatch):
    custom = tmp_path / "custom_storage_state.json"
    custom.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("SIMPLYFRESH_STORAGE_STATE_PATH", str(custom))
    assert resolve_auth_storage_path() == custom.resolve()
