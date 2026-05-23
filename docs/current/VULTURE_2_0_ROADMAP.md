# Vulture 2.0 Roadmap

_Last refreshed: 2026-05-21_

## Version framing

The current working system should be treated as **Vulture 2.0**.

The original v1.0 project was the Craigslist/YAML/webhook/scheduler version. The current system has moved beyond that and now includes Discord-based hunt management, SQLite-backed hunts, and LLM/rules-assisted hunt translation.

## Current version: Vulture 2.0

### Definition

Vulture 2.0 is a Discord-controlled, DB-backed deal-hunting engine with deterministic scraping, filtering, deduplication, storage, and alerts.

### Included in 2.0 baseline

- Craigslist adapter
- SQLite listing persistence
- SQLite hunt persistence
- Discord bot command layer
- Discord slash-command hunt lifecycle
- DB-backed runtime mode
- deterministic rules engine
- rules-based/LLM-assisted translation
- structured hunt fields and adapter options
- logging
- scheduler/manual execution path
- headless Raven server deployment path

### 2.0 quality focus

The current quality work is vertical-specific title intelligence:

- GPUs
- RAM
- TVs
- vehicles
- general marketplace noise reduction

## 2.0 remaining cleanup

Before major expansion, clean the foundation:

1. Replace stale GPT Project source docs.
2. Treat old v1.0/YAML docs as archived history.
3. Make sure README reflects DB-backed Discord 2.0 behavior.
4. Confirm Windows dev and Raven runtime are synchronized through GitHub.
5. Add adapter registry.
6. Add source capability metadata.
7. Preserve current Craigslist behavior.

## 2.1 — Adapter expansion foundation

### Goal

Prepare the codebase for additional website adapters without changing the runtime architecture.

### Scope

- adapter registry
- source capability metadata
- experimental adapter probe pattern
- adapter smoke-test pattern
- source grouping by vertical
- first additional stable adapter candidate research

### Non-goals

- no database redesign
- no web UI
- no LLM runtime filtering
- no Facebook Marketplace production adapter yet
- no major Discord UX redesign in the same batch

### Success criteria

- Craigslist still works through the registry.
- Adding a new source no longer requires editing core runtime logic in multiple places.
- Each source can declare whether it is stable, experimental, browser-required, login-required, and which verticals it supports.

## 2.2 — First additional stable adapter

### Goal

Add one new source after the adapter registry foundation is stable.

### Candidate sources

Priority candidates:

1. Swappa or similar electronics marketplace
2. eBay experiment
3. Micro Center experiment
4. vehicle-specific site experiment

### Success criteria

- New adapter returns normalized `Listing` objects.
- New adapter is registered cleanly.
- A DB-backed hunt can target the new source.
- New listings are deduped and alerted through the existing pipeline.
- Adapter failure does not crash the whole run.

## 2.3 — Multi-source hunts

### Goal

Allow a single hunt to run across multiple sources cleanly.

### Scope

- per-hunt `source_sites` support, if not already complete
- per-source adapter options
- per-source run summaries
- better handling of one source failing while others continue
- cross-source dedupe review

### Success criteria

- One hunt can search multiple websites.
- Results remain normalized.
- Alerts remain readable.
- Logs show source-by-source behavior.

## 2.4 — Vertical-aware source selection

### Goal

Make source selection smarter by vertical instead of presenting a flat website list.

### Example

```text
Intent: RTX 3080 under $300
Vertical: computer_parts
Default sources: craigslist, swappa, ebay if stable
```

```text
Intent: Toyota Sequoia under $25k
Vertical: vehicles
Default sources: craigslist, vehicle-specific sources when added
```

### Scope

- vertical source groups
- translator integration with source selection
- optional user override
- experimental source opt-in

## 2.5 — Discord UX cleanup

### Goal

Make commands faster and clearer.

Previously preferred direction:

- `/hunt ...` for normal LLM-based quick hunt creation
- `/huntmanual_...` for manual/structured hunt creation
- `/huntadmin_...` for lifecycle/admin actions

### Scope

- reduce keystrokes
- simplify normal hunt creation
- keep admin/lifecycle actions discoverable
- avoid mixing UX overhaul with adapter expansion commit

## 3.0 — Valuation and scoring

### Goal

Move beyond matching into deal quality.

### Possible features

- bargain scoring
- comparable price lookup
- price history
- re-alert on price drop
- trend summaries
- listing quality summaries
- category-specific valuation logic

### Non-goal for now

Do not start valuation before adapter expansion and deterministic filtering are stable.

## 3.x — Platform expansion

Possible later features:

- web dashboard
- analytics/reporting
- per-hunt performance stats
- mobile-friendly controls
- richer Discord summaries
- saved searches by user/profile
- advanced archive/purge workflows

## Near-term priority order

1. Replace GPT Project source files with current markdown files.
2. Verify GitHub has the current code from Windows.
3. Pull current code onto Raven.
4. Create `feature/adapter-expansion-foundation`.
5. Add adapter registry and capability metadata.
6. Confirm Craigslist still works.
7. Commit foundation.
8. Research/probe the first new source.
9. Promote only one new adapter once proven.

## Definition of success for the next phase

The next phase is successful when:

- current docs no longer contradict the code,
- Vulture 2.0 is clearly defined as the active baseline,
- adapter registration is centralized,
- Craigslist still runs exactly as before,
- at least one candidate new source has a probe or feasibility result,
- Facebook Marketplace remains experimental rather than derailing the stable adapter path.
