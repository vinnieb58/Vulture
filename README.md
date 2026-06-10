# Aviary (monorepo)

**Aviary** is a personal home-lab platform. This repository hosts multiple services that run primarily on **Raven**, a headless Ubuntu server.

| Name | Role |
|------|------|
| **Aviary** | Umbrella platform |
| **Raven** | Physical server / infrastructure host |
| **Vulture** | Marketplace deal-hunting service (core engine in this repo) |
| **Crow** | Discord read-only ops / Raven health checks |
| **Canary** | Periodic read-only monitoring (JSON + logs) |
| **Dashboard** | Read-only web ops UI (`:8088`) |
| **Roost** | Storage/NAS layer (`/mnt/storage/*`) |

**Start here:** [docs/current/AVIARY_PROJECT_CONTEXT.md](docs/current/AVIARY_PROJECT_CONTEXT.md) — authoritative platform context, service catalog, Raven runtime, and agent guidance.

Vulture-specific docs live under `docs/current/VULTURE_*.md`. Crow: [docs/CROW_V0_2.md](docs/CROW_V0_2.md).

---

## Repository layout

```text
.
├── main.py                 # Vulture hunt-cycle runner (one shot per invocation)
├── discord_bot.py          # Discord bot: Vulture hunt commands + Crow ops
├── adapters/               # Marketplace source adapters + registry
├── engine/                 # Hunts, rules, translator, DB, notifications
├── crow/                   # Crow Discord ops package (v0.2)
├── canary/                 # Canary monitoring service (v0.1)
├── dashboard/              # Read-only ops dashboard (v0.2)
├── deploy/systemd/         # Raven production unit files
├── config/hunts.yaml       # Legacy YAML hunts (dev / yaml mode only)
├── data/vulture.db         # SQLite (runtime; auto-created)
├── docs/current/           # Current-state documentation
└── scripts/                # Raven deploy and smoke scripts
```

---

## Quick start (development)

### 1. Clone and virtualenv

```bash
git clone <repo-url>
cd vulture   # repo directory name may still be "vulture" on Raven
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

Copy `.env.example` to `.env` and set at minimum:

```bash
DISCORD_BOT_TOKEN=...          # for discord_bot.py (Vulture + Crow)
DISCORD_WEBHOOK_URL=...        # for hunt alert webhooks
VULTURE_HUNT_SOURCE=db         # production-style: SQLite hunts from Discord
```

### 3. Run Vulture

One hunt cycle (loads hunts per `VULTURE_HUNT_SOURCE`):

```bash
python main.py
```

Discord bot (Vulture hunt commands + Crow `/check` commands):

```bash
python discord_bot.py
```

### 4. Tests

```bash
pytest tests
```

---

## Vulture (deal-hunting service)

Vulture scrapes configured marketplace sources, applies **deterministic** rules, deduplicates by listing URL in SQLite, and sends Discord alerts for new matches.

### Production behavior (Vulture 2.0+)

- Hunts are created and managed via **Discord slash commands** (`/hunt`, `/hunt_list`, …) and stored in **SQLite** (`data/vulture.db`).
- `VULTURE_HUNT_SOURCE=db` is the production default (see `.env.example`).
- `main.py` is invoked on a schedule (on Raven: **`vulture-scheduler.timer`** every 15 minutes).
- Adapter dispatch goes through **`adapters/registry.py`** with multi-source fan-out per hunt vertical.
- Runtime filtering is **deterministic only** (`engine/rules.py`) — no LLM at scrape time.

See [docs/current/VULTURE_2_0_ARCHITECTURE.md](docs/current/VULTURE_2_0_ARCHITECTURE.md) and [docs/current/OPERATING_MODEL.md](docs/current/OPERATING_MODEL.md).

### Legacy v1.0 compatibility

The original YAML-only, Craigslist-only, webhook-only workflow remains available for local/dev use:

- Set `VULTURE_HUNT_SOURCE=yaml` and edit `config/hunts.yaml`.
- Schedule with cron, Task Scheduler, or manual runs.

That path is **not** the Raven production default. See the historical sections in git history or `docs/current/VULTURE_2_0_SOURCE_CLEANUP_AND_HISTORY.md` for the v1.0 → 2.0 transition narrative.

---

## Crow, Canary, dashboard

| Service | Docs | Run on Raven |
|---------|------|--------------|
| Crow | [docs/CROW_V0_2.md](docs/CROW_V0_2.md) | Same bot as Vulture (`vulture-bot.service`) |
| Canary | [canary/README.md](canary/README.md) | `docker compose -f docker-compose.canary.yml up -d` |
| Dashboard | [dashboard/README.md](dashboard/README.md) | `docker compose -f docker-compose.dashboard.yml up -d` |

---

## Raven production deploy

On Raven, use tracked scripts from the repo root:

```bash
./scripts/update_raven_quick.sh          # default operational update
./scripts/update_raven.sh                # full validation + one hunt cycle
```

Systemd model: [docs/current/RAVEN_SYSTEMD_RUNTIME.md](docs/current/RAVEN_SYSTEMD_RUNTIME.md).

---

## Documentation index

| Document | Purpose |
|----------|---------|
| [AVIARY_PROJECT_CONTEXT.md](docs/current/AVIARY_PROJECT_CONTEXT.md) | Platform vision, services, Raven inventory, accuracy notes |
| [CODEBASE_STATUS.md](docs/current/CODEBASE_STATUS.md) | Implementation-grounded code map |
| [PROJECT_STATUS.md](docs/current/PROJECT_STATUS.md) | Current workstream / priorities |
| [OPERATING_MODEL.md](docs/current/OPERATING_MODEL.md) | How Vulture runs in production |
| [SESSION_LOG.md](docs/current/SESSION_LOG.md) | Session history |
