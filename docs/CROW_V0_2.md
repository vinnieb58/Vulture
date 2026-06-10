# Crow v0.2 — Raven Health & Reboot Awareness

**Crow** is the Aviary Discord operations service (read-only). It runs inside `discord_bot.py` / `vulture-bot.service` alongside Vulture hunt commands. Platform context: [docs/current/AVIARY_PROJECT_CONTEXT.md](current/AVIARY_PROJECT_CONTEXT.md).

Crow v0.2 expands the read-only Discord ops console with Raven health visibility aligned to:

- `scripts/raven_healthcheck.sh`
- `scripts/raven_post_reboot_check.sh`

These shell scripts remain the on-host source of truth. Crow implements equivalent internal checks so Discord, future Canary alerts, and Nest dashboards can share the same Python APIs.

## How to run

No change to startup:

```bash
python discord_bot.py
```

Crow registers `/check` subcommands on the same bot instance as Vulture hunt commands and legacy v0.1 ops commands.

## `/check` commands (read-only)

All responses use **Discord embeds** with color coding:

| Color | Meaning |
|-------|---------|
| Green | OK |
| Yellow | WARN |
| Red | FAIL |

| Command | Description |
|---------|-------------|
| `/check raven` | High-level Raven health summary (network, storage, services, Vulture, Docker) |
| `/check services` | Critical systemd units: SSH, Tailscale, Samba, Docker, Vulture Bot, Vulture Scheduler |
| `/check storage` | Expected mounts and disk usage; highlights missing USB/storage |
| `/check docker` | Docker daemon status, running and stopped container names |
| `/check tailscale` | Tailscale connected state, IPv4, hostname |
| `/check network` | Internet reachability, LAN IPv4, Tailscale IPv4 |
| `/check reboot` | Post-reboot validation checklist |
| `/check uptime` | Host uptime and last boot time |
| `/check ports` | Summarized listening services (no full socket dump) |
| `/check logs` | Sanitized recent warning/error summary from known Vulture log files and optional journal excerpts |

Legacy v0.1 commands (`/raven_status`, `/check_disk`, `/check_vulture`, etc.) remain available.

Use `/crow_help` for the full command list, including the `/check` group under **Raven / system checks** and legacy v0.1 commands listed separately.

## Example responses

### `/check raven`

```text
Raven Status
Hostname: raven
Uptime: 3 days 4 hours
Network:
✅ Internet
✅ Tailscale
Storage:
✅ Root SSD — 35% used
⚠ toshiba_ext — NOT_MOUNTED (example)
⚠ roost_spinning_0 — AUTOMOUNT_WAITING (example)
Services:
✅ SSH — ACTIVE
✅ Tailscale — ACTIVE
...
Overall: WARN
```

### `/check reboot`

```text
Post-Reboot Validation
✅ SSH
✅ Tailscale
✅ Docker
✅ Samba
✅ Internet
⚠ toshiba_ext — not mounted (example)
⚠ microsd — missing (example)
✅ Vulture Bot
✅ Scheduler
Overall: WARN
```

### `/check ports`

```text
Open Services
22   SSH
445  Samba
9443 Portainer
8088 Vulture Dashboard
```

### `/check logs`

```text
Logs
Recent issues: Warnings: 1 | Errors: 1
Last warning/error lines:
• 2026-06-01 10:05:30,000 [WARNING] adapters.ebay: rate limited
• 2026-06-01 10:05:45,000 [ERROR] engine.hunt: hunt failed DISCORD_BOT_TOKEN=[REDACTED]
Last Vulture cycle:
2026-06-01 10:06:00,000 [INFO] main: Hunt cycle completed
Last bot startup:
2026-06-01 10:00:00,000 [INFO] __main__: Starting Vulture Discord bot
Log sources:
vulture.log: OK — 6 tail line(s) from vulture.log
journal:vulture-bot: Unavailable — journal excerpt unavailable
Overall: FAIL
```

Log excerpts are read only from configured paths (`logs/vulture.log` by default) and optional systemd journal units. Secrets, tokens, webhook URLs, and similar patterns are redacted before display.

## Configuration

In addition to [v0.1 environment variables](CROW_V0_1.md), v0.2 supports:

| Variable | Purpose |
|----------|---------|
| `CROW_EXPECTED_MOUNTS` | Comma-separated `Label:/path` pairs. **Default in code** still uses legacy `/mnt/microsd`, `/mnt/portable_beast`, `/mnt/toshiba_ext`. On Raven, set to `/mnt/storage/*` paths (see below). |
| `CROW_RAVEN_HEALTHCHECK_SCRIPT` | Optional path to `~/raven_healthcheck.sh` for future external fallback |
| `CROW_RAVEN_POST_REBOOT_SCRIPT` | Optional path to `~/raven_post_reboot_check.sh` |

Example:

Recommended on Raven (matches dashboard/Canary/Roost layout):

```bash
export CROW_EXPECTED_MOUNTS="Root SSD:/,MicroSD:/mnt/storage/microsd,Toshiba EXT:/mnt/storage/toshiba_ext,Pelican Backup:/mnt/storage/pelican_backup,Roost Spinning 0:/mnt/storage/roost_spinning_0,Raven NVME:/mnt/storage/raven_nvme"
```

Legacy default (code fallback if unset): `/mnt/microsd`, `/mnt/portable_beast`, `/mnt/toshiba_ext`.

## Package layout

```
crow/
  embeds.py              # Discord embed builders
  system/
    health.py            # Raven summary + post-reboot validation
    services.py          # systemd service checks
    storage.py           # mount and usage checks
    docker.py            # container visibility
    network.py           # internet, LAN, Tailscale
    ports.py             # summarized listening ports
    logs.py              # sanitized log summaries
  commands/
    check.py             # /check command group handlers
    help.py              # /crow_help command list
```

Business logic lives in `crow/system/` so Canary, Nest, and future REST endpoints can reuse the same functions without importing Discord handlers.

## Tests

```bash
pytest tests/test_crow.py tests/test_crow_system.py tests/test_crow_logs.py -v
```

## Explicitly out of scope (v0.2)

- `/restart`, `/stop`, `/reboot`, `/shutdown`
- Docker control (restart, pause, remove)
- Arbitrary shell execution

Observe first. Control later.

## Future v0.3 ideas

- Canary alert integration from `/check` status APIs
- Nest dashboard cards consuming `crow/system` helpers
- Longer bounded log excerpts or filtered views beyond `/check logs`
- Admin-gated controlled restarts
