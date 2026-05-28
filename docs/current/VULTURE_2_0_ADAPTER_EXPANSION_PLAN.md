# Vulture 2.0+ Adapter Expansion Plan (Current Reality)

Last refreshed: 2026-05-28 (UTC)

## Current adapter baseline

The registry foundation already exists in `adapters/registry.py`.

### Runtime-registered adapters

| Source | Classification | Why |
|---|---|---|
| `craigslist` | stable | Primary production adapter |
| `offerup` | experimental candidate | Works as adapter, but location control is GeoIP-only |
| `carsdotcom` | experimental candidate | Works with Playwright on residential-like network conditions; still marked experimental |

### Probe-only/deferred

| Source | Classification | Why |
|---|---|---|
| eBay | probe only | Recon documents repeated 403/network-layer block; Browse API suggested |
| Micro Center | probe only | Probe script exists; no runtime adapter |
| Mercari | deferred | No implementation/probe in repository |

## Expansion policy (from current code constraints)

1. Keep runtime deterministic (`rules.py`) and adapter outputs normalized to `Listing`.
2. Keep adapter side effects out of adapter modules (no direct DB writes/alerts from adapters).
3. Promote adapters to stable only after repeated smoke results in real run cycles.
4. Keep capability metadata truthful (`stable`, `experimental`, `requires_browser`, `supports_location`, etc.).

## Next adapter hardening priorities

1. Add repeatable smoke checks for all registered adapters.
2. Record per-adapter pass/fail evidence in docs/session logs.
3. Add minimal regression coverage around registry metadata access paths.
4. Avoid introducing new adapter families until current experimental adapters have clear status evidence.

## Promotion checklist (experimental -> stable)

An adapter should only be promoted to stable when all are true:

- Runs repeatedly without fatal errors in normal hunt cycles
- Returns normalized listings (`source`, `title`, `price`, `location`, `link`)
- Dedupe and alerts behave correctly under real data
- Location behavior is documented and predictable
- Bot/rules pipeline operates without adapter-specific hacks in `main.py`
