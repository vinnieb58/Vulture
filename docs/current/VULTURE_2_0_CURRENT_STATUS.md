# Vulture 2.0 Current Status

_Last refreshed: 2026-05-21_

## Executive summary

Vulture should now be treated as a **Vulture 2.0** project, not a v1.0/YAML prototype.

The current working system is a Python-based, Discord-controlled deal-hunting engine. Hunts are created and managed from Discord, stored in SQLite, translated from user intent into structured deterministic rules, executed by `main.py`, filtered by the rules engine, deduplicated by listing link, persisted to SQLite, and alerted back to Discord.

The current project is no longer blocked by core architecture. The next major work is **adapter expansion**: preparing the codebase to support additional websites cleanly, then adding one new source at a time.

## Current baseline version

**Current baseline:** Vulture 2.0  
**Current runtime host:** Raven, the Ubuntu Server Chromebox  
**Primary development machine:** Windows PC using Cursor  
**Runtime execution:** Headless server over SSH / tmux  
**Current main branch should be treated as:** the active Vulture 2.0 baseline once verified and cleaned up

## What is confirmed working

| Area | Status | Notes |
|---|---|---|
| Craigslist adapter | Working | Current stable production adapter |
| Listing normalization | Working | Listings normalize into the shared model |
| SQLite listing persistence | Working | Listings are stored locally |
| Link-based dedupe | Working | Existing listing links are skipped |
| Discord alerts | Working | Alerts are sent for new matching listings |
| Discord bot startup | Working | Bot has successfully started and synced commands |
| Discord slash commands | Working | Hunt lifecycle commands are wired |
| DB-backed hunt storage | Working | Discord-created hunts persist to SQLite |
| Hunt lifecycle | Working | Create/list/show/pause/resume/end exist |
| `VULTURE_HUNT_SOURCE=db` | Working | Current intended hunt source mode |
| Scheduler path | Working conceptually | `main.py` is the repeated hunt-cycle entry point |
| Logging | Working | Logs expose runtime and filtering behavior |
| Secret hygiene | Working | `.env` is not committed and should not be modified by code |
| Rules-based translation | Working | User intent becomes structured hunt data |
| Deterministic runtime filtering | Working | Runtime listing decisions are not LLM-based |

## Current implemented architecture

### Discord command path

```text
discord_bot.py
  -> engine/command_router.py
  -> engine/hunt_service.py
  -> engine/hunt_repository.py
  -> SQLite hunts table
```

### Hunt execution path

```text
main.py
  -> load active hunts from SQLite when VULTURE_HUNT_SOURCE=db
  -> dispatch to site adapter
  -> normalize listings
  -> apply deterministic rules
  -> dedupe by link
  -> save new listings
  -> send Discord alerts
  -> log cycle summary
```

## Current source of truth

The active truth for Vulture is now:

1. the live repository code,
2. `PROJECT_STATUS.md`,
3. `SESSION_LOG.md`,
4. current docs describing Vulture 2.0 behavior.

Older documents that describe YAML as the primary hunt source should be considered **historical only**.

## Current hunt source modes

| Mode | Meaning | Status |
|---|---|---|
| `yaml` | Reads `config/hunts.yaml` | Legacy v1.0 path |
| `db` | Reads active hunts from SQLite | Current Vulture 2.0 path |
| `mixed` | Reads YAML and DB hunts | Compatibility path, not preferred |

The preferred operating mode is:

```env
VULTURE_HUNT_SOURCE=db
```

## Current vertical-specific intelligence

Vulture has moved into a title-intelligence refinement phase. The system uses translation and deterministic rules to improve hunt quality without allowing the LLM to decide runtime listing matches.

| Vertical | Current status |
|---|---|
| GPUs | Improved NVIDIA/AMD model handling and laptop/system exclusions |
| RAM | Capacity and speed parsing added in conservative title-based form |
| TVs | Improved 4K/UHD and screen-size intent handling |
| Vehicles | Make/model parsing improved; typo and parts filtering still need refinement |
| General marketplace | Supported but can be noisy |

## Current known limitations

- Craigslist is still the only stable production adapter.
- Runtime filtering is mostly title-based because Craigslist search results expose limited data.
- Missing or ambiguous title data is usually allowed through rather than guessed.
- Vehicle hunts can still be polluted by parts listings unless excludes catch them.
- Broad/base-model hunts are naturally noisier than specific hunts.
- eBay and Micro Center previously showed anti-bot / blocking friction.
- Facebook Marketplace should be treated as experimental due to brittleness, login/session complexity, and policy risk.

## Current priority

The next phase is **adapter expansion foundation**.

Immediate priorities:

1. Clean and replace stale GPT Project source files.
2. Treat the current runtime as Vulture 2.0.
3. Add an adapter registry so `main.py` does not grow source-specific dispatch logic.
4. Add source capability metadata.
5. Preserve Craigslist behavior exactly.
6. Add the next website only after the registry foundation is in place.
7. Choose a lower-risk second adapter before attempting Facebook Marketplace.

## Recommended next branch

```bash
git checkout -b feature/adapter-expansion-foundation
```

## Recommended next commit boundary

First commit should be small:

```text
Add adapter registry foundation
```

That commit should only:

- add adapter registry module,
- register Craigslist,
- move existing Craigslist dispatch through the registry,
- add minimal source capability metadata,
- preserve all existing behavior,
- avoid changing `.env`, Discord behavior, or the database schema.
