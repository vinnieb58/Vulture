# Vulture 2.0 Adapter Expansion Plan

_Last refreshed: 2026-05-21_

## Purpose

This document defines the path for adding additional websites to Vulture without turning the codebase into spaghetti.

The goal is to support more sources while preserving the current deterministic pipeline:

```text
hunt -> adapter -> normalized Listing -> rules -> dedupe -> store -> alert
```

## Current adapter status

| Source | Status | Notes |
|---|---|---|
| Craigslist | Stable | Current production adapter |
| eBay | Deferred / experimental | Previously encountered challenge / anti-bot behavior |
| Micro Center | Deferred / experimental | Previously returned blocking/403 behavior to plain requests |
| Facebook Marketplace | Experimental only | Likely needs browser automation, cookies/session handling, and may carry policy/brittleness risk |
| Swappa | Candidate | Potentially good electronics-focused second adapter to investigate |
| OfferUp | Candidate but risky | Likely bot friction; marketplace relevance is high |
| Cars.com / Autotrader | Future vehicle candidates | Useful for vehicle vertical if pages are accessible |

## Strategy

Do not add websites randomly.

Use this sequence:

1. Add adapter registry.
2. Add source capability metadata.
3. Create an experiment/probe for one candidate source.
4. Promote to a real adapter only after clean normalized listings are proven.
5. Add one source at a time.
6. Keep browser-heavy or login-heavy sources behind an experimental flag.

## Phase 1 — Adapter registry foundation

### Goal

Remove source dispatch from scattered runtime logic and centralize it.

### Recommended new file

```text
adapters/registry.py
```

### Registry responsibilities

- normalize source names,
- return the correct adapter function,
- expose basic capability metadata,
- provide a single place to register new sources.

### Example shape

```python
from adapters import craigslist

ADAPTERS = {
    "craigslist": craigslist.search,
}

SOURCE_CAPABILITIES = {
    "craigslist": {
        "stable": True,
        "experimental": False,
        "requires_browser": False,
        "requires_login": False,
        "supports_location": True,
        "supports_radius": False,
        "supports_price_filter_in_url": False,
        "verticals": [
            "general_marketplace",
            "computer_parts",
            "vehicles",
            "home_theater",
        ],
    },
}


def normalize_source(source: str) -> str:
    return source.lower().strip().replace(" ", "_")


def get_adapter(source: str):
    return ADAPTERS.get(normalize_source(source))


def get_capabilities(source: str) -> dict:
    return SOURCE_CAPABILITIES.get(normalize_source(source), {})
```

Use names that fit the live repo.

### Acceptance criteria

- Existing Craigslist hunts still run.
- Logs still show clear source names.
- Discord behavior does not change.
- DB schema does not change.
- No new website is added in this first foundation commit.

## Phase 2 — Candidate source reconnaissance

Before implementing a new adapter, answer these questions:

| Question | Why it matters |
|---|---|
| Can search results be fetched with `requests`? | Avoids browser automation complexity |
| Is the content server-rendered? | Easier parsing |
| Are title, price, location, and link visible in search results? | Required for `Listing` normalization |
| Is JavaScript required? | May require Playwright |
| Is login required? | Moves source toward experimental |
| Are results stable enough for link dedupe? | Required for persistence |
| Does the source block normal requests? | May not be worth early effort |
| Are there legal/policy concerns? | Determines stable vs experimental |

## Phase 3 — Experiments before adapters

Do not wire unproven sites directly into production.

Recommended experiment location:

```text
experiments/adapters/
  swappa_probe.py
  ebay_probe.py
  microcenter_probe.py
  facebook_marketplace_probe.py
```

Experiment scripts should:

- take a simple search term,
- fetch or render one results page,
- print raw page status / title,
- extract candidate listing cards,
- print normalized candidate objects,
- avoid touching the production DB,
- avoid sending Discord alerts.

## Phase 4 — Promote to real adapter

Only promote a source to `adapters/` after it can reliably return normalized listings.

Promotion target:

```text
adapters/<source>.py
```

Adapter output must be:

```python
list[Listing]
```

Each listing should include at minimum:

```text
source
title
price
location
link
```

If a source lacks location, use `None` or a sensible source-specific value rather than faking it.

## Phase 5 — Source groups by vertical

Vulture should not show websites as one flat list forever.

Recommended source grouping:

```python
VERTICAL_SOURCES = {
    "computer_parts": ["craigslist", "swappa", "ebay"],
    "vehicles": ["craigslist", "cars_com", "autotrader"],
    "home_theater": ["craigslist", "facebook_marketplace"],
    "gaming": ["craigslist", "swappa", "ebay"],
    "general_marketplace": ["craigslist", "facebook_marketplace", "offerup"],
    "retail": ["microcenter", "bestbuy"],
}
```

This should eventually help the Discord UX.

Example:

- User asks for `RTX 3080 under 300`.
- Translator classifies as `computer_parts`.
- System defaults to computer-parts sources.
- Experimental sources are skipped unless enabled.

## Recommended adapter order

### 1. Registry and capability metadata

This comes before any new website.

### 2. Swappa or another structured electronics marketplace

Reason: likely relevant to electronics/gaming/computer parts and potentially more structured than Facebook Marketplace.

### 3. eBay experiment

Reason: high value, but anti-bot behavior has already been observed. Treat as experiment until proven stable.

### 4. Micro Center experiment

Reason: useful for retail deals, but previous plain requests hit blocking. Product-page or alternate endpoint research may be needed.

### 5. Facebook Marketplace experimental adapter

Reason: very high deal value but high complexity. Requires browser automation and likely session/cookie handling. Should not be the first production expansion.

### 6. Vehicle-specific sources

Cars.com, Autotrader, or similar sources could become useful once vehicle vertical logic is stronger.

## Adapter quality checklist

A new adapter is not ready until:

- it returns normalized `Listing` objects,
- it does not crash on empty results,
- it handles missing prices,
- it handles relative links,
- it has clear logging on failure,
- it does not send alerts directly,
- it does not write to the DB directly,
- it can be disabled by removing it from the registry or marking it experimental,
- it passes at least one smoke run.

## Smoke test pattern

For each adapter, add a simple smoke command or documented manual test.

Example manual test goal:

```text
Run one search for a known common term.
Confirm:
- nonzero listings if the source has results
- each listing has title and link
- price parsing does not crash
- output can flow through rules
- duplicate links do not alert again
```

## What not to do

Do not:

- put browser automation into the main runtime before needed,
- mix adapter expansion with database redesign,
- add Facebook Marketplace as a normal stable adapter immediately,
- use the LLM to decide listing matches,
- let adapters send Discord alerts directly,
- let adapters write to SQLite directly,
- add a new listing model field for every site-specific detail.

## Near-term Cursor prompt

```text
We are now treating the current runtime as Vulture 2.0. Do not redesign the architecture.

Goal: prepare the codebase for adding additional website adapters.

Current architecture:
- Discord commands create/manage hunts in SQLite
- main.py runs active DB hunts when VULTURE_HUNT_SOURCE=db
- Craigslist is the only stable adapter
- Runtime filtering must remain deterministic
- LLM translation is only for creating structured hunts, not for runtime listing decisions

Task:
1. Inspect the current adapter dispatch path in main.py and related files.
2. Add a small adapter registry module under adapters/ that maps source names to adapter search functions.
3. Move Craigslist dispatch into that registry without changing behavior.
4. Add simple source capability metadata for Craigslist.
5. Keep the existing Listing model unchanged unless absolutely necessary.
6. Add or update minimal smoke coverage / manual test instructions showing that existing Craigslist hunts still run.
7. Do not add a new website adapter yet.
8. Do not touch .env or secrets.
9. Do not change Discord command behavior unless required by the registry refactor.
10. Keep this as a small reviewable foundation commit.

After editing, summarize:
- files changed
- behavior preserved
- how to test on Windows
- how to test on Raven
- recommended commit message
```
