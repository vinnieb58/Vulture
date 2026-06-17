# Project Status — 2026-06-17

## Platform context

This repo is the **Aviary monorepo**. **Vulture** is the active deal-hunting service; **Crow**, **Canary**, and the **dashboard** provide read-only Raven observability. **Sparrow** is an experimental household meal-ordering assist. See [AVIARY_PROJECT_CONTEXT.md](AVIARY_PROJECT_CONTEXT.md).

## Overall status

| Area | Status |
|------|--------|
| Aviary documentation | **In transition** — Sparrow naming + service catalog refresh |
| Vulture 2.0+ runtime | **Production-active** on Raven (systemd + SQLite hunts) |
| Adapter registry | **Implemented** — 8 registered sources |
| Crow | **v0.2** — `/check` Raven health group + v0.1 commands |
| Canary | **v0.1** — Docker periodic checks → JSON/log |
| Dashboard | **v0.2** — read-only `:8088` UI |
| Roost | **Operational** — `/mnt/storage/*` mounts monitored, not a separate daemon |
| **Sparrow** | **Experiment v0** — Simply Fresh meal dry-run on Raven; manual probes only |

## Sparrow — what works today

- Saved Playwright session reuse (`simplyfresh_storage_state.json`)
- Logged-in flow: Order Now → profile chooser → **Choose your meals** calendar
- Profile deduplication + `--profile-name` / `--school` filters
- Inspect-only calendar mapping and dry-run non-vegetarian meal selection (max N weekdays)
- Overlay/modal dismiss (profile chooser + meal modal) before next day click
- Forbidden-control guards — no submit/checkout/pay automation

See [SPARROW_MEAL_ORDERING.md](SPARROW_MEAL_ORDERING.md).

## Sparrow — not yet

- systemd service or scheduled execution
- Order submission / checkout automation
- Discord/Telegram/WhatsApp operator channel
- Promotion out of `experiments/simplyfresh_probe/`

## Vulture — what works today

- Discord hunt creation and lifecycle (`/hunt`, `/hunt_list`, `/hunt_pause`, …)
- DB-backed hunt storage (`VULTURE_HUNT_SOURCE=db` on Raven)
- Adapter registry dispatch + multi-source fan-out by vertical
- Deterministic rules filtering + Discord webhook alerts
- Registered adapters: `craigslist` (stable), `mercari`/`microcenter` (beta), plus experimental retail/vehicle sources

## Active refinement areas

Quality work continues on vertical-specific hunt translation and false-positive reduction (GPUs, TVs, vehicles). Architecture is stable; remaining work is **filter/translation quality**, not platform redesign.

Sparrow: expand dry-run reliability and artifact review before any scheduler or service promotion.

Known imperfect areas (from live testing history):

- Vehicle typo robustness and parts-listing exclusions
- Broad hunts noisier than tier-specific hunts
- Experimental adapters (OfferUp GeoIP, Cars.com Playwright flakiness)
- Sparrow DOM sensitivity (overlays, autosave risk)

## Immediate priorities

1. Sparrow documentation and operator runbook ([SPARROW_MEAL_ORDERING.md](SPARROW_MEAL_ORDERING.md)).
2. Continue Sparrow dry-run validation on Raven (multi-day selection without pointer blocking).
3. Adapter smoke evidence for registered sources.
4. Optional: align Crow default mount paths with `/mnt/storage/*` via Raven `.env`.

## Definition of success (current phase)

- Docs clearly separate Aviary / Raven / Vulture / Crow / Canary / Roost / **Sparrow**.
- Sparrow documented as experiment-only with explicit safety boundaries.
- Vulture docs remain accurate but scoped to deal-hunting.
- Production path documented as: Discord → SQLite → systemd timer → adapters → rules → alerts.
- No Sparrow systemd or unattended scheduling until explicitly approved.
