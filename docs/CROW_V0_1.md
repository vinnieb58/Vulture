# Crow v0.1

Crow is the read-only Discord command and control layer for **The Aviary** — the operations surface for **Raven** (Ubuntu host) and **Vulture** (deal-hunting runtime).

## Why Crow lives in the Vulture repo (for now)

- The existing production Discord bot already runs from `discord_bot.py` in this repo.
- Raven deploy scripts (`scripts/update_raven.sh`) already manage `discord_bot.py` and `main.py` in tmux sessions on the same host.
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

## v0.1 command list (read-only)

| Command | Description |
|---------|-------------|
| `/raven_status` | Host summary: hostname, uptime, memory, `/` disk, load average, UTC timestamp |
| `/check_disk` | Disk usage for `/`, `/mnt` and `/media` mounts, and configured extras; warns ≥80%, critical ≥90% |
| `/check_memory` | Total / used / available memory and percent used |
| `/check_services` | Whether Discord bot, scheduler (`main.py`), and `bot` / `scheduler` tmux sessions appear active |
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

Checks use safe, timed subprocess calls (`hostname`, `uptime`, `df`, `free`, `pgrep`, `tmux ls`) and stdlib `/proc` reads where possible. Service states are **running**, **not detected**, or **unknown** — never a hard failure for the whole command.

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

## Future v0.2 ideas

- `/view_logs` — bounded, redacted log excerpts
- Controlled restart commands (bot / scheduler) with confirmation and admin gating
- Admin-only actions and role checks
- Docker / container awareness without socket mounting where possible
- Canary integration
- Nest dashboard integration
