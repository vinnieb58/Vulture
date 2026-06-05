# Crow v0.2 — Raven Health & Reboot Awareness

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

Legacy v0.1 commands (`/raven_status`, `/check_disk`, `/check_vulture`, etc.) remain available.

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
⚠ portable_beast — MISSING
⚠ toshiba_ext — MISSING
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
⚠ portable_beast — missing
⚠ toshiba_ext — missing
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

## Configuration

In addition to [v0.1 environment variables](CROW_V0_1.md), v0.2 supports:

| Variable | Purpose |
|----------|---------|
| `CROW_EXPECTED_MOUNTS` | Comma-separated `Label:/path` pairs (default includes `/`, `/mnt/microsd`, `/mnt/portable_beast`, `/mnt/toshiba_ext`) |
| `CROW_RAVEN_HEALTHCHECK_SCRIPT` | Optional path to `~/raven_healthcheck.sh` for future external fallback |
| `CROW_RAVEN_POST_REBOOT_SCRIPT` | Optional path to `~/raven_post_reboot_check.sh` |

Example:

```bash
export CROW_EXPECTED_MOUNTS="Root SSD:/,MicroSD:/mnt/microsd,portable_beast:/mnt/portable_beast,toshiba_ext:/mnt/toshiba_ext"
```

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
  commands/
    check.py             # /check command group handlers
```

Business logic lives in `crow/system/` so Canary, Nest, and future REST endpoints can reuse the same functions without importing Discord handlers.

## Tests

```bash
pytest tests/test_crow.py tests/test_crow_system.py -v
```

## Explicitly out of scope (v0.2)

- `/restart`, `/stop`, `/reboot`, `/shutdown`
- Docker control (restart, pause, remove)
- Arbitrary shell execution

Observe first. Control later.

## Future v0.3 ideas

- Canary alert integration from `/check` status APIs
- Nest dashboard cards consuming `crow/system` helpers
- Bounded `/view_logs` excerpts
- Admin-gated controlled restarts
