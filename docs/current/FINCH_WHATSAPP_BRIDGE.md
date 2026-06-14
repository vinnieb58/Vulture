# Finch WhatsApp Bridge (v0.3)

_Last updated: 2026-06-14_

Finch v0.3 adds a **thin WhatsApp Cloud API webhook** on Raven. It receives messages from whitelisted phone numbers, calls the local Finch API on `127.0.0.1:8091`, and sends text replies on WhatsApp. It does **not** duplicate grocery logic, checkout, payment, or broad AI interpretation.

```text
WhatsApp user
     │
     ▼
Meta Cloud API webhook
     │
     ▼
finch-whatsapp.service (:8092)
     │  X-Finch-Key
     ▼
finch-api.service (:8091)
     │
     ▼
Finch core (preview, guarded cart, history)
     │
     ▼
WhatsApp reply via Graph API
```

## Design

- **Bridge only** — command parsing and HTTP glue live in `finch_whatsapp/`; alias resolution and cart guardrails stay in `finch/`.
- **Explicit commands** — only `help`, `preview …`, `add …`, `add-list …`, and `history` are handled. Random chat is not treated as a grocery list.
- **Whitelist** — only numbers in `FINCH_WHATSAPP_ALLOWED_NUMBERS` are processed.
- **Cart guard unchanged** — `FINCH_LIVE_CART=false` (default) blocks cart writes at the Finch API; the bridge surfaces a simple “cart writes disabled” reply.
- **No secrets in logs** — tokens, API keys, and full inbound payloads are never logged.

## Meta setup

Configure these fields in the [Meta Developer Console](https://developers.facebook.com/) for your WhatsApp Business app:

| Field | Env var | Notes |
|-------|---------|-------|
| Callback URL | _(deploy URL)_ | Public HTTPS URL pointing at `https://<host>/webhook` (Tailscale funnel, reverse proxy, etc.) |
| Verify token | `FINCH_WHATSAPP_VERIFY_TOKEN` | Must match GET `/webhook` verification |
| Phone number ID | `FINCH_WHATSAPP_PHONE_NUMBER_ID` | From WhatsApp > API Setup |
| Access token | `FINCH_WHATSAPP_ACCESS_TOKEN` | System user or long-lived token with `whatsapp_business_messaging` |
| Allowed numbers | `FINCH_WHATSAPP_ALLOWED_NUMBERS` | Comma-separated E.164 digits without `+` |

Subscribe the webhook to **messages** (and optionally **message_echoes** if needed later). Status-only notifications are ignored.

## Environment

Add to repo-root `.env` (see `.env.example`):

```bash
# Finch local API (already required for finch-api.service)
FINCH_API_KEY=your_local_api_key_here
FINCH_API_BASE_URL=http://127.0.0.1:8091

# WhatsApp bridge
FINCH_WHATSAPP_VERIFY_TOKEN=your_meta_verify_token
FINCH_WHATSAPP_ACCESS_TOKEN=your_meta_access_token
FINCH_WHATSAPP_PHONE_NUMBER_ID=123456789012345
FINCH_WHATSAPP_ALLOWED_NUMBERS=15551234567,15557654321

# Optional bind overrides
FINCH_WHATSAPP_HOST=127.0.0.1
FINCH_WHATSAPP_PORT=8092
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `FINCH_WHATSAPP_VERIFY_TOKEN` | _(required)_ | Meta webhook verification token |
| `FINCH_WHATSAPP_ACCESS_TOKEN` | _(required)_ | Meta Graph API bearer token for outbound messages |
| `FINCH_WHATSAPP_PHONE_NUMBER_ID` | _(required)_ | WhatsApp phone number ID for Graph API sends |
| `FINCH_WHATSAPP_ALLOWED_NUMBERS` | _(required)_ | Comma-separated sender whitelist |
| `FINCH_API_BASE_URL` | `http://127.0.0.1:8091` | Local Finch API base URL |
| `FINCH_API_KEY` | _(required)_ | Sent as `X-Finch-Key` to Finch API |
| `FINCH_WHATSAPP_HOST` | `127.0.0.1` | Bind address |
| `FINCH_WHATSAPP_PORT` | `8092` | Listen port |
| `FINCH_WHATSAPP_TEST_MODE` | off | Set `1` in tests only |

If required vars are missing, the service **refuses to start** unless `FINCH_WHATSAPP_TEST_MODE=1`.

## Commands

Send these as plain WhatsApp text messages from a whitelisted number:

| Command | Example | Finch API call |
|---------|---------|----------------|
| `help` | `help` | _(none)_ |
| `preview` | `preview eggs, milk` | `POST /finch/preview` |
| `add` | `add eggs` | `POST /finch/cart/add` |
| `add-list` | `add-list eggs, milk` | `POST /finch/cart/add-list` |
| `history` | `history` | `GET /finch/cart/history?limit=10` |

### Reply shapes

- **preview** — lists matched aliases and missing items
- **add / add-list** — lists added and skipped items; blocked cart returns “Cart writes are currently disabled.”
- **history** — recent Finch cart activity entries
- **unknown text** — “Unknown command. Send 'help' for available commands.”

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/webhook` | Meta verification (`hub.mode`, `hub.verify_token`, `hub.challenge`) |
| `POST` | `/webhook` | Inbound WhatsApp message webhook |
| `GET` | `/health` | Liveness check |

## systemd on Raven

Install after `finch-api.service`:

```bash
sudo cp deploy/systemd/finch-whatsapp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now finch-whatsapp.service
sudo systemctl status finch-whatsapp.service
```

The unit binds **localhost only** on port **8092**. Expose `/webhook` to Meta via a controlled reverse proxy or Tailscale funnel — do not bind `0.0.0.0` on the public internet.

Manual run (development):

```bash
python -m finch_whatsapp.app
# or
uvicorn finch_whatsapp.app:app --host 127.0.0.1 --port 8092
```

## Local smoke tests

With both services running locally and `.env` configured:

```bash
# Health
curl -s http://127.0.0.1:8092/health | jq

# Meta verification (simulate Meta GET)
curl -s "http://127.0.0.1:8092/webhook?hub.mode=subscribe&hub.verify_token=$FINCH_WHATSAPP_VERIFY_TOKEN&hub.challenge=smoke123"
# expect: smoke123

# Simulate inbound preview webhook (whitelisted sender)
curl -s -X POST http://127.0.0.1:8092/webhook \
  -H 'Content-Type: application/json' \
  -d '{
    "object": "whatsapp_business_account",
    "entry": [{
      "changes": [{
        "value": {
          "messages": [{
            "from": "15551234567",
            "type": "text",
            "text": {"body": "preview eggs, milk"}
          }]
        }
      }]
    }]
  }'
```

Then confirm a WhatsApp reply arrives on the test handset. For cart commands with `FINCH_LIVE_CART=false`, expect the disabled-cart message.

Run unit tests:

```bash
pytest tests/test_finch_whatsapp.py -v
pytest tests/test_systemd_units.py -v -k whatsapp
```

## Related docs

- [FINCH_LOCAL_API.md](./FINCH_LOCAL_API.md) — Finch HTTP API v0.2
- [FINCH_GROCERY_ASSISTANT.md](./FINCH_GROCERY_ASSISTANT.md) — CLI, OAuth, aliases, cart guardrails
