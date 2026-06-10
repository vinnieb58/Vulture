# Codebase Status (Current)

Last refreshed: 2026-06-10 (UTC)

**Platform context:** [AVIARY_PROJECT_CONTEXT.md](AVIARY_PROJECT_CONTEXT.md) — this file focuses on **what is implemented in code**.

## Git snapshot

- Branch at inspection: `cursor/aviary-docs-transition-3510` (docs refresh)
- Production target branch: `main`
- Repo name on Raven: `vulture` (directory path); platform name: **Aviary**

## Aviary services in this repo

| Service | Package / path | Entry / deploy |
|---------|----------------|----------------|
| Vulture | `main.py`, `engine/`, `adapters/` | `vulture-scheduler.timer` → `main.py` |
| Vulture + Crow Discord | `discord_bot.py`, `crow/` | `vulture-bot.service` |
| Canary | `canary/` | `docker-compose.canary.yml` |
| Dashboard | `dashboard/` | `docker-compose.dashboard.yml` (`:8088`) |
| Roost (storage) | — | Host `/mnt/storage/*`; observed by above |

## Runtime entrypoints

- `main.py` — scheduled/one-shot hunt cycle runner
- `discord_bot.py` — Discord slash commands (Vulture hunts + Crow v0.2 ops)
- `canary/app.py` — periodic health checks (Docker)
- `dashboard/app.py` — read-only ops UI (Docker)

## Core Vulture modules

- `engine/database.py` — SQLite listings + link dedupe
- `engine/hunt_repository.py` — SQLite hunts CRUD
- `engine/hunt_service.py` — hunt business rules + execution dict conversion
- `engine/command_router.py` — command dispatch
- `engine/llm_translator.py` — translator entry; vehicles → v2 pipeline
- `engine/intent_translator_v2.py` — deterministic vehicle translation
- `engine/source_selection.py` — vertical-aware `source_sites` selection
- `engine/rules.py` — deterministic listing filter
- `adapters/registry.py` — adapter registry + capability metadata

## Current runtime flow

```text
main.py
  -> init_db() + init_hunts_table()
  -> VULTURE_HUNT_SOURCE (yaml | db | mixed) — production: db via .env
  -> load hunts → _expand_hunt_sources() per source_sites
  -> adapters.registry.get_adapter(source)
  -> rules.rejection_reason() → database.save_listing() → notifier.send_discord_alert()
```

## Discord commands

**Vulture hunts:** `/hunt_list`, `/hunt_show`, `/hunt_create`, `/hunt_pause`, `/hunt_resume`, `/hunt_end`, `/hunt`, `/hunt_from_intent`

**Crow v0.1:** `/raven_status`, `/check_disk`, `/check_memory`, `/check_services`, `/check_vulture`, `/crow_help`

**Crow v0.2 `/check` group:** `/check raven`, `/check services`, `/check storage`, `/check docker`, `/check tailscale`, `/check network`, `/check reboot`, `/check uptime`, `/check ports`, `/check logs`

## Registered adapters (`adapters/registry.py`)

| Source | `status` | Browser | Notes |
|--------|----------|---------|-------|
| `craigslist` | stable | No | Primary adapter |
| `mercari` | beta | No | GraphQL |
| `microcenter` | beta | Yes | `storeid`; computer/laptop verticals |
| `offerup` | experimental | No | GeoIP-only location |
| `carsdotcom` | experimental | Yes | Vehicles; flaky |
| `swappa` | experimental | No | Electronics/gaming |
| `bestbuy` | experimental | Yes | Retail |
| `newegg` | experimental | No | Retail |

Probe-only: eBay and others under `experiments/adapters/` (not in `_REGISTRY`).

## Deployment model

- **Raven:** systemd for Vulture bot + scheduler; Docker for dashboard + Canary.
- **Not** multi-tenant SaaS — single-operator self-hosted.
- Hunt source on Raven: `VULTURE_HUNT_SOURCE=db` (`.env.example`).

## Storage paths (Roost)

Authoritative monitoring paths use **`/mnt/storage/*`** (dashboard `storage_config.py`, Canary `config.py`).

**Known mismatch:** `crow/config.py` default `CROW_EXPECTED_MOUNTS` still lists legacy `/mnt/microsd`, `/mnt/portable_beast`, `/mnt/toshiba_ext`. Override on Raven or update defaults in a future code change.

## Test status

Run maintained suite:

```bash
pytest tests
```

Root `pytest` without `tests/` may collect script-style files under `scripts/` — use `pytest tests` explicitly.

## Repository map

```text
.
├── main.py / discord_bot.py
├── adapters/          # registry + source adapters
├── engine/            # hunts, rules, translator, DB
├── crow/              # Discord ops (v0.2)
├── canary/            # monitoring (v0.1)
├── dashboard/         # ops UI (v0.2)
├── deploy/systemd/    # vulture-bot, vulture-scheduler.*
├── config/hunts.yaml  # legacy YAML hunts
├── docs/current/      # current docs (start: AVIARY_PROJECT_CONTEXT.md)
└── scripts/           # update_raven*.sh, smokes, healthchecks
```

## Verification commands

### Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest tests
python main.py                    # one cycle; set VULTURE_HUNT_SOURCE
python discord_bot.py             # if DISCORD_BOT_TOKEN set
python -m compileall -q adapters engine dashboard crow canary scripts main.py discord_bot.py
```

### Raven (production)

```bash
systemctl is-active vulture-bot vulture-scheduler.timer
journalctl -u vulture-scheduler.service -n 50 --no-pager
./scripts/update_raven_quick.sh
curl -I http://localhost:8088        # dashboard
cat data/canary_status.json          # if Canary running
```
