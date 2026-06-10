# Crow v0.1

Crow is the read-only Discord operations service for **Aviary** — observability for **Raven** (Ubuntu host) and **Vulture** (deal-hunting runtime). Authoritative platform context: [docs/current/AVIARY_PROJECT_CONTEXT.md](current/AVIARY_PROJECT_CONTEXT.md).

## Why Crow lives in the Vulture repo (for now)

- The existing production Discord bot already runs from `discord_bot.py` in this repo.
- Raven deploy scripts (`scripts/update_raven.sh`) restart `vulture-bot.service` and `vulture-scheduler.service` after a successful pull, install, validation, and one hunt cycle.
- v0.1 adds observability without splitting deployment or duplicating tokens.
- The `crow/` package is structured so it can later be extracted into its own container or repository when Docker and control features are ready.

## How to run

No change to startup:

```bash
python discord_bot.py
```

Crow registers additional slash commands on the same bot instance as Vulture hunt commands. Secrets remain in `.env` only (`DISCORD_BOT_TOKEN`, optional `DISCORD_GUILD_ID`).

Optional Crow-specific paths (no secrets):

| Variable | Purpose |
|----------|---------|
| `CROW_PROJECT_ROOT` | Repo root for path resolution (default: cwd) |
| `CROW_VULTURE_DB_PATH` | SQLite path override |
| `CROW_VULTURE_LOGS_DIR` | Logs directory override |
| `CROW_EXTRA_DISK_PATHS` | Comma-separated extra mount paths for `/check_disk` |
| `CROW_TIMEZONE` | IANA timezone for displayed timestamps (default: `America/Chicago` — Central, CST/CDT) |

## v0.1 command list (read-only)

| Command | Description |
|---------|-------------|
| `/raven_status` | Host summary: hostname, uptime, memory, `/` disk, load average, Central-time timestamp |
| `/check_disk` | Disk usage for `/`, `/mnt` and `/media` mounts, and configured extras; warns ≥80%, critical ≥90% |
| `/check_memory` | Total / used / available memory and percent used |
| `/check_services` | Whether `vulture-bot` / `vulture-scheduler` systemd units, `discord_bot.py`, and `main.py` processes appear active (plus recent journal excerpts) |
| `/check_vulture` | DB file presence, logs directory, latest log activity, scheduler visibility |
| `/crow_help` | Command list and v0.1 read-only notice |

Vulture hunt slash commands (`/hunt`, `/hunt_list`, etc.) are unchanged.

## What v0.1 intentionally does **not** do

- Restart, stop, or kill processes
- Run hunts or mutate the Vulture database
- Expose `.env`, tokens, or full environment variables
- Mount the Docker socket or run `docker` control commands
- Dockerize the bot or scheduler
- Move the existing Vulture runtime into containers
- Tail or download logs (deferred)
- Admin-only destructive actions

Checks use safe, timed subprocess calls (`hostname`, `uptime`, `df`, `free`, `pgrep`, `systemctl is-active`, `journalctl`) and stdlib `/proc` reads where possible. Service states are **running**, **not detected**, or **unknown** — never a hard failure for the whole command.

## Package layout

```
crow/
  __init__.py
  bot.py              # setup_crow() — register commands on existing tree
  config.py
  formatting.py
  commands/
    raven.py          # /raven_status, /check_disk, /check_memory
    vulture.py        # /check_vulture, /check_services
    help.py           # /crow_help
  checks/
    system.py
    services.py
    vulture.py
```

## Tests

```bash
pytest tests/test_crow.py -v
```

## v0.2 (current)

Raven health and reboot awareness via the `/check` command group. See [CROW_V0_2.md](CROW_V0_2.md).

## Future ideas

- `/view_logs` — bounded, redacted log excerpts
- Controlled restart commands (bot / scheduler) with confirmation and admin gating
- Admin-only actions and role checks
- Canary integration
- Nest dashboard integration
