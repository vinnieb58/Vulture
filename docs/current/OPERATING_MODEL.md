# Vulture 2.0 — Operating Model

_Last updated: 2026-06-10_

**Scope:** This document describes how the **Vulture** deal-hunting service runs. For the broader **Aviary** platform (Raven, Crow, Canary, Roost, dashboard), see [AVIARY_PROJECT_CONTEXT.md](AVIARY_PROJECT_CONTEXT.md).

---

## 1. Current baseline: Vulture 2.0+

Vulture 2.0+ is the active, in-production version on Raven. The v1.0 designation in older docs and `config/hunts.yaml` refers to the original YAML-only, Craigslist-only design. That path remains for dev compatibility but is **not** the Raven production default.

---

## 2. System layers

### Discord bot — command and control

`discord_bot.py` is the primary interface for managing hunts at runtime. It also hosts **Crow** read-only ops commands on the same bot instance (`vulture-bot.service`).

Users create, list, pause, resume, and end hunts through Discord slash commands. The bot writes hunt definitions to SQLite; no YAML editing is required in production.

### `main.py` — single hunt-cycle runner

`main.py` executes one complete hunt cycle:

1. Load environment (`.env`).
2. Initialize SQLite (`data/vulture.db`).
3. Load enabled hunts from the configured source (`VULTURE_HUNT_SOURCE`).
4. For each hunt (with multi-source fan-out): adapter → filter → dedupe → persist → Discord alert.
5. Log a cycle summary and exit.

`main.py` does not loop. Repetition is the scheduler's responsibility.

### Scheduler — systemd on Raven

On Raven production:

| Unit | Role |
|------|------|
| `vulture-bot.service` | Long-running `discord_bot.py` |
| `vulture-scheduler.timer` | Triggers hunt cycles every 15 minutes |
| `vulture-scheduler.service` | Oneshot `main.py` — **inactive between runs is normal** |

tmux was previously used for bot/scheduler longevity; that model is **deprecated** for production. Use tmux only for optional manual debugging.

See [RAVEN_SYSTEMD_RUNTIME.md](RAVEN_SYSTEMD_RUNTIME.md).

---

## 3. Hunt source: SQLite is the production source of truth

| `VULTURE_HUNT_SOURCE` | Behavior |
|-----------------------|----------|
| `db` | Load from SQLite `hunts` table only — **Raven production default** (`.env.example`) |
| `yaml` | Load from `config/hunts.yaml` only — v1.0 / dev fallback |
| `mixed` | Load both; YAML wins on name collision |

If the variable is unset, `main.py` falls back to `yaml` — production `.env` must set `db`.

---

## 4. `config/hunts.yaml` — legacy and dev only

Not the production hunt store. Used for:

- `VULTURE_HUNT_SOURCE=yaml` local workflows
- Dev/test seeding
- Mixed-mode supplement

---

## 5. Adapters and registry

Adapter dispatch is **implemented** in `adapters/registry.py`. `main.py` calls `get_adapter(source)` — not scattered `if/elif` chains.

### Registered runtime adapters (current)

| Source | Classification | Notes |
|--------|----------------|-------|
| `craigslist` | **stable** | Primary production adapter |
| `mercari` | **beta** | GraphQL search |
| `microcenter` | **beta** | Playwright; computer/laptop verticals |
| `offerup` | experimental | GeoIP-only location |
| `carsdotcom` | experimental | Playwright; vehicles; flaky |
| `swappa`, `bestbuy`, `newegg` | experimental | Vertical profiles in `engine/source_selection.py` |

Probe-only work (eBay, etc.) lives under `experiments/adapters/` without runtime registration.

Vertical-aware defaults: `engine/source_selection.py` selects `source_sites` per translated hunt category.

---

## 6. Runtime filtering is deterministic

Listing pass/fail is exclusively `engine/rules.py` — price, keywords, and structured constraints (TV size, GPU tier, RAM, vehicle year/miles). No LLM at runtime.

---

## 7. LLM translation — hunt creation only

Natural-language `/hunt` intents are translated into structured hunt rows before SQLite persistence. Vehicle intents route through `engine/intent_translator_v2.py`. Once stored, hunts are plain data records evaluated deterministically at scrape time.

---

## 8. Raven — headless runtime target

Raven is the Aviary physical host. Vulture runs without GUI; logs go to `logs/vulture.log` and journald (bot/scheduler units).

Working directory for scheduled runs must be the repo root so `data/`, `logs/`, and `config/` resolve correctly.

---

## 9. `.env` — local only, never committed

See `.env.example` for variables. Key production values:

| Variable | Purpose |
|----------|---------|
| `DISCORD_WEBHOOK_URL` | Webhook alerts for new listings |
| `DISCORD_BOT_TOKEN` | Bot token for `discord_bot.py` |
| `DISCORD_GUILD_ID` | Optional dev guild for instant slash registration |
| `VULTURE_HUNT_SOURCE` | `db` on Raven |

---

## Architecture summary

```text
Discord (Vulture hunts + Crow /check)
  └── discord_bot.py → SQLite hunts (data/vulture.db)

vulture-scheduler.timer
  └── vulture-scheduler.service → main.py (oneshot)
        └── VULTURE_HUNT_SOURCE=db
              └── per hunt / per source:
                    adapters.registry.get_adapter()
                    engine.rules (deterministic)
                    engine.database (dedupe)
                    engine.notifier (Discord webhook)
```

---

## What is NOT current

- YAML as primary production hunt configuration
- tmux as production scheduler/bot supervisor
- Adapter registry as "planned future work"
- Craigslist as the only registered adapter
- Treating this repo as "Vulture-only" with no Aviary/Crow/Canary context
