"""Tests for Kestrel Tuya dual-meter power probe."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kestrel.tuya_power import (
    CHANNEL_MAPPING,
    KNOWN_METER_DEVICE_IDS,
    METER_1_KEY,
    METER_2_KEY,
    TuyaPowerApiError,
    TuyaPowerConfigError,
    WIZARD_DEFAULT_PROTOCOL_VERSION,
    build_appliance_index,
    build_tuya_power_snapshot,
    format_debug_dps_summary,
    format_raw_dps_lines,
    index_tinytuya_devices_by_id,
    load_tinytuya_devices,
    load_tuya_power_config,
    parse_dual_meter_dps,
    parse_tinytuya_devices_payload,
    redact_tuya_message,
    sanitize_tuya_payload,
    scan_local_devices,
)
from kestrel.tuya_power_error import (
    build_tuya_error_record,
    classify_tuya_error,
    tuya_error_path_for,
)
from kestrel.tuya_power_history import (
    TuyaPowerHistoryRecord,
    append_history_from_snapshot,
    build_history_record,
    read_history,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "tuya_dual_meter_dps.json"
DEVICES_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "tinytuya_devices.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _clear_tuya_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TUYA_DEVICES_JSON",
        "TUYA_METER1_DEVICE_ID",
        "TUYA_METER1_IP",
        "TUYA_METER1_LOCAL_KEY",
        "TUYA_METER2_DEVICE_ID",
        "TUYA_METER2_IP",
        "TUYA_METER2_LOCAL_KEY",
        "TUYA_LOCAL_KEY",
        "TUYA_DEVICE_VERSION",
        "TUYA_STATUS_PATH",
        "TUYA_HISTORY_PATH",
        "TUYA_CLOUD_API_KEY",
        "TUYA_CLOUD_API_SECRET",
        "TUYA_CLOUD_REGION",
    ):
        monkeypatch.delenv(name, raising=False)


class TestParseDualMeterDps:
    def test_parses_meter_1_channels(self) -> None:
        parsed = parse_dual_meter_dps(_load_fixture(), meter_key=METER_1_KEY, source="local")

        ch1 = parsed["channels"]["channel_1"]
        ch2 = parsed["channels"]["channel_2"]
        assert ch1["key"] == "ac_compressor"
        assert ch1["label"] == "AC compressor"
        assert ch1["power_w"] == pytest.approx(245.0)
        assert ch1["current_a"] == pytest.approx(10.2)
        assert ch1["energy_forward_kwh"] == pytest.approx(154.32)

        assert ch2["key"] == "furnace_air_handler"
        assert ch2["power_w"] == pytest.approx(12.0)
        assert ch2["current_a"] == pytest.approx(0.5)

        assert parsed["voltage_v"] == pytest.approx(240.5)
        assert parsed["total_power_w"] == pytest.approx(257.0)

    def test_channel_mapping_covers_all_appliances(self) -> None:
        keys = set()
        for meter in CHANNEL_MAPPING.values():
            for appliance_key, _label in meter.values():
                keys.add(appliance_key)
        assert keys == {"ac_compressor", "furnace_air_handler", "dryer", "dishwasher"}


class TestBuildSnapshot:
    def test_build_snapshot_shape(self) -> None:
        meter_1 = parse_dual_meter_dps(_load_fixture(), meter_key=METER_1_KEY, source="local")
        meter_2 = parse_dual_meter_dps(_load_fixture(), meter_key=METER_2_KEY, source="local")
        snapshot = build_tuya_power_snapshot(
            {METER_1_KEY: meter_1, METER_2_KEY: meter_2},
            updated_at="2026-06-27T12:00:00+00:00",
            source="local",
            limited=False,
        )

        assert snapshot["updated_at"] == "2026-06-27T12:00:00+00:00"
        assert snapshot["device_model"] == "V-WIFI-DL02-ES"
        assert snapshot["source"] == "local"
        assert snapshot["limited"] is False
        assert snapshot["stale"] is False
        assert set(snapshot["appliances"]) == {
            "ac_compressor",
            "furnace_air_handler",
            "dryer",
            "dishwasher",
        }

    def test_appliance_index_flattens_meters(self) -> None:
        meter_1 = parse_dual_meter_dps(_load_fixture(), meter_key=METER_1_KEY, source="local")
        appliances = build_appliance_index({METER_1_KEY: meter_1})
        assert appliances["ac_compressor"]["meter"] == METER_1_KEY
        assert appliances["ac_compressor"]["power_w"] == pytest.approx(245.0)


class TestDebugFormatting:
    def test_format_raw_dps_lines_sorted(self) -> None:
        lines = format_raw_dps_lines(
            meter_key=METER_1_KEY,
            raw_dps=_load_fixture(),
            source="local",
        )
        assert len(lines) == 8
        assert all("meter=meter_1" in line for line in lines)
        assert all("local_key" not in line for line in lines)

    def test_format_debug_dps_summary(self) -> None:
        meter_1 = parse_dual_meter_dps(_load_fixture(), meter_key=METER_1_KEY, source="local")
        snapshot = build_tuya_power_snapshot({METER_1_KEY: meter_1}, source="local")
        lines = format_debug_dps_summary(snapshot)
        assert len(lines) == 2
        combined = "\n".join(lines)
        assert "appliance=ac_compressor" in combined
        assert "power_w=245.0" in combined


class TestRedactTuyaMessage:
    def test_redacts_local_key_assignment(self) -> None:
        raw = "connect failed local_key=abcd1234secret5678"
        redacted = redact_tuya_message(raw)
        assert "abcd1234secret5678" not in redacted
        assert "local_key=[REDACTED]" in redacted

    def test_redacts_device_id_assignment(self) -> None:
        raw = "missing device_id=bf1234567890abcdef"
        redacted = redact_tuya_message(raw)
        assert "bf1234567890abcdef" not in redacted


class TestTinytuyaDevicesFile:
    def test_parse_flat_list_payload(self) -> None:
        payload = json.loads(DEVICES_FIXTURE_PATH.read_text(encoding="utf-8"))
        devices = parse_tinytuya_devices_payload(payload)
        assert len(devices) == 2
        assert devices[0]["id"] == KNOWN_METER_DEVICE_IDS[METER_1_KEY]

    def test_parse_wrapped_dict_payload(self) -> None:
        wrapped = {"devices": json.loads(DEVICES_FIXTURE_PATH.read_text(encoding="utf-8"))}
        devices = parse_tinytuya_devices_payload(wrapped)
        assert len(devices) == 2

    def test_load_tinytuya_devices_from_fixture(self) -> None:
        devices = load_tinytuya_devices(DEVICES_FIXTURE_PATH)
        indexed = index_tinytuya_devices_by_id(devices)
        assert set(indexed) == set(KNOWN_METER_DEVICE_IDS.values())


class TestLoadTuyaPowerConfig:
    def test_loads_meters_from_devices_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_tuya_env(monkeypatch)
        config = load_tuya_power_config(devices_json_path=DEVICES_FIXTURE_PATH)

        assert len(config.meters) == 2
        meter_1 = next(item for item in config.meters if item.meter_key == METER_1_KEY)
        meter_2 = next(item for item in config.meters if item.meter_key == METER_2_KEY)

        assert meter_1.device_id == KNOWN_METER_DEVICE_IDS[METER_1_KEY]
        assert meter_1.address == "192.168.1.101"
        assert meter_1.local_key == "wizardkey12345678"
        assert meter_1.version == pytest.approx(3.5)

        assert meter_2.device_id == KNOWN_METER_DEVICE_IDS[METER_2_KEY]
        assert meter_2.address == "192.168.1.102"
        assert meter_2.local_key == "wizardkey87654321"
        assert meter_2.version == pytest.approx(WIZARD_DEFAULT_PROTOCOL_VERSION)

    def test_env_overrides_devices_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_tuya_env(monkeypatch)
        monkeypatch.setenv("TUYA_METER1_IP", "10.0.0.99")
        monkeypatch.setenv("TUYA_METER1_LOCAL_KEY", "overridekey123456")
        monkeypatch.setenv("TUYA_DEVICE_VERSION", "3.4")

        config = load_tuya_power_config(devices_json_path=DEVICES_FIXTURE_PATH)
        meter_1 = next(item for item in config.meters if item.meter_key == METER_1_KEY)

        assert meter_1.address == "10.0.0.99"
        assert meter_1.local_key == "overridekey123456"
        assert meter_1.version == pytest.approx(3.4)

        meter_2 = next(item for item in config.meters if item.meter_key == METER_2_KEY)
        assert meter_2.address == "192.168.1.102"
        assert meter_2.local_key == "wizardkey87654321"
        assert meter_2.version == pytest.approx(3.4)

    def test_missing_devices_json_reports_devices_json_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_tuya_env(monkeypatch)

        with pytest.raises(TuyaPowerConfigError, match="devices.json"):
            load_tuya_power_config(devices_json_path=Path("/nonexistent/devices.json"))


class TestSanitizeTuyaPayload:
    def test_strips_local_keys_from_nested_status(self) -> None:
        payload = {
            "dps": {"101": 100},
            "key": "wizardkey12345678",
            "local_key": "wizardkey12345678",
            "nested": {"secret": "cloud-secret-value"},
        }
        sanitized = sanitize_tuya_payload(payload)
        rendered = json.dumps(sanitized)

        assert "wizardkey12345678" not in rendered
        assert "cloud-secret-value" not in rendered
        assert sanitized["key"] == "[REDACTED]"
        assert sanitized["nested"]["secret"] == "[REDACTED]"
        assert sanitized["dps"]["101"] == 100


class TestErrorHelpers:
    def test_classify_local_error(self) -> None:
        assert classify_tuya_error("Local read failed: connection timeout") == "local"

    def test_error_path_adjacent_to_status(self) -> None:
        assert tuya_error_path_for("data/kestrel_tuya_power_status.json").name == (
            "kestrel_tuya_power_error.json"
        )

    def test_build_error_record_includes_last_success(self) -> None:
        record = build_tuya_error_record(
            "local read timeout",
            last_success="2026-06-27T10:00:00+00:00",
        )
        assert record["error_type"] == "local"
        assert record["last_success"] == "2026-06-27T10:00:00+00:00"
        assert "timeout" in record["message"]


class TestScanLocalDevices:
    def test_device_scan_uses_maxretry_kwarg(self) -> None:
        fake_devices = {"192.168.1.54": {"gwId": "abcd1234", "version": "3.4"}}
        mock_scan = MagicMock(return_value=fake_devices)
        fake_tinytuya = MagicMock(deviceScan=mock_scan)

        with patch.dict(sys.modules, {"tinytuya": fake_tinytuya}):
            result = scan_local_devices(maxretry=15)

        assert result == fake_devices
        mock_scan.assert_called_once_with(maxretry=15)
        call_kwargs = mock_scan.call_args.kwargs
        assert "max_retries" not in call_kwargs

    def test_device_scan_rejects_legacy_max_retries_kwarg(self) -> None:
        def _reject_max_retries(**kwargs: object) -> dict:
            if "max_retries" in kwargs:
                raise TypeError("deviceScan() got an unexpected keyword argument 'max_retries'")
            return {}

        fake_tinytuya = MagicMock(deviceScan=MagicMock(side_effect=_reject_max_retries))

        with patch.dict(sys.modules, {"tinytuya": fake_tinytuya}):
            scan_local_devices()

        fake_tinytuya.deviceScan.assert_called_once_with(maxretry=15)

    def test_device_scan_wraps_failures(self) -> None:
        fake_tinytuya = MagicMock(
            deviceScan=MagicMock(side_effect=RuntimeError("network unreachable"))
        )

        with patch.dict(sys.modules, {"tinytuya": fake_tinytuya}):
            with pytest.raises(TuyaPowerApiError, match="network unreachable"):
                scan_local_devices()


class TestHistory:
    def test_build_history_record_compact(self) -> None:
        meter_1 = parse_dual_meter_dps(_load_fixture(), meter_key=METER_1_KEY, source="local")
        snapshot = build_tuya_power_snapshot({METER_1_KEY: meter_1}, source="local")
        record = build_history_record(snapshot)
        assert record["source"] == "local"
        assert "power_w" in record["appliances"]["ac_compressor"]
        assert "label" not in record["appliances"]["ac_compressor"]

    def test_append_history_from_snapshot(self, tmp_path: Path) -> None:
        meter_1 = parse_dual_meter_dps(_load_fixture(), meter_key=METER_1_KEY, source="local")
        snapshot = build_tuya_power_snapshot(
            {METER_1_KEY: meter_1},
            updated_at="2026-06-27T12:00:00+00:00",
            source="local",
        )
        history_path = tmp_path / "history.jsonl"
        assert append_history_from_snapshot(snapshot, path=history_path) is True
        records = read_history(history_path)
        assert len(records) == 1
        assert isinstance(records[0], TuyaPowerHistoryRecord)
        assert records[0].appliances["ac_compressor"]["power_w"] == pytest.approx(245.0)
