# Finch Grocery Assistant

_Last updated: 2026-06-11_

Finch is a small Aviary module that turns a plain-English grocery list into preferred Kroger product matches. It starts with **dry-run preview** and **live product search** — no checkout, no payment automation.

## Operator flow (recommended)

Build your preferred Kroger item map **before** touching OAuth or cart writing.

### Step 1 — Preview (no Kroger auth)

Dry-run against your local alias database:

```bash
python -m finch.preview "eggs, milk, coffee pods"
python -m finch.preview "2 eggs, flank steak" --json
```

This shows what Finch would pick today from your alias map.

### Step 2 — Check setup

```bash
python -m finch.setup
```

Verifies `.env` without printing secrets:

- `FINCH_KROGER_CLIENT_ID` — required for live search
- `FINCH_KROGER_CLIENT_SECRET` — required for live search
- `FINCH_KROGER_LOCATION_ID` — required for store prices
- `FINCH_KROGER_REDIRECT_URI` — optional now; needed later for cart add
- `FINCH_LIVE_CART` — should be **off** while building aliases

### Step 3 — Live search and pick preferred products

```bash
python -m finch.search "eggs"
python -m finch.search "coffee pods"
python -m finch.search "eggs" --json
```

Example output:

```text
Search: "eggs" at location 01400441
Found 2 result(s):

  [1] Kroger Grade A Large Eggs 12 ct
      brand: Kroger | size: 12 ct | UPC: 0001111081708 | product_id: 001 | price: $2.99

  [2] Simple Truth Organic Eggs 12 ct
      brand: Simple Truth | size: 12 ct | UPC: 0001111081709 | product_id: 002 | price: $4.29
```

Save your preferred match as an alias (confirmation required):

```bash
# Non-interactive (scripted)
python -m finch.search "eggs" --save-alias eggs --pick 1 --confirm

# Interactive — prompts to pick number and confirm
python -m finch.search "eggs" --save-alias eggs
```

Re-run preview to confirm the alias map:

```bash
python -m finch.preview "eggs"
# status should be exact_default with your chosen UPC
```

### Step 4 — Later: OAuth and cart add

Only after your alias map is solid:

1. Complete Kroger OAuth authorization code flow (browser login).
2. Set `FINCH_KROGER_USER_ACCESS_TOKEN` (or a future refresh-token helper).
3. Set `FINCH_LIVE_CART=true` to allow guarded cart add.
4. Finish checkout manually in the Kroger app — Finch never pays or places orders.

---

## Purpose

1. Accept a messy grocery list (comma-separated, multiline, bullets, quantities).
2. Map common terms to your preferred Kroger products via a local alias store.
3. Search Kroger live to find and pin the right products.
4. Eventually add matched items to your Kroger cart for manual review.

## Boundaries

| Allowed | Not allowed |
|---------|-------------|
| Dry-run preview | Automated checkout |
| Live product search (client credentials) | Storing secrets in git |
| Alias mapping (SQLite + YAML seed) | Logging tokens or full auth headers |
| Guarded cart add (`FINCH_LIVE_CART=true`, later) | Payment or order placement |

## Quick start (preview only)

```bash
cd /home/vinnieb58/projects/vulture   # Raven project root
source .venv/bin/activate
pip install -r requirements.txt

python -m finch.setup
python -m finch.preview "eggs, milk, coffee pods"
```

### Preview status values

| Status | Meaning |
|--------|---------|
| `exact_default` | Alias matched and UPC/product ID is configured |
| `needs_search` | Alias matched but product not pinned — search Kroger |
| `ambiguous` | Multiple aliases partially match |
| `missing` | No alias — search Kroger by normalized name |

## Module layout

```text
finch/
  __init__.py
  config.py           # FINCH_* paths and flags (no secrets)
  env_check.py        # Setup validation (no secret values printed)
  env_util.py         # load .env for CLIs
  models.py           # GroceryIntent, PreviewLine, MatchStatus
  parser.py           # Messy text → normalized intents
  aliases.py          # SQLite store + YAML seed + upsert
  preview.py          # Dry-run CLI (python -m finch.preview)
  setup.py            # Setup helper (python -m finch.setup)
  search.py           # Live search + alias pinning (python -m finch.search)
  kroger_client.py    # OAuth + search + guarded cart add
  data/
    default_aliases.yaml
```

Alias data lives in `data/finch_aliases.db` (seeded from `finch/data/default_aliases.yaml` on first run).

## Environment variables

Add to repo-root `.env` (never commit `.env`):

| Variable | Step | Purpose |
|----------|------|---------|
| `FINCH_KROGER_CLIENT_ID` | 2–3 | Kroger developer app client ID |
| `FINCH_KROGER_CLIENT_SECRET` | 2–3 | Kroger developer app client secret |
| `FINCH_KROGER_LOCATION_ID` | 2–3 | Store location ID (prices/aisle) |
| `FINCH_KROGER_REDIRECT_URI` | 4 | OAuth callback (cart add only) |
| `FINCH_KROGER_USER_ACCESS_TOKEN` | 4 | User token after browser OAuth |
| `FINCH_LIVE_CART` | 4 | Set `true` to allow cart add (default off) |
| `FINCH_ALIASES_DB_PATH` | Optional | Override alias SQLite path |

See `.env.example` for template entries.

## Kroger API notes

**Product search** uses client credentials (`product.compact` scope) — no browser login required.

**Cart add** requires authorization code OAuth (`cart.basic:write`) — defer until Step 4.

Register at [Kroger Developer Portal](https://developer.kroger.com/).

## Security notes

- Secrets belong in `.env` only; `.env` is gitignored.
- `finch.setup` and `finch.search` never print client secrets or tokens.
- Cart add never triggers checkout — you finish in the Kroger app.
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
| `finch.setup` | Live (reads .env, no API call) |
| `finch.search` | Live when credentials configured |
| Kroger API in unit tests | Mocked |
| Cart add | Guarded — off until Step 4 |

## Related docs

- `docs/current/AVIARY_PROJECT_CONTEXT.md` — Aviary platform overview
- `docs/current/CODEBASE_STATUS.md` — repo entrypoints and test commands
