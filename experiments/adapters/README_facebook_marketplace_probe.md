# Facebook Marketplace Probe (probe-only)

Reconnaissance script for Vulture. **Not** registered in `adapters/registry.py` and **not** selectable in production hunts.

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
