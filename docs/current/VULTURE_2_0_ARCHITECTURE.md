# Vulture 2.0+ Architecture (Live)

Last refreshed: 2026-05-28 (UTC)

## Core architecture

```text
Discord operator
  -> discord_bot.py (slash commands)
  -> engine.command_router.dispatch()
  -> engine.hunt_service (+ engine.llm_translator for intent flows)
  -> engine.hunt_repository (SQLite hunts table)

Scheduled/manual cycle
  -> main.py
  -> load hunts (yaml | db | mixed)
  -> fan out source_sites per hunt
  -> adapters.registry.get_adapter(source)
  -> adapter returns Listing objects
  -> engine.rules.rejection_reason()
  -> engine.database.save_listing() (link dedupe)
  -> engine.notifier.send_discord_alert() for new listings
```

## Key design rule (implemented)

- Translation can be LLM/rules-assisted for hunt creation.
- Runtime listing acceptance is deterministic only (rules engine).

## Hunt source modes in live code

- `yaml`: `engine.hunts.load_hunts()`
- `db`: `engine.hunt_service.list_hunts(status="active")`
- `mixed`: merged YAML + DB; YAML wins on name collision

## Multi-source execution behavior

- DB hunts can carry multiple `source_sites`
- `main._expand_hunt_sources()` creates one execution unit per source
- Source failures are isolated by try/except around each run

## Persistence model

- SQLite file: `data/vulture.db`
- `hunts` table: structured hunt state with JSON-encoded list/dict fields
- `listings` table: normalized listing rows; uniqueness on `link`

## Adapter architecture in current code

- Central dispatch and capability metadata is already implemented in `adapters/registry.py`
- Runtime-registered sources: `craigslist`, `offerup`, `carsdotcom`
- Capability metadata includes fields such as:
  - `stable`, `experimental`
  - `requires_browser`, `requires_login`
  - `supports_location`, `location_control`
  - `verticals`

## Translator and rules split

- `engine.llm_translator.translate()` is the public translator entry
- Vehicle intents route to `engine.intent_translator_v2.translate_v2()`
- Non-vehicle intents use preserved v1 deterministic builder path
- `engine.hunt_service.hunt_to_execution_dict()` forwards structured constraints to runtime rules
- `engine.rules` performs deterministic checks for price, keyword rules, and structured constraints (TV size, GPU tier, RAM capacity/speed, vehicle year/miles)
