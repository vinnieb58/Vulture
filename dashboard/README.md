# Aviary Dashboard v0.2 (Vulture repo)

Read-only operational dashboard for **Raven** (Aviary host). Observe host health, Vulture runtime,
hunts, adapters, storage, Docker, and logs — without any write or admin controls.

## What v0.2 shows

- **Summary cards** — hunt counts, running containers, failed systemd units
- **Raven Health** — hostname, uptime, CPU %, CPU saturation, CPU temperature,
  memory usage, load average (with thread count and load pressure), containers
- **Key Services** — ssh/ssh.socket, tailscaled, smbd, docker, vulture-bot,
  vulture-scheduler timer (`is-active` / `is-enabled`)
- **Vulture Runtime** — bot/scheduler process or systemd status, tmux sessions,
  log mtime, scheduler health (timer heartbeat + oneshot service idle/running)
- **Hunts** — schema-tolerant hunt table (name, status, sources, timestamps,
  max price, query, vertical when columns exist)
- **Adapter Summary** — per-source listing counts, latest listing, recent log errors
- **Storage / Roost** — root and expected storage mounts with real mountpoint
  detection (`findmnt`, `/proc/mountinfo`), systemd automount/mount unit state,
  UUID validation, and legacy path handling
- **Docker** — daemon status, container counts, running container table
- **Logs** — split recent errors/warnings vs general lines
- **Warnings** — defensive alerts (missing DB/log, failed commands, missing mounts)
- **Auto-refresh** — page reload every 60 seconds with “Last refreshed” timestamp

## Run

From the Vulture repo root on Raven:

```bash
./scripts/rebuild_docker.sh --file docker-compose.dashboard.yml
```

Rebuild all compose stacks in the repo (currently just the dashboard):

```bash
./scripts/rebuild_docker.sh
```

`scripts/rebuild_docker.sh` and `scripts/update_raven_quick.sh` ensure stable
storage mountpoint directories exist before starting the dashboard container.

Manual prep if needed (drives may be unplugged):

```bash
sudo mkdir -p /mnt/storage/{microsd,toshiba_ext,portable_beast,pelican_backup,raven_nvme,roost_spinning_0}
```

`scripts/update_raven.sh` also creates these directories and restarts the dashboard
automatically (skip with `SKIP_DASHBOARD_RESTART=1`).

Lower-level equivalent:

```bash
docker compose -f docker-compose.dashboard.yml up -d --build
```

## Open

- http://raven:8088
- http://192.168.1.143:8088
- http://100.82.1.18:8088

## Read-only guarantee

This dashboard does **not** start, stop, restart, or modify hunts, services,
containers, or schedules. It only runs read-only queries and observability
commands with timeouts. Missing data surfaces as warnings, not crashes.

## Host command execution

The slim container does not run systemd itself. Service checks use the host's
`systemctl` via `chroot /host/root` or `nsenter` (with `pid: host`), connecting
through the mounted D-Bus socket (`DASHBOARD_SYSTEMD_BUS_SOCKET`). Docker and
Tailscale commands use the same host execution path.

## Host mounts (docker-compose)

The container uses scoped read-only host mounts for observability:

- `./data` and `./logs` — Vulture SQLite DB and main log
- `/host/root` — host root filesystem usage
- `/host/proc` — host load/memory/mount tables
- `/etc/hostname` — hostname
- `/var/run/docker.sock` — read-only Docker status
- `/run/systemd` and D-Bus socket — host `systemctl` status
- `/mnt/storage` — parent bind for Roost / external storage (read-only).
  Individual optional drive paths are **not** bind-mounted directly, so unplugged
  USB/HDD drives cannot prevent the container from starting.

`pid: host` allows process/tmux visibility on the host.

## Resilience to missing optional drives

The dashboard container starts even when optional external drives are unplugged.
Docker bind-mounts only the stable `/mnt/storage` parent directory — not fragile
per-drive paths that break with `no such device` when a drive is removed.

Inside the container, the Storage / Roost section checks expected subpaths:

- `/mnt/storage/microsd`
- `/mnt/storage/toshiba_ext`
- `/mnt/storage/portable_beast`
- `/mnt/storage/pelican_backup`
- `/mnt/storage/raven_nvme`
- `/mnt/storage/roost_spinning_0`

Each mount reports detailed statuses such as **OK**, **OK_AUTOMOUNTED**,
**AUTOMOUNT_WAITING**, **NOT_MOUNTED**, **NOT_MOUNTED_PARENT_ROOT**,
**LEGACY_PATH**, **PATH_MISSING**, or **ERROR**. Unplugged optional drives
appear as **warnings** on the dashboard — they do not crash the container or
fail the HTTP health endpoint.

### Quick recovery

```bash
./scripts/rebuild_docker.sh --file docker-compose.dashboard.yml
docker ps
curl -I http://localhost:8088
```

## Known limitations

- **systemctl / Docker / tailscale** require the corresponding host tools and
  mounts; failures show warnings instead of breaking the page.
- **Optional / automounted storage** shows yellow `AUTOMOUNT_WAITING` when the
  path exists but the backing device is not mounted (unplugged or not yet triggered).
- **Legacy `portable_beast`** is reported separately from active `pelican_backup`.
- **USB storage mounts** may show as missing or not mounted after reboot if Raven
  did not detect or mount external drives (known Raven issue). This is surfaced as
  a warning, not a container failure.
- **Scheduler health** uses `vulture-scheduler.timer` as the heartbeat. The
  oneshot `vulture-scheduler.service` is expected to be inactive between runs.
  Stale warnings apply only when the timer is active but hunt-cycle logs are old.
- **No authentication** — intended for local LAN / Tailscale access only.
- **Adapter errors** are matched heuristically from recent log lines.
- **LAN/Tailscale IP** accuracy depends on host network namespace visibility
  from the container.

## Raven Health metrics

The Nest Raven Health card samples host metrics on each page load (no separate
monitoring daemon). Samples are written to a local JSONL file and retained for
48 hours by default.

### Load average vs CPU %

Linux **load average** counts runnable processes, not CPU utilization. On a
4-thread Chromebox, a load of ~6 can be normal under bursty work while current
CPU % is much lower. The card shows:

- **CPU now** — utilization % from `/proc/stat`
- **Load 1/5/15** — traditional load averages
- **CPU threads** — logical CPU count from `/proc/cpuinfo`
- **Load pressure** — `load_1 / cpu_threads` (values above 1.0 suggest queuing)
- **Peak load avg** — peak 1-minute load in the Details section, with tooltip:
  *"Load is runnable work, not CPU %. Compare load to CPU threads."*

### CPU temperature

Temperature is read from host sysfs thermal zones via the `/host/root/sys`
bind mount:

- Preferred zones: `x86_pkg_temp`, `coretemp`, `k10temp`, `cpu`, `acpitz`
- Path: `/host/root/sys/class/thermal/thermal_zone*/temp` (millidegrees → °C)
- If no readable sensor is found, the card shows **not available** (no crash)

### Sampling and retention

| Setting | Default | Purpose |
|---------|---------|---------|
| `DASHBOARD_METRICS_HISTORY_PATH` | `/app/data/raven_metrics_history.jsonl` | JSONL sample file |
| `DASHBOARD_METRICS_SAMPLE_INTERVAL_SECONDS` | `60` | Minimum seconds between samples |
| `DASHBOARD_METRICS_RETENTION_HOURS` | `48` | Hours of history to keep |

Each sample records: timestamp, CPU %, memory %, load 1/5/15, CPU temp, CPU
thread count, and raw jiffies (for delta-based CPU % on the next sample).

### Operating thresholds

Configurable via environment variables (sane defaults):

| Variable | Default | Effect |
|----------|---------|--------|
| `DASHBOARD_TEMP_WARN_CELSIUS` | `80` | WARN when current temp exceeds |
| `DASHBOARD_TEMP_CRITICAL_CELSIUS` | `90` | FAIL when current temp exceeds |
| `DASHBOARD_CPU_SAT_THRESHOLD` | `90` | CPU % threshold for saturation |
| `DASHBOARD_CPU_SAT_WARN_MINUTES_1H` | `10` | WARN when above threshold this many minutes in last hour |
| `DASHBOARD_CPU_SAT_CRITICAL_MINUTES_1H` | `30` | FAIL when above threshold this many minutes in last hour |

### Deploy and verify on Raven

```bash
# From the Vulture repo root on Raven:
./scripts/rebuild_docker.sh --file docker-compose.dashboard.yml
```

Verify samples are populating:

```bash
# Check JSONL history file (one line per sample, appended every ~60s on page loads):
tail -3 ./data/raven_metrics_history.jsonl

# Confirm Nest page shows CPU/temp fields:
curl -s http://localhost:8088/ | grep -E 'CPU now|Temp now|CPU threads'

# Container liveness (unchanged):
curl -sf http://localhost:8088/health
```

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `VULTURE_DB_PATH` | `/app/data/vulture.db` | SQLite database path |
| `VULTURE_LOG_PATH` | `/app/logs/vulture.log` | Main Vulture log |
| `VULTURE_LOG_TAIL_LINES` | `100` | Lines to tail from log |
| `DASHBOARD_AUTO_REFRESH_SECONDS` | `60` | Meta refresh interval |
| `DASHBOARD_HOST_ROOT` | `/host/root` | Host root bind for `df` |
| `DASHBOARD_HOST_PROC` | `/host/proc` | Host proc bind |
| `DASHBOARD_HOST_SYS` | `/host/root/sys` | Host sysfs for CPU temperature |
| `DASHBOARD_SCHEDULER_FRESH_MINUTES` | `30` | Freshness window for scheduler logs |
| `DASHBOARD_METRICS_HISTORY_PATH` | `/app/data/raven_metrics_history.jsonl` | Metrics JSONL path |
| `DASHBOARD_METRICS_SAMPLE_INTERVAL_SECONDS` | `60` | Min seconds between metric samples |
| `DASHBOARD_METRICS_RETENTION_HOURS` | `48` | Hours of metric history to retain |
| `DASHBOARD_TEMP_WARN_CELSIUS` | `80` | CPU temp WARN threshold (°C) |
| `DASHBOARD_TEMP_CRITICAL_CELSIUS` | `90` | CPU temp FAIL threshold (°C) |
| `DASHBOARD_CPU_SAT_THRESHOLD` | `90` | CPU % saturation threshold |
| `DASHBOARD_CPU_SAT_WARN_MINUTES_1H` | `10` | WARN minutes above threshold in 1h |
| `DASHBOARD_CPU_SAT_CRITICAL_MINUTES_1H` | `30` | FAIL minutes above threshold in 1h |

## Local validation

```bash
python3 -m compileall -q dashboard
python3 -m pytest tests/test_dashboard.py tests/test_raven_metrics_history.py tests/test_dashboard_storage.py -q
```
