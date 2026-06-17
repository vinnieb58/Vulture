# Facebook Marketplace (experimental runtime adapter)

Facebook Marketplace is registered in `adapters/registry.py` as **experimental** and **explicit opt-in only**. It is not included in any default vertical profile or translated hunt `source_sites`.

Raven residential SSR smoke tests (May 2026) returned listings for queries such as steam deck, rtx 4070, 65 inch tv, and mercedes e550, but every run also reported `login_wall` and `captcha_checkpoint` blocker signals. Public access is fragile and may fail without notice.

**Safety boundaries:** no credentials, session storage, cookie persistence, or CAPTCHA/login/checkpoint bypass are implemented. The adapter logs blocker warnings and returns any SSR listings present; otherwise `[]`.

**Explicit usage:** add `facebook_marketplace` to a hunt's `source_sites` list only when you accept the fragility:

```python
resolve_source_sites("general", explicit_sources=["facebook_marketplace", "craigslist"])
```

Probe/recon script (isolated): `experiments/adapters/facebook_marketplace_probe.py`

---

# Facebook Marketplace Probe (probe-only recon)

Reconnaissance script for Vulture. The experimental runtime adapter lives in
`adapters/facebook_marketplace.py` (explicit opt-in via `source_sites` only).
This probe remains useful for isolated viability checks without touching hunt runtime.

## Purpose

Answer whether public Facebook Marketplace search pages expose enough listing data (title, price, location, link, image) for a future adapter — without storing credentials or bypassing login/CAPTCHA controls.

## Commands

```bash
python experiments/adapters/facebook_marketplace_probe.py --query "steam deck" --location "Houston, TX" --limit 5
python experiments/adapters/facebook_marketplace_probe.py --query "macbook screen" --location "Houston, TX" --limit 5
python experiments/adapters/facebook_marketplace_probe.py --query "rtx 4070" --location "Houston, TX" --limit 5 --json
```

Suggested smoke queries:

- steam deck
- macbook screen
- rtx 4070
- 65 inch tv
- mercedes e550

## Prerequisites

```bash
pip install playwright beautifulsoup4 lxml
python -m playwright install chromium
```

## Blockers reported

- `login_wall`
- `captcha_checkpoint`
- `location_permission_wall`
- `location_resolution_failed` (bad city slug → IP-geo fallback)
- `empty_public_results`
- `unsupported_page_shape`
- `region_unavailable` / `marketplace_unavailable`

## Safety boundaries

- Public search/listing URLs only
- No credential storage
- No CAPTCHA/login bypass
- Exits with a report; does not touch hunt runtime

## Tests

Parser/blocker helpers are covered by `tests/test_facebook_marketplace_probe.py` using HTML fixtures (no live Facebook calls in unit tests).
