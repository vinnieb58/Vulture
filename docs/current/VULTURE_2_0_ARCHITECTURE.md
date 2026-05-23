# Vulture 2.0 Architecture

_Last refreshed: 2026-05-21_

## Purpose

Vulture is a modular deal-hunting system that searches supported websites for user-defined hunts, normalizes listings into a shared model, filters listings deterministically, deduplicates sightings, stores data in SQLite, and sends Discord alerts for new matches.

Vulture 2.0 adds Discord-based hunt management, DB-backed hunt storage, and LLM/rules-assisted translation of natural-language hunt intent into structured hunt definitions.

## Core principle

The LLM may help translate user intent into a structured hunt definition, but **runtime execution remains deterministic**.

The LLM should not decide whether a scraped listing passes or fails. Listing acceptance/rejection should come from predictable code in the rules engine.

## High-level architecture

```text
Discord user
  -> Discord slash command / intent
  -> discord_bot.py
  -> engine/command_router.py
  -> engine/hunt_service.py
  -> engine/llm_translator.py, when needed
  -> engine/hunt_repository.py
  -> SQLite hunts table

Scheduler / manual run
  -> main.py
  -> load active hunts
  -> adapter dispatch
  -> normalize listings
  -> deterministic rules
  -> SQLite listings table
  -> Discord notification
```

## Current module map

```text
vulture/
  adapters/
    craigslist.py
    registry.py                 # recommended next addition

  engine/
    command_router.py           # Discord command routing
    hunt_service.py             # hunt business logic
    hunt_repository.py          # DB interface for hunts
    llm_translator.py           # intent -> structured hunt translation
    rules.py                    # deterministic runtime filtering
    database.py                 # listings persistence / dedupe
    notifier.py                 # Discord alerting
    hunts.py                    # legacy YAML support

  models/
    listing.py                  # normalized listing model
    hunt.py                     # structured hunt model, if present in repo

  config/
    hunts.yaml                  # legacy v1.0 path only

  data/
    vulture.db                  # SQLite database

  logs/
    vulture.log                 # runtime logs

  discord_bot.py                # Discord control layer
  main.py                       # one hunt execution cycle
  requirements.txt
  .env                          # local secrets, not committed
  .env.example                  # placeholders only
```

## Runtime data flow

### 1. Hunt creation

```text
Discord input
  -> command router
  -> hunt service
  -> optional translator
  -> normalized hunt fields
  -> repository
  -> SQLite hunts table
```

### 2. Hunt execution

```text
main.py
  -> load active DB hunts
  -> build execution dict/context
  -> adapter search
  -> Listing objects
  -> rules.matches_rules()
  -> database dedupe by link
  -> save new listing
  -> Discord alert
```

## Hunt storage

### Current source of truth

The active Vulture 2.0 source of truth is the SQLite `hunts` table.

### Legacy source

`config/hunts.yaml` is legacy v1.0 support. It can remain for compatibility, but new development should not be designed around YAML as the primary hunt store.

## Listing storage

Listings are stored in SQLite. The key dedupe mechanism is the listing URL/link.

Current listing fields are expected to include at least:

```text
source
title
price
location
link
first_seen
```

Optional future fields may include:

```text
image_url
posted_at
seller
condition
raw
```

Do not expand the listing model casually. Add fields only when a second adapter proves they are necessary.

## Adapter architecture

### Current state

Craigslist is the only stable adapter.

### Desired near-term state

Add a registry so main execution does not hard-code one `if/elif` block per website.

Recommended file:

```text
adapters/registry.py
```

Recommended shape:

```python
from adapters import craigslist

ADAPTERS = {
    "craigslist": craigslist.search,
}

SOURCE_CAPABILITIES = {
    "craigslist": {
        "stable": True,
        "requires_browser": False,
        "supports_location": True,
        "supports_price_filter_in_url": False,
        "verticals": [
            "general_marketplace",
            "computer_parts",
            "vehicles",
            "home_theater",
        ],
    }
}


def get_adapter(source: str):
    normalized = source.lower().strip()
    return ADAPTERS.get(normalized)
```

The exact function names should follow the live codebase, but the principle should be preserved.

## Source capability metadata

Every adapter should eventually describe its capabilities.

Minimum useful fields:

| Field | Meaning |
|---|---|
| `stable` | Whether the adapter is safe for normal use |
| `experimental` | Whether it should be hidden or gated |
| `requires_browser` | Whether Playwright/Selenium is required |
| `requires_login` | Whether cookies/session/login are likely needed |
| `supports_location` | Whether the source can search by location |
| `supports_radius` | Whether search radius is supported |
| `supports_price_filter_in_url` | Whether price can be pushed into the search URL |
| `verticals` | Which hunt categories the source is useful for |

## Vertical model

Vulture should stay one program, not separate programs for cars, GPUs, appliances, etc.

The system should support vertical-aware translation and source selection.

Recommended vertical examples:

```text
computer_parts
vehicles
home_theater
gaming
general_marketplace
retail
```

Recommended future source grouping:

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

Do not implement all of this at once. The near-term task is to make the code ready for it.

## LLM translator rules

The translator may:

- detect vertical/category,
- extract max price,
- extract make/model/product family,
- expand obvious aliases,
- generate include/exclude keywords,
- set structured `adapter_options`,
- reject unsupported or invalid locations.

The translator must not:

- scrape websites,
- decide individual listing matches at runtime,
- replace deterministic rules,
- mutate `.env`,
- silently change persisted hunt behavior without confirmation where appropriate.

## Runtime rules philosophy

Runtime filtering should be:

- deterministic,
- explainable in logs,
- conservative where data is missing,
- stricter when a listing clearly violates a rule.

Example:

- If a RAM title clearly says `8GB` and the hunt requires `16GB`, reject it.
- If a title does not state capacity, allow it rather than guessing.
- If a vehicle title clearly says `headlight`, reject it as a parts listing.
- If a title is ambiguous, prefer logging and conservative behavior over risky assumptions.

## Secret handling

`.env` must remain local and uncommitted.

Code should read `.env` but never write or rewrite it.

`.env.example` may be committed with placeholders only.

## Stability constraints

Do not redesign the architecture unless explicitly chosen.

Do not replace deterministic filtering with LLM listing judgment.

Do not add a hard site like Facebook Marketplace before the adapter framework is ready.

Do not combine adapter expansion, database redesign, and Discord UX redesign into one commit.

Prefer small, reviewable changes.
