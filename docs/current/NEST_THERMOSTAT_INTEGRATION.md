# Nest thermostat integration (Google SDM)

Read-only Kestrel probe that polls [Google Smart Device Management (SDM)](https://developers.google.com/nest/device-access) for Nest thermostat status and writes a normalized JSON snapshot for the Nest dashboard and operators.

Thermostat **control commands are not implemented** in this probe.

## Environment variables

Add these to repo-root `.env` on Raven (never commit values):

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `NEST_SDM_PROJECT_ID` | yes | — | Google Device Access project id (enterprise id) |
| `NEST_GOOGLE_CLIENT_ID` | yes | — | OAuth client id |
| `NEST_GOOGLE_CLIENT_SECRET` | yes | — | OAuth client secret |
| `NEST_GOOGLE_REFRESH_TOKEN` | yes | — | Long-lived refresh token with SDM scope |
| `NEST_STATUS_PATH` | optional | `data/kestrel_nest_status.json` | Output snapshot path |

Example project id (non-secret):

```bash
NEST_SDM_PROJECT_ID=616e2a03-0969-424c-b5ac-1a8ba461e0be
```

Obtain OAuth credentials and a refresh token through the Google Cloud / Device Access console. Store all secrets only in `.env` on Raven.

## Manual poll

```bash
cd /home/vinnieb58/projects/vulture
source .venv/bin/activate
python experiments/kestrel/nest_probe.py --once
```

On success the probe writes `data/kestrel_nest_status.json` and prints the thermostat display names found (for example `Downstairs`, `Upstairs`).

On failure the probe exits non-zero, logs a redacted error, and **does not overwrite** the last good snapshot file.

## Scheduled systemd polling (every 5 minutes)

Isolated units (separate from Vulture scheduler and Smart Meter Texas timer):

| Unit | Purpose |
|------|---------|
| `kestrel-nest-poll.service` | Oneshot read-only poll (`nest_probe.py --once`) |
| `kestrel-nest-poll.timer` | Triggers every 5 minutes after boot |

Reference files: `deploy/systemd/kestrel-nest-poll.service`, `deploy/systemd/kestrel-nest-poll.timer`.

### Install

```bash
cd /home/vinnieb58/projects/vulture
chmod +x scripts/install_kestrel_nest_timer.sh

# Copy units + daemon-reload
./scripts/install_kestrel_nest_timer.sh

# Copy units, daemon-reload, enable timer
./scripts/install_kestrel_nest_timer.sh --enable
```

Or manually:

```bash
sudo cp deploy/systemd/kestrel-nest-poll.service /etc/systemd/system/
sudo cp deploy/systemd/kestrel-nest-poll.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kestrel-nest-poll.timer
systemctl list-timers --all | grep kestrel-nest
```

Requires `NEST_*` OAuth variables in `/home/vinnieb58/projects/vulture/.env`. The service unit loads that file and runs from the repo root. **No thermostat control commands** are invoked.

### Manual one-shot test

```bash
sudo systemctl start kestrel-nest-poll.service
journalctl -u kestrel-nest-poll.service -n 100 --no-pager
python3 experiments/kestrel/nest_probe.py --once
```

### Timer status

```bash
systemctl status kestrel-nest-poll.timer
systemctl list-timers --all | grep kestrel-nest
```

Successful runs log thermostat count and snapshot path. Failures log redacted OAuth/SDM errors only — never tokens, client secrets, refresh tokens, or Authorization headers.

### Disable / rollback

```bash
sudo systemctl disable --now kestrel-nest-poll.timer
sudo rm -f /etc/systemd/system/kestrel-nest-poll.service /etc/systemd/system/kestrel-nest-poll.timer
sudo systemctl daemon-reload
```

## Output shape

The snapshot uses lowercase snake_case keys per room:

```json
{
  "updated_at": "2026-06-19T12:00:00+00:00",
  "thermostats": {
    "downstairs": {
      "name": "Downstairs",
      "device_name": "enterprises/.../devices/...",
      "temperature": 73,
      "humidity": 65,
      "mode": "COOL",
      "action": "COOLING",
      "setpoint": 71,
      "online": true,
      "raw_mode": "COOL",
      "eco_mode": "OFF",
      "cool_setpoint": 71,
      "heat_setpoint": null
    }
  }
}
```

### Parsing rules

1. **Celsius is the API source of truth.** `Settings.temperatureScale` is ignored. All `*Celsius` traits convert with `(c * 9/5) + 32` and dashboard-facing values round to the nearest whole °F.
2. **Eco mode changes setpoint source.** When `ThermostatEco.mode` is `MANUAL_ECO`, effective `mode` is `MANUAL_ECO` and setpoints come from `ThermostatEco.coolCelsius` / `heatCelsius`. Otherwise effective `mode` comes from `ThermostatMode.mode` and setpoints come from `ThermostatTemperatureSetpoint`.
3. **Room names.** Prefer `parentRelations[0].displayName`, then `Info.customName`, then the final device id segment. Snapshot keys normalize to lowercase snake_case (`Downstairs` → `downstairs`).
4. **Read-only.** No SDM command traits are called.

## Security

- Never log `Authorization` headers, access tokens, refresh tokens, client secrets, or raw OAuth responses.
- Error and log helpers redact `ya29.*` Google access tokens and secret-like `key=value` patterns.
- Do not paste live bearer tokens into code, docs, or git.
- Keep `.env` and `data/kestrel_nest_status.json` out of version control if they contain household-specific identifiers you consider sensitive.

## Tests

```bash
python -m compileall kestrel experiments/kestrel tests/test_kestrel_nest_probe.py
pytest tests/test_kestrel_nest_probe.py -q
```

Fixtures live at `tests/fixtures/nest_sdm_two_thermostats.json` (Downstairs COOL + Upstairs MANUAL_ECO).

## Related files

| Path | Purpose |
|------|---------|
| `kestrel/nest.py` | SDM OAuth, fetch, parse, snapshot builder |
| `experiments/kestrel/nest_probe.py` | CLI entry point (`--once`) |
| `data/kestrel_nest_status.json` | Latest poll output (generated) |
| `deploy/systemd/kestrel-nest-poll.service` | systemd oneshot poll unit |
| `deploy/systemd/kestrel-nest-poll.timer` | 5-minute poll timer |
| `scripts/install_kestrel_nest_timer.sh` | Copy units and optional enable |
