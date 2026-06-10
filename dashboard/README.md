# Aviary Dashboard v0.2 (Vulture repo)

Read-only operational dashboard for **Raven** (Aviary host). Observe host health, Vulture runtime,
hunts, adapters, storage, Docker, and logs — without any write or admin controls.

## What v0.2 shows

- **Summary cards** — hunt counts, running containers, failed systemd units
- **Raven Health** — hostname, server time, uptime, boot time, LAN/Tailscale IP,
  internet reachability, failed systemd units, CPU load, memory usage
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

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `VULTURE_DB_PATH` | `/app/data/vulture.db` | SQLite database path |
| `VULTURE_LOG_PATH` | `/app/logs/vulture.log` | Main Vulture log |
| `VULTURE_LOG_TAIL_LINES` | `100` | Lines to tail from log |
| `DASHBOARD_AUTO_REFRESH_SECONDS` | `60` | Meta refresh interval |
| `DASHBOARD_HOST_ROOT` | `/host/root` | Host root bind for `df` |
| `DASHBOARD_HOST_PROC` | `/host/proc` | Host proc bind |
| `DASHBOARD_SCHEDULER_FRESH_MINUTES` | `30` | Freshness window for scheduler logs |

## Local validation

```bash
python3 -m compileall -q dashboard
python3 -m pytest tests/test_dashboard.py tests/test_dashboard_storage.py -q
```
