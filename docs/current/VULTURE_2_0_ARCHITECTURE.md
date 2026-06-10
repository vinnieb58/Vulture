# Vulture 2.0+ Architecture (Live)

Last refreshed: 2026-06-10 (UTC)

**Scope:** Vulture service architecture. Aviary platform: [AVIARY_PROJECT_CONTEXT.md](AVIARY_PROJECT_CONTEXT.md).

## Core architecture

```text
Discord operator
  -> discord_bot.py (Vulture hunt slash commands; Crow ops on same bot)
  -> engine.command_router.dispatch()
  -> engine.hunt_service (+ engine.llm_translator for intent flows)
  -> engine.hunt_repository (SQLite hunts table)

vulture-scheduler.timer -> vulture-scheduler.service (oneshot)
  -> main.py
  -> load hunts (yaml | db | mixed) — production: db
  -> engine.source_selection / hunt source_sites fan-out
  -> adapters.registry.get_adapter(source)
  -> adapter returns Listing objects
  -> engine.rules.rejection_reason()
  -> engine.database.save_listing() (link dedupe)
  -> engine.notifier.send_discord_alert() for new listings
```

## Key design rule (implemented)

- Translation assists hunt **creation** (optional LLM/rules path).
- Runtime listing acceptance is **deterministic only** (`engine/rules.py`).

## Hunt source modes

| Mode | Loader |
|------|--------|
| `yaml` | `engine.hunts.load_hunts()` |
| `db` | `engine.hunt_service.list_hunts(status="active")` |
| `mixed` | Merged; YAML wins on name collision |

## Multi-source execution

- DB hunts carry `source_sites` (from translation / manual create).
- `main._expand_hunt_sources()` runs one execution unit per source.
- Per-source failures isolated (adapter returns `[]` or exception caught per hunt).

## Persistence

- SQLite: `data/vulture.db`
- `hunts` table: JSON-encoded list/dict fields
- `listings` table: unique `link` for dedupe

## Adapter architecture

Central registry: `adapters/registry.py`

Runtime-registered: `bestbuy`, `carsdotcom`, `craigslist`, `microcenter`, `newegg`, `offerup`, `mercari`, `swappa`

Capability metadata per source: `status`, `stable`/`experimental`, `requires_browser`, `location_control`, `verticals`, etc.

Vertical profiles: `engine/source_selection.py` maps hunt categories to default `source_sites`.

## Translator and rules split

- `engine.llm_translator.translate()` — public entry
- Vehicles → `engine.intent_translator_v2.translate_v2()`
- `engine.hunt_service.hunt_to_execution_dict()` — runtime field forwarding
- `engine.rules` — price, keywords, TV/GPU/RAM/vehicle structured checks

## Related Aviary components (not Vulture core loop)

| Component | Relationship |
|-----------|--------------|
| Crow | Same Discord bot process; read-only Raven/Vulture health |
| Canary | Independent Docker monitor; reads same host signals |
| Dashboard | Read-only UI for hunts table + adapter log heuristics |
| Roost | Host storage under `/mnt/storage/*` |
