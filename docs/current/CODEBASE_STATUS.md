# Vulture Codebase Status (Current)

Last refreshed: 2026-05-28 (UTC)

## Git snapshot

- Branch at inspection start: `main`
- Working tree at inspection start: clean (`git status -sb` showed no tracked or untracked changes)
- Head commit: `8e82b42` — vertical-aware hunt process updates (TV size, GPU tier, RAM ddr generation/card-only, or-better flow)
- Recent commits also include:
  - `9db0196` intent translator v2 vehicle mileage/price fixes + tests
  - `bdb82bf` Cars.com probe + experimental adapter work
  - `b5c5be6` eBay reconnaissance conclusion (Browse API recommended)

## What is implemented now

### Runtime entrypoints

- `main.py`: scheduled/one-shot hunt cycle runner
- `discord_bot.py`: Discord slash-command control surface

### Core implemented modules

- `engine/database.py`: SQLite listing storage + link dedupe
- `engine/hunt_repository.py`: SQLite hunts table CRUD/update/list
- `engine/hunt_service.py`: hunt business rules + status transitions + runtime execution dict conversion
- `engine/command_router.py`: command dispatch (`list/show/create/pause/resume/end/create_from_intent`)
- `engine/llm_translator.py`: public translator entry; routes vehicles to v2 deterministic pipeline
- `engine/intent_translator_v2.py`: deterministic v2 vehicle-first translation pipeline
- `engine/rules.py`: deterministic listing filter + structured title parsing
- `adapters/registry.py`: source registry and capability metadata
- `adapters/craigslist.py`: stable requests/bs4 adapter
- `adapters/offerup.py`: production-usable requests + `__NEXT_DATA__` parser (GeoIP-only location)
- `adapters/mercari.py`: production-usable requests GraphQL search (canonical `/us/item/` URLs)
- `adapters/carsdotcom.py`: production-usable Playwright adapter (vehicles; residential IP + Chromium on Raven)

## Current runtime flow

```text
main.py
  -> init_db() + init_hunts_table()
  -> resolve hunt source via VULTURE_HUNT_SOURCE (yaml | db | mixed)
  -> load hunts
      yaml: engine.hunts.load_hunts()
      db:   engine.hunt_service.list_hunts(status="active") + hunt_to_execution_dict()
      mixed: merge yaml + db by hunt name (YAML wins on duplicates)
  -> expand multi-source hunts (source_sites -> one run per source)
  -> adapter dispatch via adapters.registry.get_adapter(source)
  -> listing loop:
       rules.rejection_reason(listing, rules)
       database.save_listing() (dedupe by unique link)
       notifier.send_discord_alert() for new listings
  -> per-hunt and cycle summary logs
```

## Current Discord command flow

```text
discord_bot.py slash command
  -> engine.command_router.dispatch(command, args)
  -> engine.hunt_service (or translator path for intent)
  -> engine.hunt_repository (SQLite hunts table)
  -> CommandResult message returned to Discord (ephemeral)
```

Implemented slash commands:
- `/hunt_list`
- `/hunt_show`
- `/hunt_create`
- `/hunt_pause`
- `/hunt_resume`
- `/hunt_end`
- `/hunt` (preferred NL create command)
- `/hunt_from_intent` (legacy alias)

## Current hunt execution flow

```text
Hunt row (DB) or YAML hunt
  -> execution dict
  -> optional source fan-out (one run per source in source_sites)
  -> adapter query(city, limit, query)
  -> Listing objects
  -> deterministic rule evaluation
  -> link dedupe in SQLite
  -> Discord webhook alert for new rows
```

## Deployment model

Vulture is a **personal/self-hosted** system for a single operator (e.g. Raven), not a multi-tenant SaaS. Adapters that work in live testing participate in normal `/hunt` source selection by vertical. Registry metadata stays honest about caveats (`geoip_only`, `requires_browser`, etc.) without blocking runtime.

## Supported adapters and status

### Registered in live runtime (`adapters/registry.py`)

| Source | Classification | Status notes |
|---|---|---|
| `craigslist` | **stable** | Primary adapter; requests + bs4; location via CL subdomain |
| `offerup` | **beta** | Production-usable on residential IP; `geoip_only` location (city arg advisory) |
| `mercari` | **beta** | Production-usable search + relevance filter; listing URLs use `/us/item/{id}/` |
| `carsdotcom` | **beta** | Production-usable on Raven with Playwright + Chromium; vehicles only; zip targeting; **flaky/browser-sensitive** (Cloudflare HTTP/2 blocks possible — adapter returns `[]`, does not crash hunt cycle) |

### Probe/experiment-only (not registered runtime adapters)

| Source / area | Classification | Evidence |
|---|---|---|
| eBay (`experiments/adapters/ebay_*`) | **probe only** | Probes document repeated 403/network-layer blocking; no production adapter file |
| Micro Center (`experiments/adapters/microcenter_probe.py`) | **probe only** | Probe script exists, no runtime adapter |
| Cars.com request/playwright probes | **probe only + informs experimental adapter** | Recon scripts exist; production adapter present but still marked experimental |
| OfferUp location probe | **probe only + informs experimental adapter** | Probe confirms GeoIP-only location behavior |
| Craigslist probe script | **probe only** | Runtime adapter exists separately and is stable |
| Mercari probe (`experiments/adapters/mercari_probe.py`) | **probe only** | Informs `adapters/mercari.py` runtime adapter |

## Database and hunt model behavior

- SQLite DB path: `data/vulture.db`
- `listings` table:
  - unique key behavior is enforced via `link TEXT NOT NULL UNIQUE`
  - `save_listing()` checks existing link before insert
- `hunts` table stores structured JSON fields as text:
  - `source_sites`, `search_terms`, `include_keywords`, `exclude_keywords`, `adapter_options`
- Hunt lifecycle statuses are enforced in service layer:
  - valid: `active`, `paused`, `ended`
  - `ended` is terminal (cannot resume/edit)
- `hunt_to_execution_dict()` builds rules + runtime fields and forwards structured adapter options (e.g. `max_miles`, `min_year`, `min_gpu_class`, `min_size_inches`)

## Rules/filtering behavior (current)

- Deterministic filtering only (no LLM at runtime)
- Core checks:
  - `min_price` / `max_price`
  - keyword include/exclude (`include_keywords`, `require_all_keywords`, `exclude_keywords`)
  - structured title-derived checks:
    - TV size min/max inches
    - vehicle mileage and year range
    - RAM min capacity + speed
    - GPU VRAM minimum (explicit vram intent)
    - GPU tier floor (`min_gpu_class` via `engine.verticals.GPU_TIER_RANK`)
- Conservative pass-through when required structured value cannot be extracted from title

## Current test status

Current test suite in `tests/`:
- `tests/test_intent_translator_v2.py`
- `tests/test_translator_non_vehicle_regression.py`
- `tests/test_verticals.py`
- `tests/test_mercari_adapter.py`
- `tests/test_source_selection.py`
- `tests/test_handheld_hunt.py`
- `tests/test_carsdotcom_adapter.py`

Test execution result is documented in the session entry for 2026-05-28 in `docs/current/SESSION_LOG.md`.

## Known gaps / TODOs

- No production adapter for eBay/Micro Center
- OfferUp location targeting is not controllable by requested city (GeoIP-driven)
- Cars.com requires Playwright/Chromium on the hunt host (Raven); intermittent Cloudflare/`ERR_HTTP2_PROTOCOL_ERROR` blocks — treat as flaky; failures return empty results
- OpenAI translator backend is stubbed (`_translate_openai` raises `TranslationError`)
- Test coverage is translator/rules focused; there are no adapter integration tests against live sites in CI here
- Docs were stale before this refresh and must continue to be maintained from code truth

## Recommended next branch / next commit boundary

- Next branch target: `cursor/adapter-status-hardening-67ca`
- Suggested next commit boundary:
  1. Add adapter health-smoke script(s) for Craigslist/OfferUp/Cars.com with clear pass/fail output only.
  2. Add documentation-only matrix updates from smoke outputs (do not promote experimental adapters to stable without evidence).

## Repository map (important files)

```text
.
├── main.py                         # Hunt-cycle runtime (DB/YAML/mixed source loading, fan-out, adapter run)
├── discord_bot.py                  # Discord slash command runtime
├── adapters/
│   ├── registry.py                 # Adapter lookup + capability metadata
│   ├── craigslist.py               # Stable Craigslist adapter
│   ├── offerup.py                  # Experimental OfferUp adapter
│   └── carsdotcom.py               # Experimental Cars.com adapter (Playwright)
├── engine/
│   ├── command_router.py           # Dispatches slash-command actions
│   ├── hunt_service.py             # Hunt business logic + execution dict conversion
│   ├── hunt_repository.py          # SQLite hunts persistence
│   ├── database.py                 # SQLite listings persistence + dedupe
│   ├── llm_translator.py           # Public translator (rules backend active)
│   ├── intent_translator_v2.py     # Deterministic v2 translation pipeline
│   ├── rules.py                    # Deterministic runtime filter engine
│   ├── notifier.py                 # Discord webhook notifications
│   └── hunts.py                    # Legacy YAML hunt loader
├── models/
│   ├── hunt.py                     # Hunt dataclass schema
│   └── listing.py                  # Listing dataclass schema
├── config/hunts.yaml               # Legacy/default YAML hunts; includes disabled OfferUp examples
├── experiments/adapters/           # Probe scripts (eBay/Cars.com/OfferUp/MicroCenter/etc.)
├── tests/                          # Translator + rules regression suites
├── docs/current/                   # Current-state project docs (this document lives here)
├── requirements.txt                # Python dependencies
├── data/vulture.db                 # Runtime SQLite database
└── logs/vulture.log                # Runtime log file
```

## Verification commands

### Windows (PowerShell)

```powershell
# from repo root
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt

# run tests
pytest

# run one hunt cycle (legacy default YAML unless env override set)
python main.py

# optional bot smoke test (safe if DISCORD_BOT_TOKEN is configured)
python discord_bot.py
```

### Raven (Ubuntu server)

```bash
# SSH reminder (replace host/user as needed)
ssh <user>@<raven-host>

# on Raven, from repo root
git pull origin main
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

# run tests
pytest

# run one hunt cycle
python main.py

# inspect logs
tail -n 200 logs/vulture.log

# check tmux sessions
tmux ls
```
