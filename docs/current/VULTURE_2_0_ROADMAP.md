# Vulture 2.0+ Roadmap (Current)

Last refreshed: 2026-06-10 (UTC)

Platform roadmap: [AVIARY_PROJECT_CONTEXT.md](AVIARY_PROJECT_CONTEXT.md) §9.

## Baseline complete (in code)

- Discord hunt lifecycle + intent-based create
- SQLite hunts + listings
- Adapter registry + capability metadata (8 registered sources)
- Multi-source fan-out + vertical source profiles
- Deterministic runtime filter engine
- Vehicle translator v2 + regression tests
- Raven systemd runtime (bot + scheduler timer)
- Crow v0.2, Canary v0.1, dashboard v0.2 (sibling services in repo)

## Current phase: documentation sync + adapter evidence

1. Aviary umbrella docs accurate and scoped (2026-06-10 refresh).
2. Repeatable smoke checks per registered adapter.
3. Adapter classifications stay evidence-based.

## Near-term milestones

### M1 — Adapter health evidence

- Standardize smoke scripts under `scripts/smoke_*_adapter.py`.
- Log pass/fail in `SESSION_LOG.md`.

### M2 — Test surface expansion

- Hunt loading mode tests (`yaml`/`db`/`mixed`).
- Multi-source fan-out regression tests.

### M3 — Adapter promotion decisions

- Promote beta/experimental → stable only after repeated successful Raven cycles.
- Keep eBay probe-only until Browse API or approved path exists.

### M4 — Aviary platform hygiene

- Align Crow default mounts with `/mnt/storage/*` (config or Raven `.env`).
- Canary → Discord alert wiring (future).
- Nest dashboard consuming `crow/system/` (future).

## Mid-term direction

- Vertical/source ergonomics without redesigning core pipeline.
- One adapter at a time for new sources.
- Optional Crow extraction to separate deploy unit when control features arrive.
