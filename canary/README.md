# Canary v0.2 — read-only Raven monitoring

**Canary** is an Aviary monitoring service — lightweight, **read-only** Raven health checks in the Vulture monorepo. Platform context: [docs/current/AVIARY_PROJECT_CONTEXT.md](../docs/current/AVIARY_PROJECT_CONTEXT.md). It runs periodic infrastructure checks every **5 minutes**, writes a machine-readable status file, and logs each run. It does **not** perform control or admin actions.

**Pelican backup monitoring** (checksum, staleness, backup timer health) runs separately on a **six-hour** schedule via `pelican-monitor.timer`. Canary only **reads** the latest `data/backup_monitor_status.json` snapshot — it does not re-run backup checks every 5 minutes. See [docs/current/PELiCAN_BACKUP.md](../docs/current/PELiCAN_BACKUP.md#pelican-backup-monitoring).

## What Canary does

- Runs infrastructure health checks every **5 minutes** by default (`CANARY_INTERVAL_SECONDS`, default `300`).
- Writes the latest snapshot to `data/canary_status.json`.
- Appends run logs to `logs/canary.log`.
- Evaluates overall status as `ok`, `warning`, or `critical`.
- Emits a top-level `alerts` array structured for Discord notifications and dashboard cards.
- Surfaces the last Pelican backup monitor snapshot at `checks.backup_monitor` (read-only, no checksum re-verification).

### Checks (read-only)

| Section | Coverage |
|---------|----------|
| Internet | ping `1.1.1.1`, optional DNS via `google.com` (timeouts enforced) |
| Network | LAN IP (`ip -br addr`), Tailscale IPv4 |
| Services | `ssh`, `tailscaled`, `smbd`, `docker`, `vulture-bot`, `vulture-scheduler.timer`, dashboard container |
| Storage | Raven mounts under `/mnt/storage/*` plus `/` — **UUID and mount path only** (never `/dev/sdX`) |
| Docker | daemon state, container counts, name/status/ports |
| Vulture runtime | process scan, optional `tmux ls`, latest log mtime |
| systemd failed | `systemctl --failed` count + unit names |
| **backup_monitor** | Reads `data/backup_monitor_status.json` written by `pelican-monitor.service` |

All subprocess calls use bounded timeouts. Stale or hung mounts surface as `STALE_MOUNT` or `DF_TIMEOUT` instead of crashing Canary.

### Raven storage volume statuses

Each volume reports one of:

| Status | Meaning |
|--------|---------|
| `OK` | Device present, mounted, reachable, `df` succeeded |
| `MISSING_DEVICE` | Configured UUID not seen in `blkid`/`lsblk` |
| `NOT_MOUNTED` | Device present but expected mount path is not active |
| `AUTOMOUNT_INACTIVE` | Automount unit expected but inactive while unmounted |
| `STALE_MOUNT` | Mount listed but path access hangs or fails |
| `DF_TIMEOUT` | `df` timed out (often stale NFS/USB mount) |
| `ERROR` | Unexpected read/check failure |

## What Canary does **not** do

- No mount, unmount, repair, wipe, format, or restart actions
- No backup checksum/staleness checks on the 5-minute loop (Pelican monitor handles that)
- No Discord alerts from Canary directly (Pelican monitor sends backup alerts via shared `canary/alerting.py`)
- No changes to Vulture bot, scheduler, or dashboard runtime
- No secrets or `.env` mounts
- No web UI or exposed HTTP port

## Run with Docker

```bash
docker compose -f docker-compose.canary.yml up -d --build
```

Compose uses `network_mode: host`, `pid: host`, read-only host root at `/host`, D-Bus, and docker.sock. Mount `./data` so Canary can read `backup_monitor_status.json` written by the host-side Pelican monitor.

## View output

```bash
cat data/canary_status.json
python3 -m json.tool data/canary_status.json | jq '.checks.backup_monitor'
cat data/backup_monitor_status.json
tail -f logs/canary.log
```

## Shared alerting helper

`canary/alerting.py` provides Discord delivery and persisted dedup (`data/canary_alert_state.json`) for **Pelican monitor** state-change alerts. Canary itself does not invoke this on its 5-minute loop.

## Local development

```bash
python3 -m compileall -q canary pelican_monitor
python3 -m pytest tests/test_canary.py tests/test_pelican_monitor.py -v
```

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `CANARY_INTERVAL_SECONDS` | `300` | Seconds between infrastructure check runs |
| `CANARY_HOST_ROOT` | `/` | Host prefix when using `/host` mount in Docker |
| `CANARY_BACKUP_MONITOR_STATUS_PATH` | `data/backup_monitor_status.json` | Snapshot read by `checks.backup_monitor` |
| `CANARY_BACKUP_MONITOR_SNAPSHOT_STALE_HOURS` | `8` | Warn when monitor snapshot is older than this |
| `CANARY_FSTAB_PATH` | `$HOST_ROOT/etc/fstab` | Read UUIDs and automount flags |
| `CANARY_STORAGE_VOLUMES` | built-in paths | JSON override for volume specs |
| `CANARY_TIMEZONE` | `America/Chicago` | Timestamp timezone in JSON |

Pelican monitor configuration is documented in [PELiCAN_BACKUP.md](../docs/current/PELiCAN_BACKUP.md#pelican-backup-monitoring).
