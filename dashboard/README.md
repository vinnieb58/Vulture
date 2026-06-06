# Vulture Dashboard v0.2

Read-only operational dashboard for Raven. Observe host health, Vulture runtime,
hunts, adapters, storage, Docker, and logs — without any write or admin controls.

## What v0.2 shows

- **Summary cards** — hunt counts, running containers, failed systemd units
- **Raven Health** — hostname, server time, uptime, boot time, LAN/Tailscale IP,
  internet reachability, failed systemd units, CPU load, memory usage
- **Key Services** — ssh/ssh.socket, tailscaled, smbd, docker, vulture-bot,
  vulture-scheduler (`is-active` / `is-enabled`)
- **Vulture Runtime** — bot/scheduler process or systemd status, tmux sessions,
  log mtime, scheduler freshness heuristic
- **Hunts** — schema-tolerant hunt table (name, status, sources, timestamps,
  max price, query, vertical when columns exist)
- **Adapter Summary** — per-source listing counts, latest listing, recent log errors
- **Storage / Roost** — root and expected USB mounts under `/mnt/storage/*`
- **Docker** — daemon status, container counts, running container table
- **Logs** — split recent errors/warnings vs general lines
- **Warnings** — defensive alerts (missing DB/log, failed commands, missing mounts)
- **Auto-refresh** — page reload every 60 seconds with “Last refreshed” timestamp

## Run

From the Vulture repo root on Raven:

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
- `/mnt/storage/*` — expected external storage paths

`pid: host` allows process/tmux visibility on the host.

## Known limitations

- **systemctl / Docker / tailscale** require the corresponding host tools and
  mounts; failures show warnings instead of breaking the page.
- **USB storage mounts** may show as missing after reboot if Raven did not
  detect or mount external drives (known Raven issue).
- **Scheduler freshness** is a log-tail heuristic, not a heartbeat API.
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
| `DASHBOARD_STORAGE_MOUNTS` | see compose file | `Label:path` comma list |
| `DASHBOARD_SCHEDULER_FRESH_MINUTES` | `30` | Freshness window for scheduler logs |

## Local validation

```bash
python3 -m compileall -q dashboard
python3 -m pytest tests/test_dashboard.py -q
```
