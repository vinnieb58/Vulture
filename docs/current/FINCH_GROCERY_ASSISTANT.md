# Finch Grocery Assistant

_Last updated: 2026-06-11_

Finch is a small Aviary module that turns a plain-English grocery list into preferred Kroger product matches. It supports dry-run preview, live search, alias pinning, and **guarded cart add** — no checkout or payment automation.

## Operator flow (recommended)

```text
setup → locations → search → alias → auth → FINCH_LIVE_CART=true → cart add → review in Kroger app
```

### Step 1 — Add Kroger client ID and secret

Copy `.env.example` to `.env`:

```bash
FINCH_KROGER_CLIENT_ID=your_client_id
FINCH_KROGER_CLIENT_SECRET=your_client_secret
FINCH_KROGER_REDIRECT_URI=http://localhost:8765/callback
```

Register at [Kroger Developer Portal](https://developer.kroger.com/). Set the same redirect URI in your Kroger app settings.

```bash
python -m finch.setup
```

### Step 2 — Find and save your store

```bash
python -m finch.locations 77406
python -m finch.locations 77406 --save --pick 1 --confirm
```

Saves to `data/finch_config.json` (gitignored). No manual location ID lookup.

### Step 3 — Search and pin preferred products

```bash
python -m finch.search "eggs"
python -m finch.search "eggs" --save-alias eggs --pick 1 --confirm
python -m finch.preview "eggs, milk"
```

### Step 4 — OAuth (one-time browser login)

```bash
python -m finch.auth
```

1. Open the printed authorize URL in your browser.
2. Sign in to Kroger and approve access.
3. Paste the authorization code from the redirect URL.

Tokens save to `data/finch_tokens.json` (gitignored, mode 600). Finch never prints access tokens, refresh tokens, or auth headers.

### Step 5 — Enable cart add and smoke test

Add to `.env`:

```bash
FINCH_LIVE_CART=true
```

```bash
python -m finch.cart test
python -m finch.cart add eggs
python -m finch.cart add "coffee pods"
```

Example cart output:

```text
Cart add attempt:
  requested: 'eggs'
  normalized: 'eggs'
  alias: 'Kroger Grade A Large Eggs 12 ct'
  upc: 0001111081708
  quantity: 1
  modality: pickup
  result: ok (ok)

Review and checkout manually in the Kroger app.
```

### Step 6 — Review in Kroger app

Finch adds items to your cart only. **You** complete checkout and payment in the Kroger app or website.

---

## Boundaries

| Allowed | Not allowed |
|---------|-------------|
| Dry-run preview | Automated checkout |
| Store finder by ZIP | Storing secrets in git |
| Live product search | Logging tokens or auth headers |
| Guarded cart add (`FINCH_LIVE_CART=true`) | Payment or order placement |
| OAuth token save (`data/finch_tokens.json`) | Printing tokens to console |

## Commands

| Command | Purpose |
|---------|---------|
| `python -m finch.setup` | Check configuration |
| `python -m finch.locations 77406 --save --pick 1 --confirm` | Save store |
| `python -m finch.search "eggs" --save-alias eggs --pick 1 --confirm` | Pin product |
| `python -m finch.preview "eggs, milk"` | Dry-run alias map |
| `python -m finch.auth` | Browser OAuth + token save |
| `python -m finch.cart test` | Smoke test (validation or one safe add) |
| `python -m finch.cart add eggs` | Add one alias-resolved item |

## Module layout

```text
finch/
  auth.py             # python -m finch.auth
  cart_ops.py         # alias resolution + cart guards
  cart/__main__.py    # python -m finch.cart
  token_store.py      # data/finch_tokens.json
  local_config.py     # data/finch_config.json
  ...
data/
  finch_config.json   # saved store (gitignored)
  finch_tokens.json   # OAuth tokens (gitignored, chmod 600)
  finch_aliases.db    # alias map (gitignored)
```

## Environment variables

| Variable | When | Purpose |
|----------|------|---------|
| `FINCH_KROGER_CLIENT_ID` | Step 1 | Kroger app client ID |
| `FINCH_KROGER_CLIENT_SECRET` | Step 1 | Kroger app client secret |
| `FINCH_KROGER_REDIRECT_URI` | Step 4 | OAuth callback URL |
| `FINCH_LIVE_CART` | Step 5 | Must be `true` for cart add |
| `FINCH_KROGER_USER_ACCESS_TOKEN` | Optional | Override saved tokens (debug only) |

Location ID: use `finch.locations --save`, not manual `.env` entry.

## Token refresh

If Kroger returns a `refresh_token`, Finch saves it in `finch_tokens.json`. Before cart add, Finch refreshes expired access tokens automatically. If refresh fails, run `python -m finch.auth` again.

## Security notes

- Client secret stays in `.env` only.
- User tokens stay in `data/finch_tokens.json` only (mode 600).
- No command prints secrets, tokens, customer IDs, or full Authorization headers.
- Keep `FINCH_LIVE_CART=false` until aliases and store are verified.

## Testing

```bash
pytest tests/test_finch.py -v
```

All Kroger HTTP and OAuth flows are mocked in tests.

## Related docs

- `docs/current/AVIARY_PROJECT_CONTEXT.md`
- `docs/current/CODEBASE_STATUS.md`
