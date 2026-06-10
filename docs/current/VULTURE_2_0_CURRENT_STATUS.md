# Vulture 2.0+ Current Status

Last refreshed: 2026-06-10 (UTC)

**Scope:** Vulture deal-hunting service only. Platform context: [AVIARY_PROJECT_CONTEXT.md](AVIARY_PROJECT_CONTEXT.md).

## Executive summary

Vulture is a Discord-controlled, DB-backed hunt system with deterministic execution in `main.py`. Production on Raven uses `VULTURE_HUNT_SOURCE=db`, systemd scheduling, and adapter registry dispatch with multi-source fan-out.

## Baseline reality (from live code)

### Hunt sources

| Mode | Use |
|------|-----|
| `db` | **Production** — SQLite hunts from Discord |
| `yaml` | Legacy/dev — `config/hunts.yaml` |
| `mixed` | Both; YAML wins on name collision |

### Registered adapters

| Source | Classification |
|--------|----------------|
| `craigslist` | stable |
| `mercari`, `microcenter` | beta |
| `offerup`, `carsdotcom`, `swappa`, `bestbuy`, `newegg` | experimental |

Probe-only: eBay (`experiments/adapters/`), others without `_REGISTRY` entry.

## Command / runtime surfaces

- Discord hunt commands: `/hunt_list`, `/hunt_show`, `/hunt_create`, `/hunt_pause`, `/hunt_resume`, `/hunt_end`, `/hunt`, `/hunt_from_intent`
- Hunt statuses: `active`, `paused`, `ended` (terminal)
- Runtime: load hunts → fan-out `source_sites` → adapter → rules → dedupe → alert

## Constraints

- OfferUp: GeoIP location; city arg advisory
- Cars.com / Best Buy / Micro Center: Playwright on Raven; may return `[]` on blocks
- OpenAI translator backend not implemented; deterministic/rules path active
- Runtime filtering conservative on missing title structure

## Source of truth

1. Repository code
2. `docs/current/CODEBASE_STATUS.md`
3. `docs/current/AVIARY_PROJECT_CONTEXT.md`
4. This file (Vulture-scoped summary)

## Next boundary

1. Repeatable adapter smoke checks with logged evidence.
2. Evidence-based adapter promotion only.
3. Vertical translation quality (vehicles, GPUs, TVs) without architecture changes.
