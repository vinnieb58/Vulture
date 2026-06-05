# Canary v0.1

Canary is a lightweight, **read-only** Raven monitoring service for the Vulture repo. It runs periodic health checks, writes a machine-readable status file, and logs each run. It does **not** perform control or admin actions.

## What Canary v0.1 does

- Runs health checks every **5 minutes** by default (`CANARY_INTERVAL_SECONDS`, default `300`).
- Writes the latest snapshot to `data/canary_status.json`.
- Appends run logs to `logs/canary.log`.
- Evaluates overall status as `ok`, `warning`, or `critical`.
- Checks (read-only):
  - **Internet** — ping `1.1.1.1`, optional DNS via `google.com`
  - **Network** — LAN IP (`ip -br addr`), Tailscale IPv4
  - **Services** — `ssh`/`ssh.socket`, `tailscaled`, `smbd`, `docker`, optional `vulture-bot` / `vulture-scheduler`
  - **Storage** — `/`, `/mnt/storage/microsd`, `/mnt/storage/portable_beast`, `/mnt/storage/toshiba_ext`
  - **Docker** — daemon state, container counts, name/status/ports list
  - **Vulture runtime** — process scan, optional `tmux ls`, latest log mtime
  - **Failed systemd units** — `systemctl --failed`

If a command fails or is unavailable, Canary records a degraded/warning result and keeps running.

## What Canary v0.1 does **not** do

- No Discord alerting
- No service restarts or systemd control
- No changes to Vulture bot, scheduler, or dashboard runtime
- No secrets or `.env` mounts
- No web UI or exposed HTTP port (v0.1)

## Run with Docker

From the Vulture repo root on Raven:

```bash
docker compose -f docker-compose.canary.yml up -d --build
```

The compose file uses `network_mode: host`, `pid: host`, and a read-only host root mount at `/host` so Canary can inspect Raven services, storage, and Tailscale without modifying the host.

Optional interval override:

```bash
CANARY_INTERVAL_SECONDS=120 docker compose -f docker-compose.canary.yml up -d --build
```

## View output

```bash
cat data/canary_status.json
tail -f logs/canary.log
```

Example shape:

```json
{
  "generated_at": "2026-06-05T22:30:00-05:00",
  "host": "raven",
  "overall_status": "warning",
  "checks": {
    "internet": {},
    "network": {},
    "services": {},
    "storage": {},
    "docker": {},
    "vulture_runtime": {},
    "systemd_failed": {}
  },
  "warnings": [],
  "critical": []
}
```

## Dashboard consumption (future)

The Vulture dashboard (or Nest) can read `data/canary_status.json` as a stable, read-only contract:

- `overall_status` drives top-level health color/state
- `checks.*` sections map to cards (network, storage, services, docker, etc.)
- `warnings` / `critical` arrays provide human-readable rollup messages
- `generated_at` supports staleness detection if the file stops updating

No API server is required for v0.1 — mount or copy the JSON file read-only.

## Local development

```bash
python3 -m compileall -q canary
python3 -m pytest tests/test_canary.py -v
```

Run one-shot on the host (outside Docker):

```bash
CANARY_INTERVAL_SECONDS=5 python -m canary.app
```

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `CANARY_INTERVAL_SECONDS` | `300` | Seconds between check runs |
| `CANARY_DATA_DIR` | `./data` | Status output directory |
| `CANARY_LOGS_DIR` | `./logs` | Log directory |
| `CANARY_STATUS_PATH` | `data/canary_status.json` | Status file path |
| `CANARY_HOST_ROOT` | `/` | Host path prefix when using `/host` mount in Docker |
| `CANARY_TIMEZONE` | `America/Chicago` | Timestamp timezone in JSON |
| `CANARY_EXPECTED_MOUNTS` | built-in list | Override as `label:/path,...` |
