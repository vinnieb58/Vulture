# Vulture 2.0 — Operating Model

_Last updated: 2026-05-22_

---

## 1. Current baseline: Vulture 2.0

Vulture 2.0 is the active, in-production version. The v1.0 designation in `README.md` and older session logs refers to the original YAML-only, webhook-only design. That design is still present in the codebase for compatibility, but it is not the default runtime path.

---

## 2. System layers

### Discord bot — command and control

`discord_bot.py` is the primary interface for managing hunts at runtime. Users create, list, enable, disable, and delete hunts through Discord slash commands. The bot writes hunt definitions directly to the SQLite database; no file editing is required.

### `main.py` — single hunt-cycle runner

`main.py` executes one complete hunt cycle:

1. Load environment (`.env`).
2. Initialize SQLite (`data/vulture.db`).
3. Load enabled hunts from the configured source.
4. For each hunt: scrape → filter → deduplicate by URL → persist new listings → send Discord alerts.
5. Log a cycle summary.

`main.py` does not loop. It runs once and exits. Repetition is the scheduler's responsibility.

### Scheduler / task layer — repetition

An external scheduler (cron, systemd service, Windows Task Scheduler, or any equivalent) invokes `python main.py` on a fixed interval. The scheduler is not part of the Vulture codebase; it is an infrastructure concern.

On Raven production, **`vulture-scheduler.service`** owns the hunt cycle loop. **`vulture-bot.service`** runs `discord_bot.py`. See `docs/current/RAVEN_SYSTEMD_RUNTIME.md`.

---

## 3. Hunt source: SQLite is the normal source of truth

The `VULTURE_HUNT_SOURCE` environment variable controls where `main.py` loads hunts from:

| Value | Behavior |
|-------|----------|
| `db` | Load from SQLite `hunts` table only — **production default** |
| `yaml` | Load from `config/hunts.yaml` only — v1.0 / dev fallback |
| `mixed` | Load both; YAML wins on name collision |

In normal operation, `VULTURE_HUNT_SOURCE=db`. Hunts created via the Discord bot live in SQLite and are authoritative.

---

## 4. `config/hunts.yaml` — legacy and dev compatibility only

`config/hunts.yaml` is **not** the production hunt store. Its roles are:

- **Legacy path:** supports `VULTURE_HUNT_SOURCE=yaml` for anyone on the old workflow.
- **Dev / test seeding:** useful for bootstrapping a local database without the Discord bot.
- **Mixed-mode fallback:** available as a supplement when `VULTURE_HUNT_SOURCE=mixed`.

Production deployments should treat this file as a dev artifact. Changes to it have no effect when `VULTURE_HUNT_SOURCE=db`.

---

## 5. Adapters: Craigslist is the only stable production adapter

Craigslist (`adapters/craigslist.py`) is the only adapter that is tested, stable, and used in production. Hunt definitions with `source: craigslist` are the only ones that will execute successfully today.

Other sources (eBay, Facebook Marketplace, etc.) are not implemented. The `adapters/` directory and the `experiments/` directory contain exploration work, but no additional adapter is production-ready.

---

## 6. Adapter registry — next architecture foundation

The adapter registry is the planned mechanism for registering, discovering, and routing hunt execution to the correct adapter by source name. It is the next significant architectural milestone after Vulture 2.0 stabilizes.

Until the registry is complete, adapter dispatch is handled by direct conditional logic in `main.py`. Do not modify the adapter registry work in progress; treat it as a protected foundation.

---

## 7. Runtime filtering is deterministic

Listing pass/fail decisions at runtime are made exclusively by the rule engine (`engine/rules.py`). Rules evaluate:

- `max_price` — numeric ceiling
- `include_keywords` — at least one must match the listing title (case-insensitive substring)
- `exclude_keywords` — none may match the listing title (case-insensitive substring)

This logic is static and deterministic. It does not call any external service and has no probabilistic component.

---

## 8. LLM translation — hunt creation only, never runtime filtering

LLM assistance (if used) is scoped to **translating natural-language hunt requests into structured hunt objects** before they are persisted to SQLite. Once a hunt is stored, it is a plain data record. The LLM has no role in evaluating individual listings at runtime.

An LLM must never be in the critical path of deciding whether a scraped listing passes or fails. Runtime decisions belong to the deterministic rule engine.

---

## 9. Raven — headless runtime target

Raven is the target headless deployment environment. Vulture is designed to run without a GUI or interactive terminal. `main.py` writes all output to `logs/vulture.log` in addition to stdout, so it operates correctly when invoked by a scheduler in a headless context.

Production Raven uses **systemd** (`vulture-bot.service`, `vulture-scheduler.service`) — not tmux — for bot and scheduler lifecycle. tmux remains available only for optional manual debugging.

Ensure the working directory is set to the project root when invoking `python main.py` under a scheduler so that relative paths (`data/`, `logs/`, `config/`) resolve correctly.

---

## 10. `.env` — local only, never committed

`.env` holds secrets and local configuration. It is listed in `.gitignore` and must never be committed to version control.

Use `.env.example` as the canonical reference for required and optional variables:

| Variable | Purpose |
|----------|---------|
| `DISCORD_WEBHOOK_URL` | Webhook for v1.0-style alert delivery |
| `DISCORD_BOT_TOKEN` | Bot token for `discord_bot.py` |
| `DISCORD_GUILD_ID` | Guild ID for instant slash command registration (dev) |
| `VULTURE_HUNT_SOURCE` | Hunt source: `db` (default), `yaml`, or `mixed` |

If `.env` is missing, `python-dotenv` will not error; variables must then be set in the environment by other means (e.g. systemd environment file, container env vars).

---

## Architecture summary

```
Discord bot (discord_bot.py)
  └── creates / manages hunts in SQLite (data/vulture.db)

Scheduler (cron / systemd / Task Scheduler)
  └── invokes: python main.py  [on interval]
        └── loads hunts from SQLite (VULTURE_HUNT_SOURCE=db)
              └── for each hunt:
                    scrape (adapters/craigslist.py)
                    filter (engine/rules.py)  ← deterministic only
                    dedupe (engine/database.py)
                    persist new listings
                    alert (engine/notifier.py → Discord)
```

---

## What is NOT current

- `README.md` still describes the v1.0 YAML-only, webhook-only model. It is accurate for that compatibility path but does not describe the default production configuration.
- Windows Task Scheduler examples in `README.md` are illustrative only; Raven (Linux/headless) is the production target.
- `config/hunts.yaml` is not a live configuration file in production.
