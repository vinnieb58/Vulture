# Finch Grocery Assistant

_Last updated: 2026-06-11_

Finch is a small Aviary module that turns a plain-English grocery list into preferred Kroger product matches. It starts with **dry-run preview** and **live product search** — no checkout, no payment automation.

## Operator flow (recommended)

Build your preferred Kroger store and item map **before** touching OAuth or cart writing.

### Step 1 — Add Kroger client ID and secret

Copy `.env.example` to `.env` and fill in:

```bash
FINCH_KROGER_CLIENT_ID=your_client_id
FINCH_KROGER_CLIENT_SECRET=your_client_secret
```

Register at [Kroger Developer Portal](https://developer.kroger.com/). You do **not** need to look up a location ID manually.

Verify with:

```bash
python -m finch.setup
```

### Step 2 — Find stores near your ZIP

```bash
python -m finch.locations 77406
python -m finch.locations --zip 77406
python -m finch.locations 77406 --json
```

Example output:

```text
Stores near ZIP 77406 (radius 20 mi)
Found 2 result(s):

  [1] Kroger
      address: 123 Main St
      city/state/zip: Richmond, TX 77406
      locationId: 01400441
      pickup (dept 94): yes
      phone: (281) 555-1234

  [2] Kroger Marketplace
      address: 456 FM 1092
      city/state/zip: Missouri City, TX 77459
      locationId: 01400442
      pickup (dept 94): no
```

### Step 3 — Save your preferred pickup store

```bash
python -m finch.locations 77406 --save --pick 1 --confirm
```

This writes `data/finch_config.json` (gitignored). Finch reads `.env` first, then falls back to this file — you never need to paste a location ID by hand.

Re-run setup to confirm:

```bash
python -m finch.setup
# FINCH_KROGER_LOCATION_ID: is set (data/finch_config.json)
```

### Step 4 — Live product search and pin aliases

```bash
python -m finch.search "eggs"
python -m finch.search "coffee pods"
python -m finch.search "eggs" --save-alias eggs --pick 1 --confirm
```

Preview your map anytime (no auth):

```bash
python -m finch.preview "eggs, milk, coffee pods"
```

### Step 5 — Later: OAuth and cart add

Only after your store and alias map are solid:

1. Complete Kroger OAuth authorization code flow (browser login).
2. Set `FINCH_KROGER_USER_ACCESS_TOKEN` (or a future refresh-token helper).
3. Set `FINCH_LIVE_CART=true` to allow guarded cart add.
4. Finish checkout manually in the Kroger app — Finch never pays or places orders.

---

## Boundaries

| Allowed | Not allowed |
|---------|-------------|
| Dry-run preview | Automated checkout |
| Store finder by ZIP | Storing secrets in git |
| Live product search (client credentials) | Logging tokens or full auth headers |
| Alias mapping (SQLite + YAML seed) | Payment or order placement |
| Guarded cart add (`FINCH_LIVE_CART=true`, later) | |

## Module layout

```text
finch/
  __init__.py
  config.py           # FINCH_* paths and flags (no secrets)
  local_config.py     # data/finch_config.json (saved store location)
  env_check.py        # Setup validation (no secret values printed)
  env_util.py         # load .env for CLIs
  models.py
  parser.py
  aliases.py
  preview.py          # python -m finch.preview
  setup.py            # python -m finch.setup
  locations.py        # python -m finch.locations
  search.py           # python -m finch.search
  kroger_client.py
  data/
    default_aliases.yaml
data/
  finch_config.json   # saved store (gitignored)
  finch_aliases.db    # alias map (gitignored)
```

## Environment variables

Add to repo-root `.env` (never commit `.env`):

| Variable | Step | Purpose |
|----------|------|---------|
| `FINCH_KROGER_CLIENT_ID` | 1 | Kroger developer app client ID |
| `FINCH_KROGER_CLIENT_SECRET` | 1 | Kroger developer app client secret |
| `FINCH_KROGER_LOCATION_ID` | Optional | Override saved store from `finch_config.json` |
| `FINCH_KROGER_REDIRECT_URI` | 5 | OAuth callback (cart add only) |
| `FINCH_KROGER_USER_ACCESS_TOKEN` | 5 | User token after browser OAuth |
| `FINCH_LIVE_CART` | 5 | Set `true` to allow cart add (default off) |

**Location ID:** use `python -m finch.locations <zip> --save` instead of setting manually.

## Security notes

- Secrets belong in `.env` only; `.env` is gitignored.
- `finch.setup`, `finch.locations`, and `finch.search` never print client secrets or tokens.
- Saved store info in `data/finch_config.json` is non-secret and gitignored.
- Keep `FINCH_LIVE_CART` off while building your alias map.

## Testing

```bash
pytest tests/test_finch.py -v
```

All Kroger HTTP calls are mocked in tests — no live network required.

## What is mocked vs live

| Component | Default |
|-----------|---------|
| Parser, aliases, preview | Live (local) |
| `finch.setup` | Live (reads .env + finch_config.json) |
| `finch.locations` | Live when credentials configured |
| `finch.search` | Live when credentials + store configured |
| Kroger API in unit tests | Mocked |
| Cart add | Guarded — off until Step 5 |

## Related docs

- `docs/current/AVIARY_PROJECT_CONTEXT.md` — Aviary platform overview
- `docs/current/CODEBASE_STATUS.md` — repo entrypoints and test commands
