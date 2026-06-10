# Project Status — 2026-06-10

## Platform context

This repo is the **Aviary monorepo**. **Vulture** is the active deal-hunting service; **Crow**, **Canary**, and the **dashboard** provide read-only Raven observability. See [AVIARY_PROJECT_CONTEXT.md](AVIARY_PROJECT_CONTEXT.md).

## Overall status

| Area | Status |
|------|--------|
| Aviary documentation | **In transition** — docs aligned to umbrella naming (this refresh) |
| Vulture 2.0+ runtime | **Production-active** on Raven (systemd + SQLite hunts) |
| Adapter registry | **Implemented** — 8 registered sources |
| Crow | **v0.2** — `/check` Raven health group + v0.1 commands |
| Canary | **v0.1** — Docker periodic checks → JSON/log |
| Dashboard | **v0.2** — read-only `:8088` UI |
| Roost | **Operational** — `/mnt/storage/*` mounts monitored, not a separate daemon |

## Vulture — what works today

- Discord hunt creation and lifecycle (`/hunt`, `/hunt_list`, `/hunt_pause`, …)
- DB-backed hunt storage (`VULTURE_HUNT_SOURCE=db` on Raven)
- Adapter registry dispatch + multi-source fan-out by vertical
- Deterministic rules filtering + Discord webhook alerts
- Registered adapters: `craigslist` (stable), `mercari`/`microcenter` (beta), plus experimental retail/vehicle sources

## Active refinement areas

Quality work continues on vertical-specific hunt translation and false-positive reduction (GPUs, TVs, vehicles). Architecture is stable; remaining work is **filter/translation quality**, not platform redesign.

Known imperfect areas (from live testing history):

- Vehicle typo robustness and parts-listing exclusions
- Broad hunts noisier than tier-specific hunts
- Experimental adapters (OfferUp GeoIP, Cars.com Playwright flakiness)

## Immediate priorities

1. Keep documentation synchronized with code (Aviary transition — this session).
2. Adapter smoke evidence for registered sources.
3. Optional: align Crow default mount paths with `/mnt/storage/*` via Raven `.env`.
4. Continue vertical quality passes without changing runtime architecture.

## Definition of success (current phase)

- Docs clearly separate Aviary / Raven / Vulture / Crow / Canary / Roost.
- Vulture docs remain accurate but scoped to deal-hunting.
- Production path documented as: Discord → SQLite → systemd timer → adapters → rules → alerts.
- No stale claims about YAML-primary hunts, future adapter registry, or tmux scheduler.
