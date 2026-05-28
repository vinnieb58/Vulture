# Vulture 2.0+ Roadmap (Current)

Last refreshed: 2026-05-28 (UTC)

## Baseline complete

The following are present in live code now:

- Discord command runtime for hunt lifecycle and intent-based create flow
- SQLite-backed hunts + listings
- Adapter registry + capability metadata
- Multi-source hunt fan-out execution
- Deterministic runtime filter engine with structured constraints
- Vehicle translator v2 routing + regression tests

## Current phase: status hardening

Focus:

1. Keep docs synchronized with code truth.
2. Build repeatable smoke checks for registered adapters.
3. Keep adapter classifications accurate (stable vs experimental vs probe only).

## Near-term milestones

### M1 — Adapter health evidence

- Add/standardize smoke commands for Craigslist, OfferUp, Cars.com.
- Capture consistent pass/fail evidence in session logs.

### M2 — Test surface expansion

- Add repository/runtime tests around hunt loading mode behavior (`yaml/db/mixed`).
- Add focused tests around multi-source fan-out expectations.

### M3 — Adapter promotion decisions

- Evaluate whether experimental adapters can be promoted based on repeated successful cycles.
- Keep eBay/Micro Center/Mercari outside runtime unless implementation evidence exists.

## Mid-term direction

- Improve vertical/source ergonomics while preserving deterministic runtime filtering.
- Keep new adapter introductions incremental (one source at a time).
- Avoid architecture redesign while core adapter reliability is still being proven.
