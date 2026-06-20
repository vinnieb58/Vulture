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

The Nest home page shows a **compact Raven Health card** (~10 high-value lines):
status, uptime, live CPU/temp, 1h/24h peaks, CPU saturation, load averages, and
container count. A **Details →** link opens the full Glances-driven page at
`/raven/health`.

Live telemetry uses **[Glances](https://github.com/nicolargo/glances)** as the
primary host metrics source. Glances runs as a companion container in
`docker-compose.dashboard.yml`, exposes its REST API on the internal compose
network, and is bound to **localhost only** on the host (`127.0.0.1:61208`) for
LAN/Tailscale verification — not the public internet.

### Raven Health home card (Nest `/`)

| Field | Source |
|-------|--------|
| Status pill + headline | Host health + operating thresholds |
| Uptime | Host probe |
| CPU now | Glances `/api/4/cpu` (fallback: `/proc`) |
| Peak CPU 1h / 24h | JSONL history |
| CPU >90% last hour | JSONL saturation |
| Temp now | Glances `/api/4/sensors` (fallback: sysfs) |
| Peak Temp 24h | JSONL history |
| Peak Memory 24h | JSONL history |
| Load 1/5/15 | Glances `/api/4/load` |
| Containers running | Docker snapshot |

Detailed telemetry (per-core CPU, swap, top processes, load pressure, etc.) lives
on the details page — not the home card.

### Raven Health details page (`/raven/health`)

Full-page read-only Glances dashboard with a dark card grid, SVG donut gauges,
sparklines, area charts, and progress bars. No heavy charting stack — just
vanilla JS helpers in `dashboard/static/raven_health.js`.

Visual sections:

- **Overview** — CPU / memory / swap / container donut gauges, load sparkline,
  temperature and disk progress bars, top CPU process table, 1h history charts
- **CPU** — total gauge, per-core bars, user/system/nice/idle legend
- **Memory** — RAM and swap gauges with used/cached/free legend
- **Processes** — compact table sorted by Glances (top CPU first)
- **Disks** — mount usage progress bars
- **Network** — interface table plus optional I/O history chart
- **Sensors** — temperature bars with highest temp emphasized
- **System** — host, uptime, OS/kernel, container summary

| Route | Purpose |
|-------|---------|
| `GET /raven/health` | Server-rendered details page (initial data + JS refresh) |
| `GET /api/raven/health/glances` | Normalized JSON payload for live updates |

Auto-refresh defaults to **5 seconds** on the details page via vanilla JS fetch
(`dashboard/static/raven_health.js`). Configure with
`DASHBOARD_RAVEN_HEALTH_REFRESH_SECONDS`. The page keeps the last good snapshot
visible if a refresh fails (badge shows **Stale**). The Nest home page still
uses the global meta refresh (`DASHBOARD_AUTO_REFRESH_SECONDS`).

History charts use existing JSONL samples when available. The API exposes both
legacy and normalized keys:

- `cpu_1h` / `cpu_history_1h`
- `load_1h` / `load_history_1h`
- `memory_1h` / `memory_history_1h`
- `network_1h` / `network_history_1h` (empty until network history exists)

When history is missing, cards show a dashed empty state instead of a blank area.

Glances API v4 endpoints used:

- Core: `/cpu`, `/load`, `/mem`, `/memswap`, `/sensors`, `/percpu`, `/processlist`
- Details extras: `/fs`, `/network`, `/uptime`, `/system`, `/docker` (when available)

Data is normalized in Python (`dashboard/raven_health_details.py`) before
rendering so templates stay simple.

### Why Glances is preferred

Glances provides richer, battle-tested host telemetry in one API:

- Live CPU % (total and per-core)
- Load averages (1/5/15)
- Memory and swap
- CPU/package temperature from sensors
- Top processes by CPU

The dashboard no longer needs its own blocking `/proc/stat` polling for the
Raven Health card. The legacy JSONL history file remains available for 1h/24h
peak reporting and as a fallback when Glances is down.

### Glances service

| Setting | Default | Purpose |
|---------|---------|---------|
| `DASHBOARD_GLANCES_URL` | `http://glances:61208` | Glances REST base URL (dashboard container) |
| `DASHBOARD_USE_GLANCES` | `true` in compose | Use Glances for live Raven Health metrics |
| `DASHBOARD_GLANCES_REQUEST_TIMEOUT_SECONDS` | `1.0` | Per-endpoint socket timeout |
| `DASHBOARD_GLANCES_FETCH_BUDGET_SECONDS` | `1.5` | Shared wall-clock budget for one snapshot (all endpoints fetched in parallel) |
| `DASHBOARD_GLANCES_TOP_PROCESSES` | `5` | Number of top CPU processes on details/overview |
| `DASHBOARD_RAVEN_HEALTH_REFRESH_SECONDS` | `5` | Details page JS auto-refresh interval |

Glances container command:

```bash
glances -w --bind 0.0.0.0 --port 61208
```

Host bind mounts for accurate metrics: `/proc`, `/sys`, `/dev`, `/run/udev`
(read-only). The compose file maps port `61208` to `127.0.0.1` only.

### Fallback behavior

When `DASHBOARD_USE_GLANCES=true` and Glances is unreachable or slow:

- The **home card** keeps working with fallback host probes + JSONL peaks (no crash)
- The **details page** shows a visible **Glances unavailable** banner and uses
  fallback data where available
- Live CPU/load/temp from existing `/proc` + sysfs readers where possible
- JSONL history peaks (1h/24h CPU, memory, load, temp) when samples exist
- `/health` is unchanged — liveness probe only, no Glances dependency

Glances endpoints are fetched **in parallel** with a shared budget (default **1.5s**
total, **1.0s** per request). Page load does not wait for seven sequential slow
responses; when the budget is exceeded, fallback is immediate.

### Legacy JSONL history (optional)

The background sampler is **disabled by default** when Glances is enabled
(`DASHBOARD_METRICS_SAMPLER_ENABLED=0` in compose). JSONL history is kept for
peak/saturation reporting and optional fallback sampling.

| Component | Behavior |
|-----------|----------|
| Glances API | Primary live telemetry for Raven Health details + home card live values |
| Nest home (`/`) | Compact Raven Health summary card |
| Raven details (`/raven/health`) | Full Glances telemetry with 5s JS refresh |
| `/api/raven/health/glances` | Normalized JSON for details page polling |
| Background sampler | Off by default with Glances; enable with `DASHBOARD_METRICS_SAMPLER_ENABLED=1` |
| `/health` | No metrics — liveness probe only (Docker HEALTHCHECK) |

If both Glances and the background sampler are disabled, 1h/24h aggregates may
be sparse unless you re-enable the sampler temporarily.

### Load average vs CPU %

Linux **load average** counts runnable processes, not CPU utilization. On a
4-thread Chromebox, a load of ~6 can be normal under bursty work while current
CPU % is much lower. The card shows:

- **CPU now** — utilization % from Glances (`/api/4/cpu`)
- **CPU per core** — per-core utilization from `/api/4/percpu` when available
- **Load 1/5/15** — from Glances `/api/4/load`
- **CPU threads** — logical CPU count
- **Load pressure** — `load_1 / cpu_threads` (values above 1.0 suggest queuing)
- **Top CPU processes** — from `/api/4/processlist`
- **Peak load avg** — peak 1-minute load in the Details section (JSONL history), with tooltip:
  *"Load is runnable work, not CPU %. Compare load to CPU threads."*

### CPU temperature

Temperature is read from Glances `/api/4/sensors`, preferring CPU/package labels
(`Package id 0`, `x86_pkg_temp`, `coretemp`, etc.). When Glances is unavailable,
the dashboard falls back to host sysfs thermal zones via `/host/root/sys`.

### Sampling and retention (legacy JSONL)

| Setting | Default | Purpose |
|---------|---------|---------|
| `DASHBOARD_METRICS_HISTORY_PATH` | `/app/data/raven_metrics_history.jsonl` | JSONL sample file |
| `DASHBOARD_METRICS_SAMPLE_INTERVAL_SECONDS` | `60` | Minimum seconds between samples |
| `DASHBOARD_METRICS_RETENTION_HOURS` | `48` | Hours of history to keep |
| `DASHBOARD_METRICS_SAMPLER_ENABLED` | `0` with Glances | Background sampler thread |

Each legacy sample records: timestamp, CPU %, memory %, load 1/5/15, CPU temp,
CPU thread count, and raw jiffies (for delta-based CPU % on the next sample).

Samples are stored at `./data/raven_metrics_history.jsonl`.

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

Verify Glances API on localhost (LAN/Tailscale only — not public):

```bash
curl http://localhost:61208/api/4/cpu
curl http://localhost:61208/api/4/load
curl http://localhost:61208/api/4/mem
curl http://localhost:61208/api/4/sensors
curl http://localhost:61208/api/4/processlist
```

Verify dashboard display and fallback:

```bash
curl -sf http://localhost:8088/health
curl -s http://localhost:8088/ | grep -E 'Raven Health|Details'
curl -s http://localhost:8088/raven/health | grep -E 'CPU Usage|Load Average|Memory Usage|Disk Usage|Network|Top CPU Processes'
curl -s http://localhost:8088/api/raven/health/glances | jq '.status'
```

Optional: confirm legacy JSONL history still works when sampler is re-enabled:

```bash
# Re-enable sampler temporarily (e.g. while validating Glances stability):
# DASHBOARD_METRICS_SAMPLER_ENABLED=1 in compose, then rebuild.
watch -n 10 'wc -l ./data/raven_metrics_history.jsonl'
tail -f ./data/raven_metrics_history.jsonl
docker logs vulture-dashboard 2>&1 | grep -i "metrics sampler"
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
| `DASHBOARD_HOST_SYS` | `/host/root/sys` | Host sysfs for CPU temperature (Glances fallback) |
| `DASHBOARD_GLANCES_URL` | `http://glances:61208` | Glances REST base URL |
| `DASHBOARD_USE_GLANCES` | `false` (code) / `true` (compose) | Use Glances for live metrics |
| `DASHBOARD_GLANCES_REQUEST_TIMEOUT_SECONDS` | `1.0` | Per-endpoint Glances socket timeout |
| `DASHBOARD_GLANCES_FETCH_BUDGET_SECONDS` | `1.5` | Shared snapshot budget (parallel fetch) |
| `DASHBOARD_GLANCES_TOP_PROCESSES` | `5` | Top CPU processes shown on card |
| `DASHBOARD_SCHEDULER_FRESH_MINUTES` | `30` | Freshness window for scheduler logs |
| `DASHBOARD_METRICS_HISTORY_PATH` | `/app/data/raven_metrics_history.jsonl` | Metrics JSONL path |
| `DASHBOARD_METRICS_SAMPLE_INTERVAL_SECONDS` | `60` | Min seconds between metric samples |
| `DASHBOARD_METRICS_RETENTION_HOURS` | `48` | Hours of metric history to retain |
| `DASHBOARD_METRICS_SAMPLER_ENABLED` | `0` with Glances | Background sampler on/off |
| `DASHBOARD_TEMP_WARN_CELSIUS` | `80` | CPU temp WARN threshold (°C) |
| `DASHBOARD_TEMP_CRITICAL_CELSIUS` | `90` | CPU temp FAIL threshold (°C) |
| `DASHBOARD_CPU_SAT_THRESHOLD` | `90` | CPU % saturation threshold |
| `DASHBOARD_CPU_SAT_WARN_MINUTES_1H` | `10` | WARN minutes above threshold in 1h |
| `DASHBOARD_CPU_SAT_CRITICAL_MINUTES_1H` | `30` | FAIL minutes above threshold in 1h |

## Local validation

```bash
python3 -m compileall -q dashboard
python3 -m pytest tests/test_dashboard.py tests/test_raven_metrics_history.py tests/test_glances_metrics.py tests/test_dashboard_storage.py -q
```
