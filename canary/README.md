# Canary v0.1 — read-only Raven monitoring

**Canary** is an Aviary monitoring service — lightweight, **read-only** Raven health checks in the Vulture monorepo. Platform context: [docs/current/AVIARY_PROJECT_CONTEXT.md](../docs/current/AVIARY_PROJECT_CONTEXT.md). It runs periodic health checks, writes a machine-readable status file, and logs each run. It does **not** perform control or admin actions.

## What Canary does

- Runs health checks every **5 minutes** by default (`CANARY_INTERVAL_SECONDS`, default `300`).
- Writes the latest snapshot to `data/canary_status.json`.
- Appends run logs to `logs/canary.log`.
- Evaluates overall status as `ok`, `warning`, or `critical`.
- Emits a top-level `alerts` array structured for Discord notifications and dashboard cards.

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

All subprocess calls (`lsblk`, `blkid`, `findmnt`, `df`, `systemctl`, `docker`, `ping`) and mount-path probes use bounded timeouts. Stale or hung mounts surface as `STALE_MOUNT` or `DF_TIMEOUT` instead of crashing Canary.

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
- No Discord alerting (JSON is alert-ready; sending is out of scope)
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
    "storage": {
      "status": "warning",
      "volumes": [
        {
          "label": "toshiba_ext",
          "mount_path": "/mnt/storage/toshiba_ext",
          "uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
          "fstype": "ext4",
          "status": "OK",
          "mounted": true,
          "use_percent": 42.0
        }
      ],
      "alerts": []
    },
    "services": { "status": "ok", "services": [], "alerts": [] }
  },
  "alerts": [
    {
      "severity": "warning",
      "category": "storage",
      "code": "MISSING_DEVICE",
      "volume": "microsd",
      "mount_path": "/mnt/storage/microsd",
      "message": "microsd: device UUID … not detected"
    }
  ],
  "warnings": [],
  "critical": []
}
```

## Dashboard / Discord consumption

- `overall_status` — top-level health color
- `checks.storage.volumes[]` — per-drive cards (UUID-based, no sdX)
- `alerts[]` — flattened, severity-tagged messages ready for Discord embeds or dashboard toast lists
- `generated_at` — staleness detection if updates stop

## Local development

```bash
python3 -m compileall -q canary
python3 -m pytest tests/test_canary.py -v
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

Per-command timeouts (`CANARY_*` not required): see `canary/config.py` (`TIMEOUT_DF`, `TIMEOUT_LSBLK`, etc.).
