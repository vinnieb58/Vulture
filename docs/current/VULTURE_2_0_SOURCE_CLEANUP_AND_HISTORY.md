# Vulture Source Cleanup and History

_Last refreshed: 2026-06-10_

## Purpose

This file replaces the old scattered project-source context. It explains which historical assumptions are obsolete, which parts remain useful, and how to interpret the project going forward.

**Platform naming (2026-06):** The repo is the **Aviary** monorepo. **Vulture** is the deal-hunting service. See [AVIARY_PROJECT_CONTEXT.md](AVIARY_PROJECT_CONTEXT.md).

The goal is to prevent future confusion when ChatGPT, Cursor, or another assistant sees older planning files.

## Current rule

The live repo and current Vulture 2.0 markdown files override older v1.0 planning documents.

If a document says YAML is the main hunt source, it is historical.

If a document says Discord-started hunt creation is deferred, it is historical.

If a document says DB-backed hunts are future work, it is historical.

## Historical timeline

### Stage 1 — v1.0 prototype planning

Original Vulture target:

- Craigslist adapter
- YAML hunts
- SQLite listing persistence
- dedupe by link
- Discord webhook alerts
- simple rule engine
- scheduler

This stage produced the original v1.0 architecture and task-list files.

These are useful for understanding where the project started, but they no longer describe the current operating system.

### Stage 2 — v1.0 completion / transition

The project implemented the practical core:

- Craigslist scraping
- normalized listings
- SQLite storage
- dedupe
- basic filtering
- Discord alerting
- scheduler path
- logging

At this point, the system was good enough to evolve beyond a YAML-managed local script.

### Stage 3 — v2.0 planning

The v2.0 plan introduced:

- Discord as the primary control surface
- DB-backed hunts
- hunt lifecycle commands
- LLM-assisted natural-language translation
- deterministic runtime execution
- multi-vertical future direction

The v2.0 plan was originally forward-looking, but much of it has now been implemented.

### Stage 4 — v2.0 implementation reality

Implemented reality now includes:

- Discord bot command path
- command router
- hunt service
- hunt repository
- SQLite hunt persistence
- DB-backed runtime loading
- rules-based translator
- structured adapter options
- deterministic rule enforcement
- title-intelligence refinement

This is the current baseline.

### Stage 5 — current next phase

The next phase is adapter expansion.

Before adding websites, the code needs:

- adapter registry
- source capability metadata
- experimental adapter probe pattern
- cleaner docs
- source grouping by vertical

## Files being replaced

The previous GPT Project source list included files like:

```text
SESSION_LOG_updated_2026-03-29.md
PROJECT_STATUS_updated_2026-03-29.md
PROJECT_STATUS_2026-03-17.md
SESSION_LOG_2026-03-17.md
SESSION_LOG_updated_2026-03-15.md
PROJECT_STATUS_updated_2026-03-15.md
README.md
Vulture_v2.0_Specification.docx
Vulture_Future_Roadmap.docx
Vulture_v1_0_Task_List_and_Revision_Log.docx
Vulture_Architecture_Blueprint_v1_0.docx
```

Those should be replaced in the GPT Project source area with the new consolidated markdown files.

## Recommended replacement source files

Upload these instead:

```text
VULTURE_2_0_CURRENT_STATUS.md
VULTURE_2_0_ARCHITECTURE.md
VULTURE_2_0_ADAPTER_EXPANSION_PLAN.md
VULTURE_2_0_ROADMAP.md
VULTURE_2_0_SOURCE_CLEANUP_AND_HISTORY.md
SESSION_LOG.md
```

## What to do with old files

Since GPT Projects may not support folders for sources, delete the old files from the Project source list and upload only the replacement markdown files.

In the GitHub repo, old docs can be kept under an archive folder if desired:

```text
docs/archive/v1_0/
docs/archive/planning/
docs/archive/old_status_logs/
```

But inside the GPT Project source list, fewer accurate files are better than many contradictory files.

## Interpretation guide for future assistants

When helping with Vulture:

1. Treat Vulture as a 2.0 system.
2. Assume DB-backed hunts are the normal path.
3. Assume Discord command control exists.
4. Assume Craigslist is the only stable current adapter unless the repo shows otherwise.
5. Preserve deterministic runtime filtering.
6. Do not reintroduce YAML as the main hunt source.
7. Do not propose a database redesign unless explicitly requested.
8. Do not use the LLM for runtime listing acceptance/rejection.
9. Keep Facebook Marketplace experimental until proven.
10. Prefer small, reviewable changes.

## Stale assumptions to ignore

Ignore these old assumptions if found in historical files:

| Stale assumption | Current truth |
|---|---|
| YAML is the source of truth | SQLite DB is the active 2.0 hunt source |
| Discord-started hunts are deferred | Discord hunt creation exists |
| LLM translation is future work | Rules-based/LLM-assisted translation exists |
| v2.0 is only a plan | v2.0 is now the working baseline |
| Scheduler is only Windows Task Scheduler | Raven/headless Linux runtime is now part of the operating plan |
| Adapter expansion is far future | Adapter expansion is the next major phase |

## Current source priorities

The most important docs going forward are:

1. `VULTURE_2_0_CURRENT_STATUS.md`
2. `VULTURE_2_0_ARCHITECTURE.md`
3. `VULTURE_2_0_ADAPTER_EXPANSION_PLAN.md`
4. `VULTURE_2_0_ROADMAP.md`
5. `SESSION_LOG.md`

This file exists mainly to explain the cleanup and prevent historical contradictions from resurfacing.
