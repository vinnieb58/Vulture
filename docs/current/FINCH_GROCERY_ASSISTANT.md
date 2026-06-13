# Finch Grocery Assistant (v0.1)

_Last updated: 2026-06-11_

Finch is a small Aviary module that turns plain-English grocery lists into preferred Kroger cart items. It supports dry-run preview, live search, alias pinning, guarded cart add, and a local activity log — **no checkout or payment automation**.

## Operator flow

```text
setup → locations → search → alias → auth → FINCH_LIVE_CART=true → cart add → review in Kroger app
```

### Step 1 — Add Kroger client ID and secret

```bash
FINCH_KROGER_CLIENT_ID=your_client_id
FINCH_KROGER_CLIENT_SECRET=your_client_secret
FINCH_KROGER_REDIRECT_URI=http://localhost:8765/callback
python -m finch.setup
```

### Step 2 — Find and save your store

```bash
python -m finch.locations 77406 --save --pick 1 --confirm
```

### Step 3 — Search and pin preferred products

```bash
python -m finch.search "eggs" --save-alias eggs --pick 1 --confirm
python -m finch.preview "eggs, milk"
```

### Step 4 — OAuth (one-time browser login)

```bash
python -m finch.auth
```

Tokens save to `data/finch_tokens.json` (gitignored, mode 600).

### Step 5 — Enable cart add

```bash
# .env
FINCH_LIVE_CART=true
```

```bash
python -m finch.cart test
python -m finch.cart add eggs
python -m finch.cart add "2 eggs"
python -m finch.cart add-list "eggs, milk, coffee pods"
python -m finch.cart history
```

Review and checkout manually in the Kroger app.

---

## Cart commands

| Command | Description |
|---------|-------------|
| `cart add eggs` | Add one alias-resolved item (qty 1) |
| `cart add "2 eggs"` | Quantity parsed from text |
| `cart add eggs --quantity 3` | Override quantity |
| `cart add-list "eggs, milk"` | Add multiple items |
| `cart test` | Smoke test (validation or one safe add) |
| `cart history` | Finch-added items from activity log |

Cart add requires `FINCH_LIVE_CART=true`, saved OAuth token, and alias with UPC.

## Activity log

Finch records cart operations locally in `data/finch_activity.db` (gitignored):

- timestamp
- requested text
- resolved alias
- UPC / product_id
- quantity
- action (`cart_add`, `cart_add_list`, `cart_test`)
- result

View with `python -m finch.cart history`. Secrets are never logged.

## Boundaries

| Allowed | Not allowed |
|---------|-------------|
| Guarded cart add | Automated checkout |
| Local activity log | Storing tokens in git |
| List + quantity parsing | Payment or order placement |
| Token refresh | Printing tokens to console |

`FINCH_LIVE_CART` defaults to **off**.

## Future: WhatsApp channel (not in v0.1)

Planned architecture for a later release:

```text
WhatsApp message
    → WhatsApp webhook receiver (LAN/Tailscale)
    → Finch local API (parse list, resolve aliases)
    → Kroger cart add (guarded, FINCH_LIVE_CART=true)
    → WhatsApp reply (summary of what was added / skipped)
```

Principles for WhatsApp integration:

- Webhook runs on Raven behind Tailscale; no public internet exposure required
- Same alias map, tokens, and guardrails as CLI
- Reply only — user still checks out in Kroger app
- No WhatsApp checkout or payment automation

## Module layout

```text
finch/
  activity.py         # data/finch_activity.db
  auth.py
  cart_ops.py
  cart/__main__.py
  token_store.py      # data/finch_tokens.json
  local_config.py     # data/finch_config.json
  env_util.py         # .env loading (FINCH_SKIP_DOTENV for tests)
  ...
data/
  finch_activity.db   # cart log (gitignored)
  finch_tokens.json   # OAuth tokens (gitignored)
  finch_config.json   # saved store (gitignored)
  finch_aliases.db    # alias map (gitignored)
```

## Testing

```bash
pytest tests/test_finch.py -v
```

Tests set `FINCH_SKIP_DOTENV=1` automatically so repo `.env` on Raven does not contaminate missing-credential tests.

## Related docs

- `docs/current/AVIARY_PROJECT_CONTEXT.md`
- `docs/current/CODEBASE_STATUS.md`
