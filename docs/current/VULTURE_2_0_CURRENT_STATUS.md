# Vulture 2.0+ Current Status

Last refreshed: 2026-05-28 (UTC)

## Executive summary

Vulture is currently a Discord-controlled, DB-backed hunt system with deterministic execution in `main.py`.  
The live implementation includes adapter registry dispatch, multi-source fan-out, SQLite hunts/listings persistence, deterministic rules, and translator routing (vehicle intents -> v2 deterministic pipeline).

## Baseline reality (from live code)

- Stable runtime adapter: `craigslist`
- Experimental runtime adapters: `offerup`, `carsdotcom`
- Probe-only sources: eBay, Micro Center (plus additional recon scripts)
- Hunt sources supported by runtime:
  - `yaml` (legacy)
  - `db` (active DB hunts)
  - `mixed` (YAML + DB, YAML name collisions win)

## Confirmed implemented command/runtime surfaces

- Discord commands: `/hunt_list`, `/hunt_show`, `/hunt_create`, `/hunt_pause`, `/hunt_resume`, `/hunt_end`, `/hunt`, `/hunt_from_intent`
- Hunt lifecycle statuses: `active`, `paused`, `ended` (terminal)
- Runtime loop:
  - load hunts -> expand by source_sites -> adapter call -> rules -> dedupe/save -> alert

## Current constraints and caveats

- OfferUp location control is GeoIP-driven; city parameter is advisory
- Cars.com adapter requires Playwright/Chromium and is explicitly marked experimental
- OpenAI translator backend is not implemented; deterministic rules backend is active
- Runtime filtering is intentionally conservative on ambiguous/missing title data

## Current source of truth

1. Repository code
2. `docs/current/CODEBASE_STATUS.md`
3. `docs/current/SESSION_LOG.md`

For historical context only, older planning docs may describe now-completed future work.

## Immediate next boundary

Recommended next technical boundary:

1. Add repeatable adapter health smoke checks (Craigslist/OfferUp/Cars.com).
2. Keep adapter status labels evidence-based (do not promote experimental adapters without repeated successful runtime evidence).
