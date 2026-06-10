# Vulture 2.0+ Adapter Expansion Plan (Current Reality)

Last refreshed: 2026-06-10 (UTC)

Platform context: [AVIARY_PROJECT_CONTEXT.md](AVIARY_PROJECT_CONTEXT.md).

## Current adapter baseline

The registry foundation **exists and is in production use** in `adapters/registry.py`.

### Runtime-registered adapters

| Source | Classification | Notes |
|--------|----------------|-------|
| `craigslist` | stable | Primary production adapter |
| `mercari` | beta | GraphQL search |
| `microcenter` | beta | Playwright; `storeid`; computer/laptop verticals |
| `offerup` | experimental | GeoIP-only location |
| `carsdotcom` | experimental | Playwright; vehicles; flaky |
| `swappa` | experimental | Requests/HTML |
| `bestbuy` | experimental | Playwright |
| `newegg` | experimental | Requests/HTML |

Vertical defaults: `engine/source_selection.py` includes experimental retail sources in computer/gaming/retail profiles when registered.

### Probe-only / no runtime adapter

| Source | Status |
|--------|--------|
| eBay | Probe scripts under `experiments/adapters/`; Browse API recommended |
| Facebook Marketplace, others | Recon/probe only unless added to `_REGISTRY` |

## Expansion policy

1. Keep runtime deterministic (`rules.py`); adapters return normalized `Listing` objects.
2. No DB writes or alerts inside adapter modules.
3. Promote to stable only after repeated smoke/hunt-cycle evidence on Raven.
4. Keep `_CAPABILITIES` truthful (`geoip_only`, `requires_browser`, `failure_mode`, etc.).

## Next hardening priorities

1. Repeatable smoke checks for all `_REGISTRY` sources (`scripts/smoke_*`).
2. Log outcomes in `SESSION_LOG.md`.
3. Regression tests for registry metadata and source selection paths.
4. One new source at a time — avoid parallel unproven adapters.

## Promotion checklist (experimental/beta → stable)

All must be true:

- Repeated successful runs on Raven without crashing hunt cycles
- Normalized listings (`source`, `title`, `price`, `location`, `link`)
- Dedupe and alerts behave under real data
- Location behavior documented and predictable
- No adapter-specific hacks accumulating in `main.py` beyond documented options (e.g. `storeid`)
