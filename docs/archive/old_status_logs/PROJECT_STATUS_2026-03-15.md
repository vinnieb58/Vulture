# Vulture Project Status

## Current State
- **Version target:** Vulture v2.0
- **Last verified date:** 2026-03-15
- **Current hunt source mode:** `db`
- **Current scheduler/runtime path:** `main.py` reads active hunts from SQLite when `VULTURE_HUNT_SOURCE=db`

## Confirmed Working
- Discord bot startup is clean and explicit.
- `.env` is not tracked in git.
- Code reads `.env` via `load_dotenv()` and does not write back to it.
- `.env.example` exists with placeholder values only.
- Slash commands sync successfully to the configured guild.
- Discord hunt lifecycle commands are implemented and wired end to end through:
  - `discord_bot.py`
  - `engine/command_router.py`
  - `engine/hunt_service.py`
  - `engine/hunt_repository.py`
  - `data/vulture.db`
- Hunt creation from Discord persists to SQLite.
- Hunt lifecycle actions from Discord operate on the SQLite `hunts` table.
- The scheduler/runtime can read hunts from SQLite when `VULTURE_HUNT_SOURCE=db`.
- The listings pipeline is active and writes scraped results into the SQLite `listings` table.
- A project tracking workflow now exists using:
  - `PROJECT_STATUS.md`
  - `SESSION_LOG.md`

## Current Implemented Architecture
### Discord command path
`discord_bot.py` → `engine/command_router.py` → `engine/hunt_service.py` → `engine/hunt_repository.py` → `data/vulture.db`

### Hunt storage
- **Source of truth for active 2.0 hunts:** SQLite `hunts` table
- **Current Discord-created hunt persistence:** SQLite only
- **YAML touched during Discord hunt creation:** No

### Listing storage
- **Listing persistence/dedupe:** SQLite `listings` table

## Hunt Source Modes
### `VULTURE_HUNT_SOURCE=yaml`
- Reads `config/hunts.yaml` only
- Legacy/v1.0 path

### `VULTURE_HUNT_SOURCE=db`
- Reads active hunts from SQLite `hunts` table
- This is the current working mode

### `VULTURE_HUNT_SOURCE=mixed`
- Reads both YAML and DB hunts
- YAML wins on name collision

## Confirmed v2.0 Progress Since Last Status Update
- Added a rules-based hunt translation layer for Discord intent-based hunt creation.
- Translator now extracts and stores structured constraints such as:
  - `max_price`
  - `max_miles`
  - `min_capacity_gb`
  - TV size / resolution hints
  - RAM type hints
- Translator improvements now include:
  - shorthand numeric parsing like `10k` → `10000`
  - conservative Craigslist-safe location validation
  - better vehicle vertical detection
  - improved vehicle make/model extraction, including alphanumeric models like `rav4`, `4runner`, and `f-150`
  - RAM-specific search/exclude logic
  - stronger vehicle exclude terms for parts, wheels/tires, and collectibles/memorabilia
- Rejected invalid Craigslist locations are now surfaced back to the user in Discord instead of only appearing in logs.
- Runtime rules now enforce selected stored structured constraints:
  - `min_price`
  - `max_miles` (title-based, conservative parsing)
  - `min_capacity_gb` (title-based, conservative parsing)
- Live tests confirmed:
  - TV hunt filtering improved
  - DDR4/DDR5 RAM hunts filter junk better
  - Porsche intent no longer degrades into malformed general search text
  - vehicle model parsing is more specific than earlier passes

## Current Known Limitations / Observations
- Constraint enforcement is still conservative and title-based for Craigslist.
  - If mileage or RAM capacity is not clearly present in the title, the listing is allowed through.
- `max_miles` is stored and partially enforced, but Craigslist body text is not currently parsed during rule checks.
- `min_capacity_gb` is stored and partially enforced, but ambiguous RAM titles are allowed through.
- TV-specific structured constraints are improved at translation time, but runtime enforcement can still be tightened further.
- Vehicle filtering still depends heavily on title conventions and exclude lists.
- The translator and filters are getting more vertical-specific; this is expected and should continue as a modular pattern rather than one giant generic rules blob.

## Current Recommended Next Step
Continue turning stored structured constraints into runtime-enforced filters, starting with the next highest-value vertical-specific cases after vehicles and RAM.

Good next candidates:
1. tighten TV scan-time enforcement for:
   - screen size
   - 4K / UHD keywords
2. continue refining vehicle filtering where Craigslist title patterns still allow false positives
3. keep old test hunts cleaned out so fresh translator behavior is what gets evaluated

## Stopping Point
We now have:
- DB-backed Discord hunt control
- a functioning rules-based translator
- visible location rejection feedback in Discord
- runtime enforcement for mileage and RAM capacity using conservative title parsing

The next phase is incremental tightening of vertical-specific runtime filtering rather than rebuilding the core pipeline.
