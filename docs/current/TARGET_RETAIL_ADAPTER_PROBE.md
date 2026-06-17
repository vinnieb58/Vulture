# Target Retail Adapter Probe Notes

_Last updated: 2026-06-17_

## Target (`experiments/adapters/target_probe.py`)

| Field | Value |
|-------|-------|
| **Source tested** | Target search (`https://www.target.com/s?searchTerm=...`) |
| **Primary method** | Redsky public search API (`plp_search_v2`) — same JSON the browser loads after hydration |
| **Fallback methods** | `requests` HTML, optional `curl_cffi` (`--cffi`), Playwright (`--playwright`) for DOM cards |
| **Parsing** | Redsky: `data.search.products[]` with `facet_list` guard against zero-result filler; DOM: `[data-test="product-title"]` / `[data-test="product-price"]`; sparse `__NEXT_DATA__` walk if products ever SSR |
| **Public API key** | Embedded in Target web bundles (`ff457966e64d5e877fdbad070f276d18ecec4a01`) — not a stored credential |
| **Viable from cloud agent (2026-06-17)** | **Partial** — Redsky returns HTTP 403 captcha JSON; HTML returns 200 Next.js shell without SSR product cards; Playwright renders page but 0 product cards without consent/residential conditions |
| **Parser viability** | **Yes** — unit-tested against Redsky JSON and DOM fixtures |
| **Runtime recommendation** | **Remain probe-only** until Raven residential validation |
| **Promote to experimental adapter?** | **Not yet** — do not create `adapters/target.py`, do not register in `adapters/registry.py`, do not add to default source profiles |

### Why Target is next after Walmart

Walmart Raven validation confirmed PerimeterX blocks all fetch paths. Target differs:

- Search HTML returns **HTTP 200** with `__NEXT_DATA__` (Next.js shell).
- Product data is intended to load via **Redsky JSON** (`redsky.target.com`), not SSR HTML.
- When Redsky works, it is a single fast request with structured fields (TCIN, title, price, buy URL, image).
- DOM cards are a Playwright fallback when Redsky is blocked or returns filler.

### Known blocking / behavior

| Signal | Meaning |
|--------|---------|
| Redsky HTTP 403 + `captchaRelativeURL` | Datacenter/bot captcha — common from cloud IPs |
| Redsky HTTP 200 + empty `facet_list` | Zero-result **recommendation filler** (~200 unrelated items) — treat as no match |
| HTML 200, no product cards | Expected — products hydrate client-side |
| Playwright 0 cards | May need Health Data Consent modal dismissal + residential IP |
| `formatted_current_price: "See price in cart"` | MAP-restricted item — use `current_retail` numeric fallback when present |

### Smoke usage

```bash
python experiments/adapters/target_probe.py --query "steam deck" --limit 5
python experiments/adapters/target_probe.py --query "rtx 4070" --limit 5
python experiments/adapters/target_probe.py --query "65 inch tv" --limit 5
python experiments/adapters/target_probe.py --query "steam deck" --redsky-only --limit 5
python experiments/adapters/target_probe.py --query "steam deck" --html-only --playwright --limit 5
python experiments/adapters/target_probe.py --query "steam deck" --limit 5 --json
pytest tests/test_target_probe.py -q
```

### Raven runtime risk

**None today** — probe is isolated under `experiments/` and is not registered. Future adapter must keep `returns_empty_list` failure mode.

### Next step

Run the smoke commands above from **Raven residential IP**. If Redsky returns genuine `facet_list` + products for electronics queries, sketch `adapters/target.py` as experimental — still without adding to default vertical profiles until hunt-cycle evidence accumulates.

---

## Related

- Walmart probe (blocked on Raven): `docs/current/WALMART_RETAIL_ADAPTER_PROBE.md`
- Adapter how-to: `docs/current/VULTURE_ADAPTER_IMPLEMENTATION_REFERENCE.md`

## Files in this work

- `experiments/adapters/target_probe.py` — recon probe + normalizer
- `tests/fixtures/target_redsky_search_response.json` — Redsky parser fixture
- `tests/fixtures/target_search_dom_snippet.html` — DOM fallback fixture
- `tests/test_target_probe.py` — parser/blocking unit tests
- `docs/current/TARGET_RETAIL_ADAPTER_PROBE.md` — this note
