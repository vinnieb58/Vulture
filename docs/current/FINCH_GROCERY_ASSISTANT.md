# Finch Grocery Assistant

_Last updated: 2026-06-11_

Finch is a small Aviary module that turns a plain-English grocery list into preferred Kroger product matches. It starts with **dry-run preview only** — no checkout, no payment automation.

## Purpose

1. Accept a messy grocery list (comma-separated, multiline, bullets, quantities).
2. Map common terms to your preferred Kroger products via a local alias store.
3. Flag unknown items for Kroger product search.
4. Eventually add matched items to your Kroger cart for manual review in the Kroger app.

## Boundaries

| Allowed | Not allowed |
|---------|-------------|
| Dry-run preview | Automated checkout |
| Alias mapping (SQLite + YAML seed) | Storing secrets in git |
| Kroger product search (when configured) | Logging tokens, refresh tokens, customer IDs, or full auth headers |
| Guarded cart add (`FINCH_LIVE_CART=true`) | Payment or order placement |

## Quick start (no live Kroger auth required)

```bash
cd /home/vinnieb58/projects/vulture   # Raven project root
source .venv/bin/activate
pip install -r requirements.txt

# Dry-run preview
python -m finch.preview "eggs, milk, coffee pods"

# Multiline / messy input
python -m finch.preview -f my_list.txt

# JSON output
python -m finch.preview "2 eggs, flank steak" --json
```

Example human-readable output:

```text
requested: 'eggs' | status: exact_default | qty: 1 | alias: 'Kroger Grade A Large Eggs 12 ct' | upc: 0001111081708 | search: 'kroger large eggs 12'
requested: 'milk' | status: exact_default | qty: 1 | alias: 'Kroger Vitamin D Whole Milk 1 gal' | upc: 0001111050350
requested: 'coffee pods' | status: needs_search | qty: 1 | alias: 'Kroger Breakfast Blend K-Cup Pods 12 ct' | search: 'kroger breakfast blend k-cup'
```

### Preview status values

| Status | Meaning |
|--------|---------|
| `exact_default` | Alias matched and UPC/product ID is configured |
| `needs_search` | Alias matched but product not pinned — would search Kroger |
| `ambiguous` | Multiple aliases partially match |
| `missing` | No alias — would search Kroger by normalized name |

## Module layout

```text
finch/
  __init__.py
  config.py           # FINCH_* paths and flags (no secrets)
  models.py           # GroceryIntent, PreviewLine, MatchStatus
  parser.py           # Messy text → normalized intents
  aliases.py          # SQLite store + YAML seed import
  preview.py          # Dry-run CLI (python -m finch.preview)
  kroger_client.py    # OAuth + search + guarded cart add
  data/
    default_aliases.yaml
```

Alias data lives in `data/finch_aliases.db` (created on first run from `finch/data/default_aliases.yaml`).

## Customizing aliases

Edit `finch/data/default_aliases.yaml`, then delete `data/finch_aliases.db` or re-seed:

```python
from finch.aliases import seed_aliases_from_yaml
seed_aliases_from_yaml(overwrite=True)
```

Fields per alias:

- `alias_key` — normalized lookup term (e.g. `eggs`)
- `display_name` — your preferred product label
- `upc` / `kroger_product_id` — pin a product for `exact_default`
- `search_term` — Kroger search fallback when UPC is unknown

## Environment variables

Add to repo-root `.env` (never commit `.env`):

| Variable | Required | Purpose |
|----------|----------|---------|
| `FINCH_KROGER_CLIENT_ID` | For live API | Kroger developer app client ID |
| `FINCH_KROGER_CLIENT_SECRET` | For live API | Kroger developer app client secret |
| `FINCH_KROGER_LOCATION_ID` | For priced search | Store location ID (prices/aisle) |
| `FINCH_KROGER_REDIRECT_URI` | For cart add | OAuth callback URL (authorization code flow) |
| `FINCH_KROGER_USER_ACCESS_TOKEN` | For cart add | Short-lived user token after OAuth (prefer refresh flow later) |
| `FINCH_LIVE_CART` | For cart add | Set `true` to allow `add_to_cart` (still no checkout) |
| `FINCH_KROGER_CART_MODALITY` | Optional | `pickup` (default), `delivery`, or `ship` |
| `FINCH_ALIASES_DB_PATH` | Optional | Override alias SQLite path |
| `FINCH_DATA_DIR` | Optional | Data directory (default: `./data`) |

See `.env.example` for template entries.

## Kroger API setup

### 1. Register an application

1. Go to [Kroger Developer Portal](https://developer.kroger.com/).
2. Create an application and note **Client ID** and **Client Secret**.
3. Set a redirect URI (e.g. `http://localhost:8765/callback` for a local OAuth helper).

### 2. Product search (client credentials)

Product search uses the **client credentials** grant with `product.compact` scope:

```bash
curl -X POST 'https://api.kroger.com/v1/connect/oauth2/token' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -H 'Authorization: Basic BASE64(CLIENT_ID:CLIENT_SECRET)' \
  -d 'grant_type=client_credentials&scope=product.compact'
```

Finch wraps this in `KrogerClient.search_products()`.

### 3. Cart add (authorization code — browser required)

Adding to a customer's cart requires:

- OAuth **authorization code** grant (user signs in via browser)
- Scope: `cart.basic:write` (plus `product.compact` for search)
- User access token on each cart request

Finch exposes `build_authorize_url()` and `exchange_authorization_code()` but does **not** ship a full OAuth callback server in v0.1. Recommended next step: a small local callback helper or Crow command that stores the refresh token outside git.

Cart add is **disabled by default**. Set `FINCH_LIVE_CART=true` only after reviewing dry-run output.

```python
from finch.kroger_client import load_kroger_client_from_env

client = load_kroger_client_from_env()
client.add_to_cart("0001111081708", quantity=1, live=True)  # guarded
```

## Security notes

- Secrets belong in `.env` only; `.env` is gitignored.
- Finch does not log access tokens, refresh tokens, customer IDs, or full `Authorization` headers.
- Cart add never triggers checkout or payment — you finish in the Kroger app.
- Start with `python -m finch.preview` before enabling `FINCH_LIVE_CART`.

## Testing

```bash
pytest tests/test_finch.py -v
```

Tests use:

- Temporary SQLite databases for aliases
- Fake HTTP sessions for Kroger API (no live network calls)

## What is mocked vs live

| Component | v0.1 default |
|-----------|--------------|
| Grocery parser | Live (local) |
| Alias store | Live (local SQLite) |
| Preview CLI | Live (local) |
| Kroger product search | Mocked in tests; live when env + network configured |
| Kroger cart add | Stub/guarded — raises unless `FINCH_LIVE_CART=true` + user token |
| OAuth browser flow | Documented only — not automated in this PR |

## Known blockers before live cart add

1. **Kroger developer account** — you need approved API credentials.
2. **Authorization code flow** — cart add requires user login via browser; Finch v0.1 documents the path but does not run the callback server.
3. **Location ID** — product prices and fulfillment need `FINCH_KROGER_LOCATION_ID`.
4. **UPC verification** — default YAML UPCs are starting points; confirm against your store's catalog via search.
5. **Refresh token storage** — v0.1 accepts `FINCH_KROGER_USER_ACCESS_TOKEN`; production should persist refresh tokens securely outside git.

## Next steps

1. Confirm alias UPCs via live product search.
2. Add a minimal OAuth callback script (localhost) to obtain user tokens.
3. Wire preview `needs_search` / `missing` items to `KrogerClient.search_products()`.
4. Optional Crow slash command: `/finch preview ...` on Raven.
5. Optional Docker service if Finch needs scheduled or remote access.

## Related docs

- `docs/current/AVIARY_PROJECT_CONTEXT.md` — Aviary platform overview
- `docs/current/CODEBASE_STATUS.md` — repo entrypoints and test commands
