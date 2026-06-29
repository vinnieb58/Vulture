# Kestrel Tuya appliance power monitoring

Read-only Kestrel investigation for **V-WIFI-DL02-ES** dual-channel WiFi energy monitors (Tuya v3.4 / PJ1103A class). The experimental probe polls local TinyTuya status first, optionally falls back to Tuya Cloud, and writes normalized JSON for future Nest/Kestrel dashboard correlation.

**Observe-only:** no device control commands, dashboard UI, or alerts in this phase. Production polling uses a read-only systemd timer (60 seconds); no dashboard wiring yet.

## Device model

| Field | Value |
|-------|-------|
| Model | `V-WIFI-DL02-ES` |
| Protocol | Tuya local LAN v3.5 (V-WIFI-DL02-ES observed layout; differs from PJ1103A docs) |
| Channels per unit | 2 (A + B) |
| Expected DPS (observed) | Channel A block `105`–`109`; channel B block `115`–`119` |

Two physical meters are installed:

| Meter slot | Channel | CT load | Snapshot appliance key |
|------------|---------|---------|----------------------|
| Meter 1 | Channel 1 (A) | AC compressor | `ac_compressor` |
| Meter 1 | Channel 2 (B) | Furnace / air handler | `furnace_air_handler` |
| Meter 2 | Channel 1 (A) | Dryer | `dryer` |
| Meter 2 | Channel 2 (B) | Dishwasher | `dishwasher` |

## Recommended integration path

1. **Discovery (LAN scan + raw DPS)** — run the probe in discover mode on Raven while both meters are powered and on the same subnet. Confirm DPS ids/scales match the **V-WIFI-DL02-ES** layout below (not the generic PJ1103A TinyTuya table).
2. **Local TinyTuya reads (preferred)** — configure device id, LAN IP, and local key per meter. Poll with `--once` every manual test cycle. No cloud dependency; lowest latency; aligns with existing Kestrel read-only probes.
3. **Tuya Cloud fallback (optional)** — only if local LAN reads are blocked (VLAN isolation, key rotation without local re-pairing, etc.). Set cloud API credentials; the probe tries local first per meter, then cloud for failed meters.
4. **Snapshot + JSONL history** — on successful poll, write latest status JSON and append compact appliance history (Nest probe pattern). Failures preserve the last good snapshot and write a sidecar error JSON.
5. **Dashboard / Nest correlation (future)** — read snapshot + history from disk; show live appliance watts beside Nest HVAC runtime and Smart Meter Texas whole-home kWh. No UI in this phase.

## Environment variables

Configuration loads from repo-root **`devices.json`** (TinyTuya wizard output) by default. **`.env` values override** matching fields when present.

### TinyTuya wizard (recommended first step)

From the repo root on Raven:

```bash
cd /home/vinnieb58/projects/vulture
source .venv/bin/activate
pip install tinytuya
python -m tinytuya wizard
```

The wizard writes local files (all git-ignored):

| File | Purpose |
|------|---------|
| `devices.json` | Device id, local key, IP, protocol version — **primary probe config source** |
| `tinytuya.json` | Wizard/cloud session metadata |
| `snapshot.json` | Last wizard poll snapshot |
| `tuya-raw.json` | Raw Tuya Cloud payload |

Known household meters (matched automatically when absent from `.env`):

| Meter slot | Device id |
|------------|-----------|
| Meter 1 | `eb1d19e2b571760833his3` |
| Meter 2 | `eb1441d488053f92efin1n` |

Field mapping from `devices.json`:

| TinyTuya field | Probe field | Default |
|----------------|-------------|---------|
| `id` | `device_id` | — |
| `key` | `local_key` | — |
| `ip` | LAN address | — |
| `version` | protocol version | `3.5` when absent |

After wizard completes, `--discover` and `--once` should work **without** duplicating keys into `.env`. Use `.env` only for overrides (for example a changed IP or global `TUYA_DEVICE_VERSION`).

Optional `.env` overrides (never commit values):

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `TUYA_DEVICES_JSON` | optional | `devices.json` | Alternate path to TinyTuya device file |
| `TUYA_METER1_DEVICE_ID` | optional | known meter 1 id | Overrides `devices.json` id lookup |
| `TUYA_METER1_IP` | optional | from `devices.json` | LAN IP override for meter 1 |
| `TUYA_METER1_LOCAL_KEY` | optional | from `devices.json` | Local key override for meter 1 |
| `TUYA_METER2_DEVICE_ID` | optional | known meter 2 id | Overrides `devices.json` id lookup |
| `TUYA_METER2_IP` | optional | from `devices.json` | LAN IP override for meter 2 |
| `TUYA_METER2_LOCAL_KEY` | optional | from `devices.json` | Local key override for meter 2 |
| `TUYA_LOCAL_KEY` | optional | — | Shared local key override for both meters |
| `TUYA_DEVICE_VERSION` | optional | per-device from file, else `3.5`/`3.4` | Global protocol version override |
| `TUYA_STATUS_PATH` | optional | `data/kestrel_tuya_power_status.json` | Latest snapshot path |
| `TUYA_HISTORY_PATH` | optional | `data/kestrel_tuya_power_history.jsonl` | Append-only history |
| `TUYA_CLOUD_API_KEY` | cloud fallback only | — | Tuya IoT platform API key |
| `TUYA_CLOUD_API_SECRET` | cloud fallback only | — | Tuya IoT platform API secret |
| `TUYA_CLOUD_REGION` | optional | `us` | Cloud region (`us`, `eu`, `cn`, …) |

Do not commit `devices.json`, `tinytuya.json`, `snapshot.json`, or `tuya-raw.json`. The probe never prints local keys or other secrets.

## Manual probe

```bash
cd /home/vinnieb58/projects/vulture
source .venv/bin/activate

# Discovery: LAN scan + raw DPS/status (no snapshot write)
python experiments/kestrel/tuya_power_probe.py --discover

# One-shot poll: normalized snapshot + history append
python experiments/kestrel/tuya_power_probe.py --once
python experiments/kestrel/tuya_power_probe.py --once --debug-dps

# Manual sampler (not scheduled): repeated polls with compact stdout lines
python experiments/kestrel/tuya_power_probe.py --sample
python experiments/kestrel/tuya_power_probe.py --sample --interval-seconds 60 --count 10
python experiments/kestrel/tuya_power_probe.py --sample --interval-seconds 30 --count 5 --debug-dps
```

`--discover` prints raw status/DPS output for operator mapping before normalization is trusted. `--once` writes the snapshot only on full success.

`--sample` runs the same read/write path as `--once` for each sample: updates the latest snapshot JSON, appends one JSONL history row per successful sample, and prints one compact appliance summary line per sample. On failure it preserves the last good snapshot and writes/updates the error sidecar, then exits non-zero (same as `--once`). This is for manual investigation only — no dashboard wiring yet.

## Production systemd polling (60 seconds)

| Unit | Role |
|------|------|
| `kestrel-tuya-power-poll.service` | Oneshot read-only poll (`tuya_power_probe.py --once`) |
| `kestrel-tuya-power-poll.timer` | Triggers every 60 seconds after boot |

Reference files: `deploy/systemd/kestrel-tuya-power-poll.service`, `deploy/systemd/kestrel-tuya-power-poll.timer`.

### Install

```bash
cd /home/vinnieb58/projects/vulture
chmod +x scripts/install_kestrel_tuya_power_timer.sh

# Copy units + daemon-reload (does not enable the timer)
./scripts/install_kestrel_tuya_power_timer.sh

# Copy units, daemon-reload, enable, and start the 60-second timer
./scripts/install_kestrel_tuya_power_timer.sh --enable
```

Manual install (same units):

```bash
sudo cp deploy/systemd/kestrel-tuya-power-poll.service /etc/systemd/system/
sudo cp deploy/systemd/kestrel-tuya-power-poll.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kestrel-tuya-power-poll.timer
systemctl list-timers --all | grep kestrel-tuya-power
```

Requires repo-root `devices.json` (TinyTuya wizard) and optional `.env` overrides. The service loads `.env` via `EnvironmentFile` but never prints secrets.

### Verify

```bash
sudo systemctl start kestrel-tuya-power-poll.service
journalctl -u kestrel-tuya-power-poll.service -n 100 --no-pager
python experiments/kestrel/tuya_power_probe.py --once
```

```bash
systemctl status kestrel-tuya-power-poll.timer
systemctl list-timers --all | grep kestrel-tuya-power
ls -la data/kestrel_tuya_power_status.json data/kestrel_tuya_power_history.jsonl
```

On poll failure the timer keeps running; the probe **does not overwrite** the last good snapshot and writes `data/kestrel_tuya_power_error.json` instead.

### Disable / rollback

```bash
sudo systemctl disable --now kestrel-tuya-power-poll.timer
sudo rm -f /etc/systemd/system/kestrel-tuya-power-poll.service /etc/systemd/system/kestrel-tuya-power-poll.timer
sudo systemctl daemon-reload
```

## Output shape (status JSON)

Latest snapshot at `data/kestrel_tuya_power_status.json`:

```json
{
  "updated_at": "2026-06-27T12:00:00+00:00",
  "device_model": "V-WIFI-DL02-ES",
  "source": "local",
  "limited": false,
  "stale": false,
  "meters": {
    "meter_1": {
      "meter_key": "meter_1",
      "online": true,
      "source": "local",
      "dps_profile": "v_wifi_dl02_es",
      "raw_dps": {
        "105": 18820,
        "106": 15759,
        "107": 1227,
        "108": 26494,
        "109": 15427,
        "115": 10273,
        "116": 10636,
        "117": 1228,
        "118": 15764,
        "119": 9516
      },
      "device_id_suffix": "his3",
      "channels": {
        "channel_1": {
          "label": "AC compressor",
          "key": "ac_compressor",
          "online": true,
          "source": "local",
          "voltage_v": 122.7,
          "power_w": 2649.4,
          "energy_forward_kwh_inferred": 154.27,
          "raw_dps": {
            "105": 18820,
            "106": 15759,
            "107": 1227,
            "108": 26494,
            "109": 15427
          },
          "raw_unknown": {
            "105": 18820,
            "106": 15759
          }
        },
        "channel_2": {
          "label": "Furnace / air handler",
          "key": "furnace_air_handler",
          "online": true,
          "source": "local",
          "voltage_v": 122.8,
          "power_w": 1576.4,
          "energy_forward_kwh_inferred": 95.16,
          "raw_dps": {
            "115": 10273,
            "116": 10636,
            "117": 1228,
            "118": 15764,
            "119": 9516
          },
          "raw_unknown": {
            "115": 10273,
            "116": 10636
          }
        }
      }
    },
    "meter_2": { }
  },
  "appliances": {
    "ac_compressor": {
      "label": "AC compressor",
      "meter": "meter_1",
      "online": true,
      "source": "local",
      "dps_profile": "v_wifi_dl02_es",
      "voltage_v": 122.7,
      "power_w": 2649.4,
      "energy_forward_kwh_inferred": 154.27
    }
  }
}
```

### DPS mapping (V-WIFI-DL02-ES, observed on Raven)

Each channel exposes a 5-DPS block. Suffix pattern repeats per channel:

| Channel | DPS block | Role (confidence) | Scale | Example |
|---------|-----------|-------------------|-------|---------|
| A (CT1) | `105`–`109` | see rows below | — | meter_1 live poll |
| B (CT2) | `115`–`119` | see rows below | — | meter_1 live poll |

Per-channel suffix (`x` = `0` for channel A, `1` for channel B):

| Suffix | DPS (ch A / ch B) | Normalized field | Scale | Confidence |
|--------|-------------------|------------------|-------|------------|
| x05 | `105` / `115` | `raw_unknown` only | — | **Unresolved** (not voltage/current) |
| x06 | `106` / `116` | `raw_unknown` only | — | **Unresolved** |
| x07 | `107` / `117` | `voltage_v` | ÷10 → volts | **High** (~122.7 V) |
| x08 | `108` / `118` | `power_w` | ÷10 → watts | **Medium** (tracks load; validate vs known draws) |
| x09 | `109` / `119` | `energy_forward_kwh_inferred` | ÷100 → kWh | **Low** (treat as cumulative energy hypothesis) |

The probe **preserves full `raw_dps`** at the meter level and per channel. Uncertain DPS are **not** mapped to `current_a` or meter-level voltage.

Legacy **PJ1103A** devices (DPS `101`/`112` present) still use the TinyTuya WiFiDualMeter map via `dps_profile: "pj1103a"`.

### Parsing rules

1. **Profile detection.** If DPS `107`/`117` appear, use `v_wifi_dl02_es` layout; if `101`/`112` appear, use `pj1103a`.
2. **Conservative normalization.** Only emit scaled fields listed above with sufficient confidence; never invent `current_a` from ambiguous DPS.
3. **Channel mapping is fixed** to the CT install table above; snapshot keys are stable for dashboard use.
4. **`source`** is `local`, `cloud`, or `mixed` when meters use different transports.
5. **`limited`** is `true` when fewer than two meters are configured or returned.
6. **`stale`** is reserved for dashboard-side age checks (snapshot timestamp older than poll interval); the probe sets it to `false` on fresh successful polls.
7. **Read-only.** No Tuya `set_*` / control commands are invoked.

## History JSONL

Append-only file at `data/kestrel_tuya_power_history.jsonl` (14-day retention, same window as Nest history):

```json
{"timestamp":"2026-06-27T12:00:00+00:00","source":"local","limited":false,"appliances":{"ac_compressor":{"voltage_v":122.7,"power_w":2649.4,"energy_forward_kwh_inferred":154.27,"online":true,"source":"local"},"furnace_air_handler":{"voltage_v":122.8,"power_w":1576.4,"energy_forward_kwh_inferred":95.16,"online":true,"source":"local"}}}
```

Each line is one successful poll. Fields are compact (no labels) to keep files small.

## Failure and stale behavior

| Scenario | Snapshot file | History | Error sidecar |
|----------|---------------|---------|---------------|
| Poll success | Overwritten | Appended | Cleared (`kestrel_tuya_power_error.json` removed) |
| Poll failure | **Preserved** (last good) | Not appended | Written with redacted message + `last_success` |
| Partial meter config | `limited: true` on success | Appended | — |
| Cloud fallback used | `source: cloud` or `mixed` | Appended | — |

Error sidecar path: `data/kestrel_tuya_power_error.json`

```json
{
  "timestamp": "2026-06-27T12:05:00+00:00",
  "error_type": "local",
  "message": "Local read failed for meter_2: connection timeout",
  "last_success": "2026-06-27T12:00:00+00:00"
}
```

Dashboard readers should treat snapshots older than ~2× the intended poll interval (120 seconds with the 60-second timer) as **stale** and surface the error sidecar when present. Never log or display local keys, cloud secrets, or full device ids.

## Dashboard / Nest display plan (future, not implemented)

Planned read-only presentation on the Kestrel/Nest dashboard:

| UI area | Data source | Display |
|---------|-------------|---------|
| Appliance power strip | `appliances.*.power_w` | Live watts per mapped load (AC, furnace, dryer, dishwasher) |
| HVAC correlation | Nest `action` + Tuya `ac_compressor` / `furnace_air_handler` | Overlay compressor/furnace draw when Nest reports COOLING/HEATING |
| Whole-home context | SMT kWh + Tuya sum | Compare circuit-level sum to meter interval (informational) |
| Health badge | `kestrel_tuya_power_error.json`, snapshot age | “Stale” / “Limited” / last success timestamp |
| History sparkline | JSONL `power_w` series | 24h appliance load shapes (read from history file) |

No dashboard routes, templates, or alerts are added in this investigation phase.

## Security

- Never log or print `local_key`, `devices.json` `key` values, cloud secrets, or raw Tuya tokens.
- Error and log helpers redact `local_key=`, `device_id=`, and token-like assignments.
- Discover mode prints device id **suffix only** (last 4 chars) for scan results; `sanitize_tuya_payload()` strips secrets from raw status output.
- Do not commit `.env`, `devices.json`, `tinytuya.json`, `snapshot.json`, `tuya-raw.json`, or generated `data/kestrel_tuya_power_*.json*`.

## Tests

```bash
python -m compileall -q kestrel experiments/kestrel tests/test_kestrel_tuya_power_probe.py tests/test_kestrel_tuya_power_systemd.py
pytest tests/test_kestrel_tuya_power_probe.py tests/test_kestrel_tuya_power_systemd.py -q
```

Fixtures live at `tests/fixtures/tuya_vwifi_meter1_observed.json` and `tests/fixtures/tuya_vwifi_meter2_observed.json` (exact Raven DPS payloads). PJ1103A regression fixture: `tests/fixtures/tuya_dual_meter_dps.json`.

## Related files

| Path | Purpose |
|------|---------|
| `kestrel/tuya_power.py` | Config, local/cloud read, DPS parse, snapshot builder |
| `kestrel/tuya_power_error.py` | Poll error sidecar (preserve last good snapshot) |
| `kestrel/tuya_power_history.py` | Append-only JSONL history |
| `experiments/kestrel/tuya_power_probe.py` | CLI (`--discover`, `--once`, `--debug-dps`, `--sample`) |
| `deploy/systemd/kestrel-tuya-power-poll.service` | systemd oneshot poll unit |
| `deploy/systemd/kestrel-tuya-power-poll.timer` | 60-second poll timer |
| `scripts/install_kestrel_tuya_power_timer.sh` | Copy units and optional enable |
| `data/kestrel_tuya_power_status.json` | Latest poll output (generated) |
| `data/kestrel_tuya_power_history.jsonl` | Poll history (generated) |
| `data/kestrel_tuya_power_error.json` | Last poll error (generated) |

## Related integrations

- Nest thermostats: `docs/current/NEST_THERMOSTAT_INTEGRATION.md`
- Smart Meter Texas: `docs/current/KESTREL_OPERATIONS.md`
