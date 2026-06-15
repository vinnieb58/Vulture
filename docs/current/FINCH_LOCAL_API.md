# Finch Local API (v0.2)

_Last updated: 2026-06-13_

Finch v0.2 adds a **local-only HTTP API** on Raven so future integrations (WhatsApp, Nest, Crow) can call Finch without shell access. The API reuses existing Finch core logic — parser, alias resolution, guarded cart add, and activity history. It **never checks out, pays, or submits orders**.

## Design

- **Localhost only** — bind to `127.0.0.1` (not `0.0.0.0`). Reachable from other machines only via Tailscale or SSH port-forward on Raven.
- **API key auth** — every mutating/read endpoint except health requires `X-Finch-Key: <FINCH_API_KEY>`.
- **Cart guardrails unchanged** — `FINCH_LIVE_CART=false` (default) blocks cart mutation at the API layer, same as the CLI.
- **No secrets in responses** — tokens and client secrets are never returned or logged.

```text
┌─────────────┐     localhost      ┌──────────────┐     HTTPS      ┌─────────┐
│  WhatsApp   │ ── webhook ──►   │  Finch API   │ ────────────►  │ Kroger  │
│  (future)   │   (future)       │  :8091       │   cart add     │  API    │
└─────────────┘                  └──────────────┘                └─────────┘
                                        │
                                        ▼
                                 finch_aliases.db
                                 finch_activity.db
```

### Future WhatsApp flow

1. User sends a grocery list to a WhatsApp bot on Raven.
2. WhatsApp webhook handler (not yet implemented) POSTs to `http://127.0.0.1:8091/finch/preview` with the list text.
3. Finch resolves aliases and returns a preview JSON payload.
4. After user confirmation, webhook POSTs to `/finch/cart/add-list` (requires `FINCH_LIVE_CART=true` and saved OAuth token).
5. Finch adds items to the Kroger cart; webhook replies with a summary and reminds the user to **review and checkout manually in the Kroger app**.

No checkout or payment automation is planned.

## Environment

Add to repo-root `.env`:

```bash
# Required to start the Finch local API (generate a long random string)
FINCH_API_KEY=your_local_api_key_here

# Existing Finch vars — see FINCH_GROCERY_ASSISTANT.md
FINCH_KROGER_CLIENT_ID=...
FINCH_KROGER_CLIENT_SECRET=...
FINCH_LIVE_CART=          # leave empty/false until ready for live cart writes
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `FINCH_API_KEY` | _(required)_ | Shared secret for `X-Finch-Key` header |
| `FINCH_API_HOST` | `127.0.0.1` | Bind address when using `python -m finch.api` |
| `FINCH_API_PORT` | `8091` | Listen port |
| `FINCH_API_TEST_MODE` | off | Set `1` in tests only — allows startup without `FINCH_API_KEY` |
| `FINCH_LIVE_CART` | off | Must be `true` for cart add endpoints to mutate Kroger cart |

If `FINCH_API_KEY` is missing, the server **refuses to start** unless `FINCH_API_TEST_MODE=1`.

## Endpoints

All paths are under `/finch`. Protected routes require header `X-Finch-Key: <FINCH_API_KEY>`.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/finch/health` | no | Liveness check |
| `POST` | `/finch/preview` | yes | Dry-run alias resolution for grocery text |
| `POST` | `/finch/cart/add` | yes | Add one item (guarded by `FINCH_LIVE_CART`) |
| `POST` | `/finch/cart/add-list` | yes | Add multiple items (guarded) |
| `GET` | `/finch/cart/history` | yes | Recent cart activity log |
| `GET` | `/finch/cart/current` | yes | Read live Kroger cart (when API access allows) |

### Cart read note

Kroger **Public** API access currently exposes only `PUT /v1/cart/add` (`cart.basic:write`). Finch calls `GET /v1/cart` with the saved user OAuth token; when Kroger returns 403/404/405, the endpoint responds with `"supported": false` and a short explanation instead of failing the request.

Reading the live cart requires **Partner** Cart API access (`GET /v1/cart` or `GET /v1/carts`) and the `cart.basic:read` OAuth scope. After Kroger grants read access, re-run `python -m finch.auth` so Finch requests the expanded scope. No new env vars are required beyond existing Kroger OAuth settings.

### Request / response shapes

**POST /finch/preview**

```json
{ "text": "eggs, milk, coffee pods" }
```

```json
{
  "lines": [
    {
      "requested_item": "eggs",
      "normalized_name": "eggs",
      "matched_alias": "Kroger Eggs",
      "upc": "0001111081708",
      "quantity": 1,
      "status": "exact_default"
    }
  ]
}
```

**POST /finch/cart/add**

```json
{ "item": "2 eggs", "quantity": null }
```

**POST /finch/cart/add-list**

```json
{ "text": "eggs, milk" }
```

**GET /finch/cart/history?limit=50**

```json
{
  "entries": [
    {
      "id": 1,
      "timestamp": "2026-06-13T12:00:00+00:00",
      "requested_text": "eggs",
      "action": "cart_add",
      "result": "ok (ok)"
    }
  ]
}
```

**GET /finch/cart/current**

When Kroger cart read is unavailable (typical on Public API access):

```json
{
  "supported": false,
  "message": "Kroger cart read is not available with Public API access. ...",
  "items": [],
  "subtotal": null
}
```

When supported:

```json
{
  "supported": true,
  "items": [
    {
      "name": "Kroger Large Eggs",
      "quantity": 2,
      "upc": "0001111081708",
      "price": "$2.99",
      "line_total": "$5.98"
    }
  ],
  "subtotal": "$5.98"
}
```

Cart add endpoints return `403` when `FINCH_LIVE_CART` is not enabled.

## Run on Raven

Set `FINCH_API_KEY` in `.env` before starting (server will exit on startup if missing).

### Production (systemd)

Reference unit file: `deploy/systemd/finch-api.service`.

The service runs as user `vinnieb58`, loads `/home/vinnieb58/projects/vulture/.env`, and binds **localhost only** (`127.0.0.1:8091`).

One-time install from the repo root on Raven:

```bash
sudo cp deploy/systemd/finch-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now finch-api.service
```

After code or `.env` changes:

```bash
sudo systemctl restart finch-api.service
```

**Systemd smoke commands:**

```bash
systemctl status finch-api --no-pager -l
curl http://127.0.0.1:8091/finch/health
journalctl -u finch-api -n 80 --no-pager
```

`scripts/update_raven_quick.sh` copies all unit files from `deploy/systemd/` on deploy, but you still need `enable --now` once for a new unit.

### Manual smoke (foreground)

Start the server (localhost only):

```bash
uvicorn finch.api:app --host 127.0.0.1 --port 8091
```

Or:

```bash
python -m finch.api
```

### API smoke commands

Replace `YOUR_KEY` with the value of `FINCH_API_KEY`.

**Health (no auth):**

```bash
curl -s http://127.0.0.1:8091/finch/health | jq .
```

**Preview:**

```bash
curl -s -X POST http://127.0.0.1:8091/finch/preview \
  -H "Content-Type: application/json" \
  -H "X-Finch-Key: YOUR_KEY" \
  -d '{"text": "eggs, milk"}' | jq .
```

**Cart add blocked (`FINCH_LIVE_CART` unset or false):**

```bash
curl -s -X POST http://127.0.0.1:8091/finch/cart/add \
  -H "Content-Type: application/json" \
  -H "X-Finch-Key: YOUR_KEY" \
  -d '{"item": "eggs"}' | jq .
```

Expected: HTTP 403 with detail mentioning `FINCH_LIVE_CART`.

**Cart history:**

```bash
curl -s http://127.0.0.1:8091/finch/cart/history \
  -H "X-Finch-Key: YOUR_KEY" | jq .
```

**Current Kroger cart:**

```bash
curl -s http://127.0.0.1:8091/finch/cart/current \
  -H "X-Finch-Key: YOUR_KEY" | jq .
```

## Tests

```bash
pip install fastapi uvicorn httpx
pytest tests/test_finch_api.py -v
```

## Related docs

- [FINCH_GROCERY_ASSISTANT.md](./FINCH_GROCERY_ASSISTANT.md) — CLI operator flow, OAuth, alias pinning
