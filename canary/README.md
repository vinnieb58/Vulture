# Canary v0.2 — read-only Raven monitoring

**Canary** is an Aviary monitoring service — lightweight, **read-only** Raven health checks in the Vulture monorepo. Platform context: [docs/current/AVIARY_PROJECT_CONTEXT.md](../docs/current/AVIARY_PROJECT_CONTEXT.md). It runs periodic health checks, writes a machine-readable status file, logs each run, and optionally sends **Pelican backup Discord alerts** on meaningful state changes. It does **not** perform control or admin actions.

## What Canary does

- Runs health checks every **5 minutes** by default (`CANARY_INTERVAL_SECONDS`, default `300`).
- Writes the latest snapshot to `data/canary_status.json`.
- Appends run logs to `logs/canary.log`.
- Evaluates overall status as `ok`, `warning`, or `critical`.
- Emits a top-level `alerts` array structured for Discord notifications and dashboard cards.
- Monitors **Pelican backup health** (timer, last service result, mount, archive freshness, checksum) and sends Discord alerts when configured.

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
| **Pelican backup** | `pelican-backup.timer` / `pelican-backup.service`, real mount at `/mnt/storage/pelican_backup`, latest `raven-recovery-*.tar.{zst,gz}` archive age + checksum |

All subprocess calls (`lsblk`, `blkid`, `findmnt`, `df`, `systemctl`, `docker`, `ping`) and mount-path probes use bounded timeouts. Stale or hung mounts surface as `STALE_MOUNT` or `DF_TIMEOUT` instead of crashing Canary.

### Pelican backup monitoring

Canary verifies that Raven is not silently assuming backups exist:

| Check | Healthy | Critical / warning |
|-------|---------|-------------------|
| Timer | enabled, active, future run scheduled | disabled, inactive, or no next run |
| Service | last run succeeded; inactive/dead between runs is OK | last run failed |
| Mount | real backing device (not autofs placeholder or root FS alias) | unavailable or placeholder |
| Archive | newest completed `raven-recovery-*` bundle readable, checksum valid | missing, unreadable, bad/missing checksum |
| Staleness | younger than stale threshold | missing archive or older than threshold; **warning** when approaching threshold |

Completed bundles match `raven-recovery-YYYYMMDDTHHMMSSZ.tar.zst` or `.tar.gz`. Incomplete (`.incomplete`, `.partial`) and unrelated files are ignored.

**Discord alerting (Pelican only):** when `CANARY_DISCORD_WEBHOOK_URL` (or `DISCORD_WEBHOOK_URL`) is set, Canary sends alerts on:

- healthy → unhealthy
- warning → critical
- unhealthy reason change (issue code fingerprint)
- unhealthy → recovered

Identical consecutive alerts are suppressed via `data/canary_alert_state.json`. Alert messages include host, severity, failure reason, timer state, last service result, and newest backup age — never `.env`, manifest contents, or secret values.

See [docs/current/PELiCAN_BACKUP.md](../docs/current/PELiCAN_BACKUP.md#canary-monitoring) for operator procedures (manual run, safe failure simulation, dedup/recovery verification).

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

Volume JSON includes `uuid`, `fstype`, `label_tag`, `mount_path`, `size`, `used`, `available`, `use_percent` when available.

Expected Raven storage paths (UUIDs read from `/etc/fstab` at runtime):

- `/mnt/storage/toshiba_ext`
- `/mnt/storage/pelican_backup`
- `/mnt/storage/roost_spinning_0`
- `/mnt/storage/raven_nvme`
- `/mnt/storage/microsd`

## What Canary does **not** do

- No mount, unmount, repair, wipe, format, or restart actions
- No Discord alerts for non-Pelican checks (storage/services JSON remains alert-ready for future consumers)
- No changes to Vulture bot, scheduler, or dashboard runtime
- No secrets or `.env` mounts
- No web UI or exposed HTTP port

### Blind spot: USB hardware blocks boot

Canary runs **after** Raven has booted. If faulty USB storage prevents the machine from reaching multi-user target (kernel hang, initramfs stall, or firmware blocking POST), Canary never starts and **cannot report that failure**. Treat absence of fresh `canary_status.json` combined with host unreachable as a separate on-site or out-of-band alert path (BIOS/IPMI/smart plug), not something Canary can detect from inside Raven.

## Run with Docker

From the Vulture repo root on Raven:

```bash
docker compose -f docker-compose.canary.yml up -d --build
```

Compose uses `network_mode: host`, `pid: host`, read-only host root at `/host`, D-Bus, and docker.sock so Canary can inspect systemd, storage, and containers without modifying the host.

To enable Pelican Discord alerts, set a webhook in the repo `.env` or export before compose up:

```bash
export CANARY_DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
docker compose -f docker-compose.canary.yml up -d --build
```

## View output

```bash
cat data/canary_status.json
cat data/canary_alert_state.json   # Pelican alert dedup state
tail -f logs/canary.log
```

Inspect Pelican section only:

```bash
python3 -m json.tool data/canary_status.json | jq '.checks.pelican_backup'
```

### Manual one-shot Pelican check

From repo root (host or inside Canary container with host mounts):

```bash
CANARY_HOST_ROOT=/host python3 -m canary.pelican_backup
```

On Raven outside Docker:

```bash
python3 -m canary.pelican_backup
```

Example shape:

```json
{
  "generated_at": "2026-06-05T22:30:00-05:00",
  "host": "raven",
  "overall_status": "warning",
  "checks": {
    "pelican_backup": {
      "status": "critical",
      "timer": { "enabled": "enabled", "active": "active", "next_run": "..." },
      "service": { "active": "inactive", "result": "success" },
      "mount": { "mounted": true, "backing_source": "UUID=..." },
      "archive": { "latest_name": "raven-recovery-....tar.zst", "age_hours": 40.2 },
      "alerts": []
    }
  },
  "pelican_alert": { "decision": "alert", "sent": true, "severity": "critical" },
  "alerts": [],
  "warnings": [],
  "critical": []
}
```

## Dashboard / Discord consumption

- `overall_status` — top-level health color
- `checks.storage.volumes[]` — per-drive cards (UUID-based, no sdX)
- `checks.pelican_backup` — Pelican timer/service/mount/archive detail
- `alerts[]` — flattened, severity-tagged messages ready for Discord embeds or dashboard toast lists
- `generated_at` — staleness detection if updates stop

## Local development

```bash
python3 -m compileall -q canary
python3 -m pytest tests/test_canary.py tests/test_canary_pelican.py -v
```

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `CANARY_INTERVAL_SECONDS` | `300` | Seconds between check runs |
| `CANARY_HOST_ROOT` | `/` | Host prefix when using `/host` mount in Docker |
| `CANARY_FSTAB_PATH` | `$HOST_ROOT/etc/fstab` | Read UUIDs and automount flags |
| `CANARY_STORAGE_VOLUMES` | built-in paths | JSON override for volume specs |
| `CANARY_VULTURE_SCHEDULER_TIMER` | `vulture-scheduler.timer` | Scheduler timer unit |
| `CANARY_DASHBOARD_CONTAINER` | `vulture-dashboard` | Expected dashboard container name |
| `CANARY_TIMEZONE` | `America/Chicago` | Timestamp timezone in JSON |
| `CANARY_PELICAN_TIMER_UNIT` | `pelican-backup.timer` | Pelican backup timer unit |
| `CANARY_PELICAN_SERVICE_UNIT` | `pelican-backup.service` | Pelican oneshot service unit |
| `CANARY_PELICAN_BACKUP_TARGET` | `/mnt/storage/pelican_backup` | Completed bundle directory |
| `CANARY_PELICAN_STALE_HOURS` | `36` | Critical when newest archive is older than this |
| `CANARY_PELICAN_STALE_WARN_HOURS` | `30` | Warning when archive age exceeds this (below stale) |
| `CANARY_DISCORD_WEBHOOK_URL` | (empty) | Discord webhook for Pelican alerts; falls back to `DISCORD_WEBHOOK_URL` |
| `CANARY_ALERT_STATE_PATH` | `data/canary_alert_state.json` | Persisted Pelican alert dedup state |

Per-command timeouts (`CANARY_*` not required): see `canary/config.py` (`TIMEOUT_DF`, `TIMEOUT_LSBLK`, etc.).
