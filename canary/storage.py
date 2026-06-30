"""
Raven storage checks — UUID and mount-path based, read-only.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from canary import config
from canary.config import StorageVolumeSpec
from canary.parsers import (
    derive_automount_unit,
    parse_df_output,
    parse_findmnt_target,
    parse_fstab_entries,
    parse_lsblk_uuid_map,
    storage_use_level,
    storage_volume_to_overall,
)
from canary.path_util import path_access_check
from canary.subprocess_util import is_timeout, run_command


def host_path(path: str) -> str:
    if config.HOST_ROOT == Path("/"):
        return path
    if path == "/":
        return str(config.HOST_ROOT)
    return str(config.HOST_ROOT / path.lstrip("/"))


def _read_fstab() -> dict[str, dict[str, Any]]:
    fstab_file = config.FSTAB_PATH
    try:
        if fstab_file.is_file():
            return parse_fstab_entries(fstab_file.read_text(encoding="utf-8"))
    except OSError:
        pass
    return {}


def _enrich_spec_from_fstab(spec: StorageVolumeSpec, fstab: dict[str, dict[str, Any]]) -> StorageVolumeSpec:
    entry = fstab.get(spec.mount_path)
    if not entry:
        return spec
    return replace(
        spec,
        uuid=spec.uuid or entry.get("uuid"),
        fstype=spec.fstype or entry.get("fstype"),
        automount_expected=spec.automount_expected or bool(entry.get("automount_expected")),
        automount_unit=spec.automount_unit or derive_automount_unit(spec.mount_path),
    )


def _probe_uuid_present(uuid: str) -> tuple[bool, str | None]:
    ok, out = run_command(["blkid", "-U", uuid], timeout=config.TIMEOUT_BLKID)
    if ok:
        return True, None
    if is_timeout(out):
        return False, "blkid timed out"
    lowered = out.lower()
    if "not found" in lowered or "couldn't find" in lowered:
        return False, None
    return False, out


def _findmnt_at_path(resolved_path: str) -> tuple[bool, dict[str, str | None], str | None]:
    ok, out = run_command(
        ["findmnt", "--mountpoint", resolved_path, "-n", "-o", "TARGET,SOURCE,FSTYPE"],
        timeout=config.TIMEOUT_FINDMNT,
    )
    if not ok:
        if is_timeout(out):
            return False, {}, "findmnt timed out"
        return False, {}, out or "not mounted"
    parsed = parse_findmnt_target(out)
    if not parsed:
        return False, {}, "findmnt returned no data"
    return True, parsed, None


def _trigger_automount(resolved_path: str) -> tuple[bool, str | None]:
    return path_access_check(resolved_path, timeout=config.TIMEOUT_PATH)


def _automount_state(unit: str) -> tuple[str, str | None]:
    ok, out = run_command(["systemctl", "is-active", unit], timeout=config.TIMEOUT_SYSTEMCTL)
    state = out.strip().splitlines()[0] if out.strip() else "unknown"
    if ok and state == "active":
        return "active", None
    if is_timeout(out):
        return "unknown", "systemctl timed out"
    return state or "inactive", out if not ok else None


def _df_for_path(resolved_path: str) -> tuple[str, dict[str, Any] | None, str | None]:
    ok, out = run_command(["df", "-P", "-B1", resolved_path], timeout=config.TIMEOUT_DF)
    if is_timeout(out):
        return "DF_TIMEOUT", None, out
    if not ok:
        return "ERROR", None, out
    parsed = parse_df_output(out)
    entry = parsed.get(resolved_path)
    if entry is None and parsed:
        entry = next(iter(parsed.values()))
    if entry is None:
        return "ERROR", None, "df returned no data"
    return "OK", entry, None


def _build_volume_result(
    spec: StorageVolumeSpec,
    *,
    status: str,
    mounted: bool,
    usage: dict[str, Any] | None = None,
    detail: str | None = None,
    lsblk: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "label": spec.label,
        "mount_path": spec.mount_path,
        "uuid": spec.uuid,
        "required": spec.required,
        "fstype": (lsblk or {}).get("fstype") or spec.fstype,
        "label_tag": (lsblk or {}).get("label") or spec.label_tag,
        "mounted": mounted,
        "status": status,
        "size": usage.get("size") if usage else None,
        "used": usage.get("used") if usage else None,
        "available": usage.get("available") if usage else None,
        "use_percent": usage.get("use_percent") if usage else None,
    }
    use_level = storage_use_level(result["use_percent"]) if mounted and status == "OK" else None
    if use_level:
        result["use_level"] = use_level
    if detail:
        result["detail"] = detail
    if spec.automount_unit:
        result["automount_unit"] = spec.automount_unit
    return result


def evaluate_storage_volume(
    spec: StorageVolumeSpec,
    *,
    lsblk_by_uuid: dict[str, dict[str, str | None]],
) -> dict[str, Any]:
    resolved = host_path(spec.mount_path)

    if not spec.uuid:
        return _build_volume_result(
            spec,
            status="ERROR",
            mounted=False,
            detail="no UUID configured or found in fstab",
        )

    uuid_key = spec.uuid.lower()
    lsblk_info = lsblk_by_uuid.get(uuid_key)

    device_present, blkid_err = _probe_uuid_present(spec.uuid)
    if blkid_err and "timed out" in blkid_err:
        return _build_volume_result(
            spec,
            status="ERROR",
            mounted=False,
            detail=blkid_err,
            lsblk=lsblk_info,
        )
    if not device_present and lsblk_info is None:
        return _build_volume_result(
            spec,
            status="MISSING_DEVICE",
            mounted=False,
            detail="UUID not present in blkid/lsblk",
            lsblk=lsblk_info,
        )

    _trigger_automount(resolved)

    mounted, findmnt_info, findmnt_err = _findmnt_at_path(resolved)
    if findmnt_err and "timed out" in (findmnt_err or ""):
        return _build_volume_result(
            spec,
            status="ERROR",
            mounted=False,
            detail=findmnt_err,
            lsblk=lsblk_info,
        )

    if not mounted:
        if spec.automount_expected and spec.automount_unit:
            automount_state, automount_err = _automount_state(spec.automount_unit)
            if automount_state not in ("active", "activating"):
                return _build_volume_result(
                    spec,
                    status="AUTOMOUNT_INACTIVE",
                    mounted=False,
                    detail=automount_err or f"automount {automount_state}",
                    lsblk=lsblk_info,
                )
        return _build_volume_result(
            spec,
            status="NOT_MOUNTED",
            mounted=False,
            detail=findmnt_err or "device present but path not mounted",
            lsblk=lsblk_info,
        )

    path_ok, path_err = path_access_check(resolved, timeout=config.TIMEOUT_PATH)
    if not path_ok:
        if path_err and "timed out" in path_err:
            return _build_volume_result(
                spec,
                status="STALE_MOUNT",
                mounted=True,
                detail=path_err,
                lsblk=lsblk_info,
            )
        return _build_volume_result(
            spec,
            status="STALE_MOUNT",
            mounted=True,
            detail=path_err or "mount point unreachable",
            lsblk=lsblk_info,
        )

    df_status, usage, df_err = _df_for_path(resolved)
    if df_status == "DF_TIMEOUT":
        return _build_volume_result(
            spec,
            status="DF_TIMEOUT",
            mounted=True,
            detail=df_err,
            lsblk=lsblk_info,
        )
    if df_status == "ERROR":
        return _build_volume_result(
            spec,
            status="ERROR",
            mounted=True,
            detail=df_err,
            lsblk=lsblk_info,
        )

    if findmnt_info.get("fstype") and not spec.fstype:
        spec = replace(spec, fstype=findmnt_info.get("fstype"))

    return _build_volume_result(
        spec,
        status="OK",
        mounted=True,
        usage=usage,
        lsblk=lsblk_info,
    )


def evaluate_root_volume() -> dict[str, Any]:
    label, mount_path = config.ROOT_MOUNT
    spec = StorageVolumeSpec(label=label, mount_path=mount_path)
    resolved = host_path(mount_path)

    mounted, findmnt_info, findmnt_err = _findmnt_at_path(resolved)
    if not mounted:
        return _build_volume_result(
            spec,
            status="NOT_MOUNTED",
            mounted=False,
            detail=findmnt_err or "root not mounted",
        )

    path_ok, path_err = path_access_check(resolved, timeout=config.TIMEOUT_PATH)
    if not path_ok:
        return _build_volume_result(
            spec,
            status="STALE_MOUNT",
            mounted=True,
            detail=path_err,
        )

    df_status, usage, df_err = _df_for_path(resolved)
    if df_status == "DF_TIMEOUT":
        return _build_volume_result(spec, status="DF_TIMEOUT", mounted=True, detail=df_err)
    if df_status == "ERROR":
        return _build_volume_result(spec, status="ERROR", mounted=True, detail=df_err)

    spec = replace(spec, fstype=findmnt_info.get("fstype"))
    return _build_volume_result(spec, status="OK", mounted=True, usage=usage)


def check_raven_storage() -> dict[str, Any]:
    fstab = _read_fstab()
    specs = [_enrich_spec_from_fstab(spec, fstab) for spec in config.STORAGE_VOLUME_SPECS]

    ok_lsblk, lsblk_out = run_command(
        ["lsblk", "-o", "UUID,FSTYPE,LABEL,SIZE", "-n", "-P"],
        timeout=config.TIMEOUT_LSBLK,
    )
    lsblk_by_uuid = parse_lsblk_uuid_map(lsblk_out) if ok_lsblk else {}

    volumes: list[dict[str, Any]] = [evaluate_root_volume()]
    for spec in specs:
        volumes.append(evaluate_storage_volume(spec, lsblk_by_uuid=lsblk_by_uuid))

    section_statuses: list[str] = []
    for vol in volumes:
        vol_status = vol["status"]
        required = bool(vol.get("required", True))
        overall = storage_volume_to_overall(vol_status, required=required)
        use_level = vol.get("use_level")
        if use_level == "critical":
            overall = "critical"
        elif use_level == "warning" and overall == "ok":
            overall = "warning"
        section_statuses.append(overall)

    alerts = storage_alerts_from_volumes(volumes)

    return {
        "status": combine_storage_section_status(section_statuses),
        "volumes": volumes,
        "alerts": alerts,
    }


def combine_storage_section_status(levels: list[str]) -> str:
    if any(level == "critical" for level in levels):
        return "critical"
    if any(level == "warning" for level in levels):
        return "warning"
    return "ok"


def storage_alerts_from_volumes(volumes: list[dict[str, Any]]) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    for vol in volumes:
        status = vol.get("status", "ERROR")
        if status == "OK" and not vol.get("use_level"):
            continue
        severity = "warning"
        if status in ("STALE_MOUNT", "DF_TIMEOUT", "ERROR") or vol.get("use_level") == "critical":
            severity = "critical"
        elif status == "OK" and vol.get("use_level") == "warning":
            severity = "warning"
        message = _alert_message(vol)
        alerts.append(
            {
                "severity": severity,
                "category": "storage",
                "code": status if status != "OK" else str(vol.get("use_level", "usage")).upper(),
                "volume": vol["label"],
                "mount_path": vol["mount_path"],
                "message": message,
            }
        )
    return alerts


def _alert_message(vol: dict[str, Any]) -> str:
    status = vol.get("status", "ERROR")
    label = vol.get("label", "unknown")
    path = vol.get("mount_path", "")
    if status == "OK" and vol.get("use_percent") is not None:
        return f"{label} ({path}): {vol['use_percent']:.0f}% used"
    if status == "MISSING_DEVICE":
        uuid = vol.get("uuid") or "unknown"
        return f"{label}: device UUID {uuid} not detected"
    if status == "NOT_MOUNTED":
        return f"{label} ({path}): present but not mounted"
    if status == "AUTOMOUNT_INACTIVE":
        return f"{label} ({path}): automount inactive"
    if status == "STALE_MOUNT":
        return f"{label} ({path}): stale or unreachable mount"
    if status == "DF_TIMEOUT":
        return f"{label} ({path}): df timed out"
    detail = vol.get("detail") or status
    return f"{label} ({path}): {detail}"
