# Vulture Programming Reference (Current)

Last updated: 2026-06-10 (UTC)

Platform context: [AVIARY_PROJECT_CONTEXT.md](AVIARY_PROJECT_CONTEXT.md). This file is **Vulture-only**.

## 1) Runtime roles

- `discord_bot.py`: Vulture hunt slash commands + **Crow** ops (same process, `vulture-bot.service`)
- `main.py`: one hunt cycle (load hunts → adapters → rules → dedupe → save → alert)
- `crow/`: read-only Raven/Vulture health (`docs/CROW_V0_2.md`) — not hunt execution

Keep these roles distinct when debugging.

## 2) Command surface

**Vulture hunts:**

- `/hunt_list`, `/hunt_show`, `/hunt_create`
- `/hunt_pause`, `/hunt_resume`, `/hunt_end`
- `/hunt` (preferred intent create), `/hunt_from_intent` (legacy alias)

**Crow (read-only, same bot):**

- v0.1: `/raven_status`, `/check_disk`, `/check_memory`, `/check_services`, `/check_vulture`, `/crow_help`
- v0.2: `/check raven`, `/check services`, `/check storage`, `/check docker`, `/check tailscale`, `/check network`, `/check reboot`, `/check uptime`, `/check ports`, `/check logs`

## 3) Hunt source loading

`VULTURE_HUNT_SOURCE`:

| Value | Source |
|-------|--------|
| `db` | Active SQLite hunts — **Raven production** (`.env.example`) |
| `yaml` | `config/hunts.yaml` — legacy/dev |
| `mixed` | Both; YAML name collisions win |

Unset env → `main.py` defaults to `yaml` (dev fallback only).

## 4) Adapter status (registry)

| Classification | Sources |
|----------------|---------|
| stable | `craigslist` |
| beta | `mercari`, `microcenter` |
| experimental | `offerup`, `carsdotcom`, `swappa`, `bestbuy`, `newegg` |

Dispatch: `adapters.registry.get_adapter(source)`.

Probe-only: eBay (`experiments/adapters/`), etc.

## 5) Deterministic runtime rule

`engine/rules.rejection_reason()` — no LLM at scrape time.

## 6) Key files

| Area | Path |
|------|------|
| Registry | `adapters/registry.py` |
| Vertical sources | `engine/source_selection.py` |
| Hunt service | `engine/hunt_service.py` |
| Translator | `engine/llm_translator.py`, `engine/intent_translator_v2.py` |
| Rules | `engine/rules.py` |
| Scheduler (Raven) | `deploy/systemd/vulture-scheduler.timer` |

## 7) Tests

```bash
pytest tests
```
