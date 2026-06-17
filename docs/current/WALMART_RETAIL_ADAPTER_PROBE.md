# Walmart & Retail Adapter Probe Notes

_Last updated: 2026-06-17_

## Walmart (`experiments/adapters/walmart_probe.py`)

| Field | Value |
|-------|-------|
| **Source tested** | Walmart search (`https://www.walmart.com/search?q=...`) |
| **Method used** | Primary: `requests` + BeautifulSoup; escalation: `curl_cffi` (`--cffi`), Playwright (`--playwright`) |
| **Parsing** | Prefer embedded `__NEXT_DATA__` JSON (`searchResult.itemStacks[].items`, filter `__typename == "Product"`); CSS fallback on `[data-item-id]` cards |
| **Viable from cloud agent (2026-06-17)** | **No** — PerimeterX redirects to `/blocked` with title "Robot or human?" for requests, curl_cffi, and headless Playwright |
| **Viable from Raven residential IP (2026-06-17)** | **No** — same PerimeterX block on all methods (see Raven validation below) |
| **Parser viability** | **Yes** — unit-tested against `tests/fixtures/walmart_search_next_data_snippet.html` |
| **Runtime recommendation** | **Remain probe-only** — Raven residential validation confirmed blocking |
| **Promote to experimental adapter?** | **No** — do not create `adapters/walmart.py`, do not register in `adapters/registry.py`, do not add to default source profiles |
| **Known blocking** | PerimeterX bot wall on datacenter/cloud IPs; `/blocked?url=...` redirect; ~15 KB challenge shell |
| **Rate limits** | Not observed (blocked before search results) |
| **Location behavior** | Search JSON may include `fulfillmentSummary`, `fulfillmentBadge`, `availabilityStatusV2`; no zip/store URL param tested — treat as shipping/pickup text only |

### Raven residential validation (2026-06-17)

| Field | Value |
|-------|-------|
| **Date** | 2026-06-17 |
| **Environment** | Raven residential IP |
| **Methods tested** | `requests`, `curl_cffi` (`--cffi`), Playwright (`--playwright`) |
| **Queries** | `steam deck`, `65 inch tv`, `rtx 4070` |
| **Result** | **Blocked on all methods** — 0 extracted listings per query |
| **Blocking indicators** | `/blocked` redirect, page title "Robot or human?", `px-captcha`, `_px` body markers |
| **Failure behavior** | Safe empty results with warnings; no crashes |
| **Runtime decision** | **Remain probe-only** |
| **Promotion decision** | Do **not** create `adapters/walmart.py`; do **not** add to `adapters/registry.py`; do **not** add to default source profiles in `engine/source_selection.py` |

Smoke commands used on Raven:

```bash
python experiments/adapters/walmart_probe.py --query "steam deck" --limit 5
python experiments/adapters/walmart_probe.py --query "65 inch tv" --limit 5
python experiments/adapters/walmart_probe.py --query "rtx 4070" --limit 5
python experiments/adapters/walmart_probe.py --query "steam deck" --cffi --limit 5
python experiments/adapters/walmart_probe.py --query "steam deck" --playwright --limit 5
```

### Smoke usage

```bash
python experiments/adapters/walmart_probe.py --query "steam deck" --limit 5
python experiments/adapters/walmart_probe.py --query "65 inch tv" --limit 5
python experiments/adapters/walmart_probe.py --query "rtx 4070" --cffi --limit 5
python experiments/adapters/walmart_probe.py --query "steam deck" --playwright --limit 5
python experiments/adapters/walmart_probe.py --query "steam deck" --limit 5 --json
pytest tests/test_walmart_probe.py -q
```

### Raven runtime risk

**None today** — probe is isolated under `experiments/` and is not registered. If Walmart is later registered, failure mode must remain `returns_empty_list` (no hunt-cycle crashes). Deterministic `engine/rules.py` filtering is unchanged.

---

## Other retail sources — quick viability (2026-06-17, cloud IP)

Probed with the same Chrome UA + `requests` pattern used by other Vulture probes. All blocked or empty from this host; none warrant full adapter work in this PR.

| Source | URL pattern | HTTP | Result | Next step |
|--------|-------------|------|--------|-----------|
| **Target** | `/s?searchTerm=` | 200 | `__NEXT_DATA__` present but no product SSR payload from this IP | Dedicated `target_probe.py`; may need residential IP or RedSky API research |
| **Home Depot** | `/s/{query}` | 403 | Akamai "Error Page" | Playwright + residential IP probe only |
| **Lowe's** | `/search?searchTerm=` | 403 | Access Denied | Same as Home Depot |
| **Costco** | `/CatalogSearch?keyword=` | 403 | Access Denied | Skip unless public search works without membership wall |
| **Sam's Club** | `/s/{query}` | 200 | "Let us know you're not a robot" (Walmart-family PerimeterX) | Low priority; membership friction likely |
| **B&H Photo** | `/c/search?q=` | 403 | Cloudflare "Just a moment..." | Playwright probe on Raven only |
| **Adorama** | `/l/?searchinfo=` | 403 | Access denied | Defer |
| **GameStop** | `/search/?q=` | 403 | Cloudflare attention page | Defer |

### Recommended order after Walmart Raven validation

1. **Target** — large catalog overlap with electronics hunts; JSON-in-`__NEXT_DATA__` pattern similar to Walmart/OfferUp.
2. **B&H Photo** — strong camera/PC parts inventory; worth Playwright probe from residential IP.
3. **Home Depot / Lowe's** — home-appliance vertical (`dyson vacuum`, TVs); heavy Akamai — probe-only unless Raven succeeds.

Skip **Costco** and **Sam's Club** until public anonymous search returns product cards without login/membership prompts.

---

## Files in this work

- `experiments/adapters/walmart_probe.py` — recon probe + normalizer
- `tests/fixtures/walmart_search_next_data_snippet.html` — parser fixture
- `tests/test_walmart_probe.py` — parser/blocking unit tests
- `docs/current/WALMART_RETAIL_ADAPTER_PROBE.md` — this note
