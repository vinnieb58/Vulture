# eBay Browse API — Vulture Adapter Recon

_Last updated: 2026-05-27_

## Executive summary

| Question | Answer |
|----------|--------|
| **Feasibility for Vulture** | **Yes, with caveats.** Browse API is the correct replacement for eBay HTML scraping (all scraping paths failed from Raven; see `experiments/adapters/ebay_probe.py`). |
| **Technical fit** | JSON over HTTPS, `requests`-friendly, no browser, maps cleanly to `models.listing.Listing`. |
| **Main blockers** | Production Buy API access is **restricted** (developer account + approvals + Application Growth Check). Sandbox is available immediately for integration testing. |
| **Sold/completed listings** | **Not available** from Browse API (active listings only). |
| **Location/radius** | **Partial.** Item location is returned; true “radius” search is only for **local pickup** listings, not general shipped inventory like Craigslist geo search. |

**Verdict:** Build an **experimental** Browse API adapter after a sandbox probe validates field mapping and token flow. Do **not** register `ebay` in the stable registry until production credentials are approved and a real hunt cycle passes on Raven.

---

## Context: why API, not scraping

Prior recon (`experiments/adapters/ebay_probe.py`, `experiments/adapters/ebay_playwright_probe.py`) concluded:

- `requests`, `curl_cffi`, bare Playwright, and `playwright-stealth` all return HTTP 403 from the target environment.
- eBay blocks non-API access at the network/TLS layer.
- Browse API is the only practical path for stable, deterministic listing fetch.

Vulture’s adapter contract (see `docs/current/VULTURE_ADAPTER_IMPLEMENTATION_REFERENCE.md`):

```text
hunt -> adapter -> Listing(source, title, price, location, link) -> rules -> dedupe -> alert
```

---

## 1. Developer account requirements

### Minimum accounts

| Requirement | Purpose |
|-------------|---------|
| **eBay member account** (ebay.com) | Required to use **sandbox** (per [Buy APIs Requirements](https://developer.ebay.com/api-docs/buy/static/buy-requirements.html)). |
| **eBay Developers Program account** | Create keysets, OAuth credentials, manage compliance (marketplace account deletion notifications). |

Sign up: [Join the eBay Developers Program](https://developer.ebay.com/signin).

### Production access (restricted)

Browse API is part of the **Buy APIs**. Official policy:

- Sandbox: available to developers for integration testing.
- Production: **restricted** — eligibility, approvals, contracts, and **Application Growth Check** before the production keyset can call Buy APIs ([Browse overview](https://developer.ebay.com/api-docs/buy/browse/overview.html), [Buy requirements](https://developer.ebay.com/api-docs/buy/static/buy-requirements.html), [Buying app guide](https://developer.ebay.com/develop/get-started/get-started-on-a-buying-application)).

For a **search-only, read-only deal hunter** (no guest checkout, no cart, no bids):

- Business model still must be described honestly in the Application Growth Check (internal tooling / deal alerts is a valid framing).
- Full EPN “guest checkout” partner flow may **not** be required if checkout APIs are unused — but **production Browse access still requires the restricted-API approval path**, not merely creating a production keyset.
- Meeting eligibility is **not** a guarantee of approval.

### Compliance before production keyset works

Production keysets stay disabled until marketplace account deletion/closure notification compliance is completed ([Create keysets](https://developer.ebay.com/api-docs/static/gs_create-the-ebay-api-keysets.html)).

---

## 2. Required app credentials

Created on **Application Keys** in the developer portal (separate **Sandbox** and **Production** keysets):

| Credential | Where used |
|------------|------------|
| **App ID (Client ID)** | OAuth + API identity |
| **Cert ID (Client Secret)** | OAuth `Authorization: Basic` header |
| **RuName** | Not needed for client-credentials (application token) flow |

Each keyset has **OAuth scopes** assigned on the portal. Browse `search` requires an application access token minted with a scope your keyset includes — typically:

```text
https://api.ebay.com/oauth/api_scope
```

Confirm the exact scope string on your Application Keys page ([search method — OAuth scope](https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search)).

**Do not commit** Client ID or Client Secret. Store only in `.env` on Raven (see §10).

---

## 3. OAuth client-credentials flow

Browse `item_summary/search` uses an **Application access token** (no user login, no refresh token for end users).

### Token endpoints

| Environment | URL |
|-------------|-----|
| Sandbox | `POST https://api.sandbox.ebay.com/identity/v1/oauth2/token` |
| Production | `POST https://api.ebay.com/identity/v1/oauth2/token` |

### Request

```http
POST /identity/v1/oauth2/token HTTP/1.1
Host: api.ebay.com
Content-Type: application/x-www-form-urlencoded
Authorization: Basic BASE64(client_id:client_secret)

grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope
```

(`scope` must be URL-encoded; use the scope(s) shown for your keyset.)

### Response (typical)

```json
{
  "access_token": "v^1.1#i^1#...",
  "expires_in": 7200,
  "token_type": "Application Access Token"
}
```

- Token lifetime: **7200 seconds (2 hours)**.
- Adapter should **cache in memory** (and optionally on disk outside git) with expiry buffer (~5 minutes).
- On `401`, refresh token once and retry the Browse call.

References: [Client credentials grant](https://developer.ebay.com/api-docs/static/oauth-client-credentials-grant.html), [Authorization guide](https://developer.ebay.com/develop/guides-v2/authorization).

---

## 4. Browse API search endpoint

### Resource

| Method | Path |
|--------|------|
| `GET` | `/buy/browse/v1/item_summary/search` |

### Base URLs

| Environment | Base |
|-------------|------|
| Sandbox | `https://api.sandbox.ebay.com` |
| Production | `https://api.ebay.com` |

### Required headers

| Header | Value | Notes |
|--------|-------|-------|
| `Authorization` | `Bearer {access_token}` | From client-credentials flow |
| `X-EBAY-C-MARKETPLACE-ID` | e.g. `EBAY_US` | Site/marketplace for search ([marketplace support](https://developer.ebay.com/api-docs/buy/static/ref-marketplace-supported.html)) |
| `Accept` | `application/json` | |
| `X-EBAY-C-ENDUSERCTX` | Optional | `contextualLocation=country=US,zip=77002` (URL-encoded) — improves shipping/price sort accuracy; recommended for production-quality results |

### Search identity rule

At least one of: `q`, `category_ids`, `epid`, or `gtin` (error `12001` if missing).

### Pagination and limits

| Parameter | Constraint |
|-----------|------------|
| `limit` | 1–200 inclusive |
| `offset` | Non-negative integer |
| Result set cap | **10,000 items** max per logical search ([overview](https://developer.ebay.com/api-docs/buy/browse/overview.html)) |

### Default buying options

By default, only listings with **`FIXED_PRICE`** (Buy It Now) are returned. Auction-only listings need `filter=buyingOptions:{AUCTION}` ([search docs](https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search)).

For deal hunting, consider explicitly including both:

```text
filter=buyingOptions:{FIXED_PRICE|AUCTION}
```

---

## 5. Query parameters for Vulture hunt dimensions

### Keyword search

| Parameter | Example | Notes |
|-----------|---------|-------|
| `q` | `rtx+3080` | URL-encode query string |

### Max price (and min price)

Use the `filter` parameter ([field filters](https://developer.ebay.com/api-docs/buy/static/ref-buy-browse-filters.html)):

| Filter | Syntax | Example |
|--------|--------|---------|
| `price` | `[min..max]` or `[max]` | `price:[..300]` = max $300 |
| `priceCurrency` | ISO 4217 | **Required with price** — `priceCurrency:USD` |

Combined example:

```text
filter=price:[..300],priceCurrency:USD
```

Map from Vulture hunt `max_price` rule → upper bound in filter; lower bound optional.

### Condition

| Approach | Example |
|----------|---------|
| Named conditions | `filter=conditions:{USED}` or `{NEW\|USED}` |
| Condition IDs | `filter=conditionIds:{3000\|4000}` |

`condition` (human string) and `conditionId` are also returned per item ([ItemSummary](https://developer.ebay.com/api-docs/buy/browse/types/gct:ItemSummary)). Condition values vary by category — prefer `conditionId` for stable mapping.

Common IDs ([condition names doc](https://developer.ebay.com/api-docs/sell/static/metadata/condition-id-values.html)):

| ID | Name |
|----|------|
| 1000 | New |
| 3000 | Used |
| 4000 | Very Good |
| 5000 | Good |
| 6000 | Acceptable |

### Category (useful)

| Parameter | Example | Notes |
|-----------|---------|-------|
| `category_ids` | `category_ids=175673` | Comma-separated IDs; can combine with `q` |
| `aspect_filter` | `categoryId:175673,Brand:{NVIDIA}` | Requires `category_ids` query param **and** `categoryId` inside `aspect_filter` |

Use category when hunts are vertical-specific (e.g. GPUs → `Computers/Tablets & Networking` subtree). Category IDs are marketplace-specific.

### Location / distance

**Important distinction for Vulture:**

| Capability | Supported? | Mechanism |
|------------|------------|-----------|
| Item location in response | **Yes** | `itemLocation` (city, state, country, postal code — postal may be anonymized) |
| Filter by item country | **Yes** | `filter=itemLocationCountry:US` |
| Delivery to buyer postal code | **Partial** | `filter=deliveryPostalCode:77002,deliveryCountry:US` — affects shipping estimates / some delivery filters, **not** a Craigslist-style “within 25 miles” inventory filter for all listings |
| Sort by distance | **Only with local pickup filters** | `sort=distance` requires `pickupCountry`, `pickupPostalCode`, `pickupRadius`, `pickupRadiusUnit`, and `deliveryOptions:SELLER_ARRANGED_LOCAL_PICKUP` |
| General radius on all local listings | **No** equivalent to Craigslist `search_distance` | |

**Recommendation for Vulture v1:** Treat eBay as **national search + optional `itemLocationCountry:US`**, then apply deterministic post-filters on `location` string if hunts need city-level narrowing (conservative: reject only when location clearly outside target). Do **not** claim `supports_radius=True` in registry metadata until a dedicated location probe proves otherwise.

Optional header for better geo-aware behavior:

```http
X-EBAY-C-ENDUSERCTX: contextualLocation=country%3DUS%2Czip%3D77002
```

### Other useful filters

| Filter | Use for Vulture |
|--------|-----------------|
| `buyingOptions:{FIXED_PRICE\|AUCTION}` | Include auctions when desired |
| `deliveryCountry:US` | US-only inventory |
| `sellerAccountTypes:{BUSINESS\|INDIVIDUAL}` | Optional noise reduction |

### `fieldgroups` for richer location

| Value | Effect |
|-------|--------|
| `MATCHING_ITEMS` | Default — items only |
| `EXTENDED` | Adds `shortDescription` and **`itemLocation.city`** ([Browse guide](https://developer.ebay.com/api-docs/buy/static/api-browse.html)) |

Use `fieldgroups=EXTENDED` if city-level `location` is required for rules.

---

## 6. Rate limits / quota

### Default Browse API limits

Per [API Call Limits](https://developer.ebay.com/develop/apis/api-call-limits):

| API | Default quota |
|-----|----------------|
| Browse API (all methods except noted) | **5,000 calls / day** (application-level, not per end user) |
| `getItems` | 5,000 / day |

Limits apply to the **application keyset**, aggregated across all users of that app.

### Monitoring

- [Analytics API `getRateLimits`](https://developer.ebay.com/api-docs/developer/analytics/resources/rate_limit/methods/getRateLimits) with `api_name=browse` and client-credentials token.

### Vulture impact estimate

One hunt cycle per active eBay hunt ≈ **1 search call** (+ amortized token refresh ≈ 1 call per 2 hours). At dozens of hunts, daily usage stays well under 5,000 unless pagination fans out heavily.

### Increasing limits

[Application Growth Check](https://developer.ebay.com/grow/application-growth-check) — free review for higher quotas and production restricted API access.

### Token rate limits

Separate from Browse search limits; see [Access token rate limits](https://developer.ebay.com/develop/guides-v2/authorization) if token minting is hammered (cache tokens).

---

## 7. Response fields → Vulture `Listing`

Vulture model (`models/listing.py`):

```python
@dataclass
class Listing:
    source: str          # "ebay"
    title: str
    price: Optional[int]   # integer dollars (truncate/round consistently)
    location: Optional[str]
    link: str
```

### Field mapping

| eBay `ItemSummary` field | Vulture field | Notes |
|--------------------------|---------------|-------|
| `title` | `title` | Required; skip item if empty |
| `price.value` (+ `price.currency`) | `price` | Parse string decimal → `int` (match Craigslist: truncate dollars, e.g. `"299.99"` → `299`) |
| `itemWebUrl` | `link` | Canonical view-item URL for dedupe |
| `itemLocation` | `location` | Build string from `city`, `stateOrProvince`, `country` e.g. `"Houston, TX, US"`; may be sparse without `fieldgroups=EXTENDED` |
| `condition` / `conditionId` | *(not in Listing)* | Use in adapter-side pre-filter if hunt rules include condition; do not extend DB schema |
| `itemId` | *(optional internal)* | Stable ID; Browse IDs differ from legacy Finding API IDs |
| `itemAffiliateWebUrl` | — | Only if EPN affiliate parameters are configured |

### Price object shape (example)

```json
"price": {
  "value": "249.99",
  "currency": "USD"
}
```

Also watch `currentBidPrice` for auction-only rows when `buyingOptions` includes `AUCTION`.

### Location object shape (example)

```json
"itemLocation": {
  "city": "Houston",
  "stateOrProvince": "TX",
  "country": "US",
  "postalCode": "77***"
}
```

Postal codes in search results may be **anonymized** per API docs.

### Normalization sketch (pseudocode)

```python
def item_summary_to_listing(item: dict) -> Listing | None:
    title = (item.get("title") or "").strip()
    link = (item.get("itemWebUrl") or "").strip()
    if not title or not link:
        return None

    price_block = item.get("price") or item.get("currentBidPrice")
    price = parse_price_int(price_block.get("value")) if price_block else None

    loc = item.get("itemLocation") or {}
    parts = [loc.get("city"), loc.get("stateOrProvince"), loc.get("country")]
    location = ", ".join(p for p in parts if p) or None

    return Listing(source="ebay", title=title, price=price, location=location, link=link)
```

---

## 8. Sold / completed listings

| Source | Sold/completed? |
|--------|-----------------|
| **Browse API `item_summary/search`** | **No** — active/purchasable listings only |
| **Finding API** | Deprecated (Feb 2025); do not build on it |
| **Marketplace Insights API** | Sold data exists but **restricted** / not generally available to new apps |
| **Terapeak / Seller Hub** | Human research tools, not adapter APIs |

Browse overview explicitly discusses `itemEndDate` / `estimatedAvailabilityStatus` for **excluding ended** listings — the API is oriented toward **current** inventory ([overview](https://developer.ebay.com/api-docs/buy/browse/overview.html)).

**Vulture implication:** eBay adapter finds **live deals**, not sold-price comps. “Selling for over $X” market-research hunts are **out of scope** for Browse API.

---

## 9. Sandbox vs production behavior

| Aspect | Sandbox | Production |
|--------|---------|------------|
| API host | `api.sandbox.ebay.com` | `api.ebay.com` |
| OAuth host | `api.sandbox.ebay.com/identity/...` | `api.ebay.com/identity/...` |
| Credentials | Sandbox keyset | Production keyset (after compliance + approvals) |
| Data | Test/synthetic catalog — **not** live eBay inventory | Real listings |
| Access | Developer + eBay member account | Application Growth Check + Buy API approvals |
| Behavior | Validate parsing, error handling, token cache | Real hunt alerts |

**Testing strategy:**

1. Implement against **sandbox** first (`experiments/adapters/ebay_browse_probe.py`).
2. Run one manual **production** smoke test only after production keyset is enabled.
3. Never point production tokens at CI logs.

Sandbox responses may not mirror live result counts or field population; treat field mapping tests as structural, not data-accuracy, in sandbox.

---

## 10. Minimal environment variables (no secrets in repo)

Add to **Raven `.env` only** (do **not** commit values; optional future `.env.example` entries without real secrets):

| Variable | Required | Example / values |
|----------|----------|------------------|
| `EBAY_CLIENT_ID` | Yes | Sandbox or production App ID |
| `EBAY_CLIENT_SECRET` | Yes | Cert ID |
| `EBAY_ENV` | Yes | `sandbox` \| `production` |
| `EBAY_MARKETPLACE_ID` | Yes (default in code OK) | `EBAY_US` |
| `EBAY_OAUTH_SCOPE` | Optional | Default `https://api.ebay.com/oauth/api_scope` |
| `EBAY_DEFAULT_DELIVERY_COUNTRY` | Optional | `US` — for `deliveryCountry` filter |
| `EBAY_DEFAULT_POSTAL_CODE` | Optional | `77002` — for `deliveryPostalCode` / `contextualLocation` |
| `EBAY_DEFAULT_CATEGORY_ID` | Optional | Per-vertical default category |

**Not required for search-only adapter:** RuName, user refresh token, Discord keys.

Adapter code should fail fast with a clear log message if credentials are missing — never read `.env` from adapter import side effects; follow existing Vulture pattern (env read in `main.py` / config layer when implemented).

---

## 11. Adapter implementation plan

### Phase 0 — This document (done)

Recon only; no production adapter, no registry change.

### Phase 1 — Isolated Browse probe (next)

Create `experiments/adapters/ebay_browse_probe.py`:

- Load credentials from environment (no `.env` writes).
- Mint application token (client credentials).
- `GET .../item_summary/search?q=...&limit=5&fieldgroups=EXTENDED`
- Print HTTP status, token expiry, raw count, normalized candidate dicts.
- Optional CLI flags: `--max-price`, `--condition USED`, `--category-id`.

**Success criteria:** HTTP 200, ≥1 `itemSummaries[]`, all five Listing fields populated for majority of items.

### Phase 2 — Production adapter module (later branch)

Create `adapters/ebay.py`:

```text
_fetch_application_token()   # cached, 2h TTL
_build_search_params(query, city, limit, hunt_context?)
_search_browse(...)
_item_summary_to_listing(...)
search_ebay(query, city, limit) -> list[Listing]
```

- Use `requests` only.
- On failure: log + return `[]` (do not crash hunt cycle).
- Map `city` → postal code via small lookup table or pass-through if already zip (pattern from `carsdotcom`).

### Phase 3 — Registry (experimental only)

In a separate PR after probe + Raven smoke:

- Register `search_ebay` in `adapters/registry.py`.
- Metadata example:

```python
"ebay": {
    "stable": False,
    "experimental": True,
    "requires_browser": False,
    "requires_login": False,
    "supports_location": False,  # until verified
    "location_control": "national_with_optional_filters",
    "supports_radius": False,
    "supports_price_filter_in_url": False,  # filter param instead
    "verticals": ["general_marketplace", "computer_parts", "gaming", "home_theater"],
}
```

- Do **not** add to default translated hunt sources.

### Phase 4 — Hunt / rules integration

- Ensure `engine/rules.py` max-price rules align with adapter `filter=price:[..N]`.
- Condition rules: map hunt vocabulary → `conditions:{...}` filter where possible.
- Document that sold-comp hunts cannot use eBay.

### Out of scope (per task constraints)

- `.env` / committed secrets
- DB schema changes
- Discord command changes
- Craigslist modifications
- Stable registry promotion

---

## 12. Risks and limitations

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Production API access denied or delayed** | High | Start sandbox; document business use case clearly in Growth Check; keep adapter experimental |
| **5,000 calls/day cap** | Low–medium | Cache tokens; one search per hunt; paginate only when needed |
| **No sold listings** | High for comp-style hunts | Exclude eBay from those hunt types; document limitation |
| **Weak local geo targeting** | Medium | Honest capability metadata; optional post-filter on `location` text |
| **Auction price vs BIN price** | Medium | Prefer `price`; fall back to `currentBidPrice`; include `buyingOptions` filter explicitly |
| **Condition/category vocabulary drift** | Medium | Use `conditionId`; log unmapped filters |
| **Buy API license / restricted use** | High | Personal deal-alert use case may still need approval; read [API License Agreement](https://developer.ebay.com/join/api-license-agreement) |
| **EPN / affiliate rules** | Low for non-affiliate links | Use `itemWebUrl` unless affiliate program is joined |
| **Sandbox ≠ production data** | Medium | Re-verify mapping on production before marking stable |
| **Token in logs** | High | Never log `access_token` or Basic auth header |

---

## 13. Endpoint examples

### A. Get application token (sandbox)

```bash
curl -s -X POST 'https://api.sandbox.ebay.com/identity/v1/oauth2/token' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -H 'Authorization: Basic '"$(printf '%s:%s' "$EBAY_CLIENT_ID" "$EBAY_CLIENT_SECRET" | base64 -w0)" \
  -d 'grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope'
```

### B. Keyword search with max price and condition (production)

```bash
curl -s -G 'https://api.ebay.com/buy/browse/v1/item_summary/search' \
  -H "Authorization: Bearer ${EBAY_ACCESS_TOKEN}" \
  -H 'X-EBAY-C-MARKETPLACE-ID: EBAY_US' \
  -H 'Accept: application/json' \
  --data-urlencode 'q=rtx 3080' \
  --data-urlencode 'limit=25' \
  --data-urlencode 'fieldgroups=EXTENDED' \
  --data-urlencode 'filter=price:[..300],priceCurrency:USD,conditions:{USED},deliveryCountry:US'
```

### C. Category + keyword

```text
GET /buy/browse/v1/item_summary/search?q=thinkpad&category_ids=58058&limit=10
```

### D. Local pickup radius (not Vulture v1 default)

```text
filter=pickupCountry:US,pickupPostalCode:77002,pickupRadius:25,pickupRadiusUnit:mi,deliveryOptions:SELLER_ARRANGED_LOCAL_PICKUP
sort=distance
```

---

## 14. Recommended next Cursor prompt (implementation)

Copy/paste for the implementation branch:

```text
Implement an experimental eBay Browse API probe and adapter for Vulture.

Constraints:
- Follow docs/adapters/EBAY_BROWSE_API_RECON.md
- Add experiments/adapters/ebay_browse_probe.py first; prove sandbox search + Listing mapping
- Then add adapters/ebay.py with cached OAuth client-credentials token (2h TTL)
- Map ItemSummary -> models.listing.Listing (title, price int, location string, itemWebUrl -> link, source="ebay")
- Support query + limit; map optional max price to filter=price:[..N],priceCurrency:USD
- Support conditions filter when feasible
- Read EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_ENV, EBAY_MARKETPLACE_ID from environment only
- Do NOT modify .env, DB schema, Discord, Craigslist, or stable registry defaults
- Register "ebay" in adapters/registry.py as experimental only after probe passes
- Fail gracefully (log + return [])
- Add no secrets to git

Test:
  EBAY_ENV=sandbox EBAY_CLIENT_ID=... EBAY_CLIENT_SECRET=... \
    python experiments/adapters/ebay_browse_probe.py "rtx 3080" --limit 5

Commit message: feat: add experimental ebay browse api adapter
```

---

## References

- [Browse API overview](https://developer.ebay.com/api-docs/buy/browse/overview.html)
- [search (item_summary)](https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search)
- [ItemSummary type](https://developer.ebay.com/api-docs/buy/browse/types/gct:ItemSummary)
- [Buy API field filters](https://developer.ebay.com/api-docs/buy/static/ref-buy-browse-filters.html)
- [Buy APIs requirements](https://developer.ebay.com/api-docs/buy/static/buy-requirements.html)
- [Client credentials grant](https://developer.ebay.com/api-docs/static/oauth-client-credentials-grant.html)
- [API call limits](https://developer.ebay.com/develop/apis/api-call-limits)
- [Application Growth Check](https://developer.ebay.com/grow/application-growth-check)
- Vulture scraping recon: `experiments/adapters/ebay_probe.py`
