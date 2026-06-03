# Vulture Programming Reference (Current)

Last updated: 2026-05-28 (UTC)

## 1) Runtime roles

- `discord_bot.py`: command/control runtime (Vulture hunt slash commands + Crow v0.1 read-only ops)
- `crow/`: read-only Raven host and Vulture health checks (`docs/CROW_V0_1.md`)
- `main.py`: one hunt cycle executor (load hunts -> run adapters -> rules -> dedupe -> save -> alert)

Keep these roles distinct when debugging.

## 2) Current command surface

Implemented slash commands:

- `/hunt_list`, `/hunt_show`, `/hunt_create`
- `/hunt_pause`, `/hunt_resume`, `/hunt_end`
- `/hunt` (preferred intent create), `/hunt_from_intent` (legacy alias)

Crow v0.1 (same bot runtime, read-only):

- `/raven_status`, `/check_disk`, `/check_memory`, `/check_services`, `/check_vulture`, `/crow_help`

## 3) Hunt source loading modes

Configured through `VULTURE_HUNT_SOURCE`:

- `yaml` -> `config/hunts.yaml`
- `db` -> active hunts in SQLite
- `mixed` -> YAML + DB (YAML name collisions win)

## 4) Current adapter status

- Stable: `craigslist`
- Experimental runtime adapters: `offerup`, `carsdotcom`
- Probe-only (not runtime adapters): eBay, Micro Center
- Deferred/absent: Mercari

## 5) Deterministic runtime rule

- Translator can help build hunts.
- Runtime listing pass/fail decisions come from `engine/rules.py` only.
- No LLM listing-level acceptance at runtime.

## 6) Persistence model (SQLite)

- DB file: `data/vulture.db`
- `hunts` table: lifecycle + structured JSON fields
- `listings` table: normalized listing rows with unique `link` dedupe key

## 7) Important current caveats

- OfferUp city parameter is advisory; result geography is GeoIP-driven.
- Cars.com adapter depends on Playwright and remains experimental.
- OpenAI translator backend is a stub and intentionally raises an error.

## 8) Quick verification commands

### Windows (PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pytest
python main.py
# optional if token is configured:
python discord_bot.py
```

### Raven (Ubuntu)

```bash
ssh <user>@<raven-host>
git pull origin main
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pytest
python main.py
tail -n 200 logs/vulture.log
tmux ls
```

## 9) Guardrails for changes

Do not:

- edit/commit `.env`
- label experimental adapters as stable without evidence
- bypass deterministic rules with LLM runtime decisions
- couple adapter internals to direct DB writes or Discord sends

Do:

- keep docs synced with live code
- keep adapter capability metadata honest
- keep changes incremental and test-backed
