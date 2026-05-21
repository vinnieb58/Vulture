# Vulture Project Status

## Current State
- **Version target:** Vulture v2.0
- **Last verified date:** 2026-03-14
- **Current hunt source mode:** `db`
- **Current scheduler/runtime path:** `main.py` reads active hunts from SQLite when `VULTURE_HUNT_SOURCE=db`

## Confirmed Working
- Discord bot startup is clean and explicit.
- `.env` is not tracked in git.
- Code reads `.env` via `load_dotenv()` and does not write back to it.
- `.env.example` exists with placeholder values only.
- Slash commands sync successfully to the configured guild.
- All 6 hunt lifecycle slash commands are implemented and wired end to end:
  - `/hunt_list`
  - `/hunt_show`
  - `/hunt_create`
  - `/hunt_pause`
  - `/hunt_resume`
  - `/hunt_end`
- Hunt creation from Discord persists to SQLite.
- Hunt lifecycle actions from Discord operate on the SQLite `hunts` table.
- The scheduler/runtime can read hunts from SQLite when `VULTURE_HUNT_SOURCE=db`.
- The listings pipeline is active and writes scraped results into the SQLite `listings` table.

## Current Implemented Architecture
### Discord command path
`discord_bot.py` → `engine/command_router.py` → `engine/hunt_service.py` → `engine/hunt_repository.py` → `data/vulture.db`

### Hunt storage
- **Source of truth for active 2.0 hunts:** SQLite `hunts` table
- **Current Discord-created hunt persistence:** SQLite only
- **YAML touched during Discord hunt creation:** No

### Listing storage
- **Listing persistence/dedupe:** SQLite `listings` table

## Database Reality
### `hunts` table
Stores hunt definitions and lifecycle state, including fields such as:
- `hunt_id`
- `name`
- `category`
- `source_sites` (JSON)
- `search_terms` (JSON)
- `include_keywords` (JSON)
- `exclude_keywords` (JSON)
- `max_price`
- `location`
- `radius`
- `status`
- `created_by`
- `created_at`
- `updated_at`
- `notes`
- `adapter_options` (JSON)

**Verified state:** 3 real rows created through Discord slash commands.

### `listings` table
Stores scraped listing results, including fields such as:
- `id`
- `source`
- `title`
- `price`
- `location`
- `link` (unique)
- `first_seen`

**Verified state:** 22 rows from actual Craigslist execution runs.

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

## Confirmed Hunt Lifecycle Behavior
- `/hunt_create` inserts into SQLite `hunts`
- `/hunt_list` reads from SQLite `hunts`
- `/hunt_show` reads from SQLite `hunts`
- `/hunt_pause` updates hunt status in SQLite
- `/hunt_resume` updates hunt status in SQLite
- `/hunt_end` updates hunt status in SQLite

## Known Issues / Observations
- One live hunt entry has `source_sites: ["craiglist"]` instead of `"craigslist"`.
- Lowercasing exists in runtime normalization, but typo correction does not.
- That hunt would be skipped as unsupported if active.
- Hunt specificity still needs improvement in the future LLM translation layer.
  - Example observed issue: a 75-inch 4K TV hunt returned a mix of relevant and unrelated results.
- Source selection should eventually be grouped by vertical/type rather than a flat website list.
  - Examples: cars sites, computer-parts sites, appliance sites.

## Current Gap
The Discord → SQLite → scheduler pipeline is implemented.

The main end-to-end item still worth explicitly verifying is:
- create/manage a hunt via Discord
- run `main.py` with `VULTURE_HUNT_SOURCE=db`
- confirm the active hunt executes
- confirm a Discord alert is delivered for new matching listings

## Recommended Next Step
Run the full live-path verification for DB-backed hunts:
1. Keep `VULTURE_HUNT_SOURCE=db`
2. Ensure there is one valid active hunt in SQLite
3. Run `python main.py`
4. Confirm it reads the active DB hunt
5. Confirm new listings are persisted
6. Confirm Discord alert delivery occurs

## Stopping Point
We now have a code-grounded understanding that hunt management is already DB-backed through Discord slash commands, and the next step is full end-to-end runtime confirmation rather than rebuilding hunt persistence.
