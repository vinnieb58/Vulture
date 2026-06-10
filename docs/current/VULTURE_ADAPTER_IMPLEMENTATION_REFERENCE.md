# Vulture Adapter Implementation Reference

_Last updated: 2026-06-10_

## Purpose

This file is a focused reference for how **Vulture** adapters should be designed, tested, registered, and promoted.

Platform context: [AVIARY_PROJECT_CONTEXT.md](AVIARY_PROJECT_CONTEXT.md). This document is Vulture-scoped only.

It is intentionally about **how Vulture works** and how future adapters should fit into the system. It does **not** document OfferUp-specific implementation details.

---

## Current adapter architecture principle

Vulture should remain one modular deal-hunting system, not a separate scraper per category or website.

The core pipeline is:

```text
hunt
  -> adapter
  -> normalized Listing
  -> deterministic rules
  -> dedupe by link
  -> SQLite storage
  -> Discord alert
```

Adapters are responsible for fetching and normalizing listings. They should not make business decisions, write to the database, or send alerts.

---

## Runtime roles that matter for adapters

### `main.py`

`main.py` runs one hunt cycle:

```text
load active hunts
  -> choose source adapter
  -> run adapter search
  -> apply rules
  -> dedupe/save listings
  -> send alerts
```

Adapters are called during this execution path.

### `discord_bot.py`

`discord_bot.py` is the command/control layer. It creates and manages hunts, but it should not contain website-specific scraping logic.

### Scheduler

The scheduler repeats `main.py`. It does not change adapter behavior. A scheduler run should be equivalent to manually running one `main.py` cycle.

---

## Adapter registry (implemented)

Adapter dispatch **is** centralized in `adapters/registry.py`. `main.py` calls `get_adapter(source)` — no scattered source `if/elif` chain.

Registry responsibilities:

- normalize source names,
- map source names to adapter search callables,
- expose capability metadata (`get_capabilities`, `get_source_metadata`),
- register runtime adapters in `_REGISTRY`,
- log and skip unknown sources gracefully.

**Runtime-registered sources (2026-06-10):** `bestbuy`, `carsdotcom`, `craigslist`, `microcenter`, `newegg`, `offerup`, `mercari`, `swappa`.

Classifications live in `_CAPABILITIES` (`status`: `stable` | `beta` | `experimental`). See `docs/current/CODEBASE_STATUS.md` for the current matrix.

Probe-only metadata may use `_PROBE_CAPABILITIES` without a `_REGISTRY` entry.

---

## Current adapter callable contract

The current registry/main path is still mostly Craigslist-shaped.

Current practical adapter call shape:

```python
adapter(query=..., city=..., limit=...)
```

This is acceptable for the adapter-registry foundation because it preserves current behavior, but it should not be treated as the final universal interface forever.

Future adapters may need one of these:

1. a shared adapter context object, such as `AdapterSearchContext`,
2. a standardized `search(hunt_context)` function,
3. small source-specific wrapper functions registered in the registry,
4. adapter options passed from the hunt model.

Do not redesign this interface casually. Multiple adapters already use this contract; evolve it only with a deliberate migration plan (e.g. shared `AdapterSearchContext`).

---

## Normalized Listing contract

Every production adapter should return existing `Listing` objects using the shared model.

Minimum useful fields:

```text
source
title
price
location
link
```

Guidelines:

- `source` should be the registry source name, such as `craigslist`.
- `title` should be human-readable and non-empty.
- `price` should be an integer when available, or handled according to the existing listing model behavior.
- `location` should be the source-provided location when available. Do not fake a location.
- `link` should be a stable canonical listing URL suitable for dedupe.

Do not add new Listing fields just because one website exposes extra data. Add model fields only when multiple adapters prove the need.

---

## What adapters must not do

Adapters must not:

- write to SQLite,
- send Discord notifications,
- call the LLM to decide whether a listing matches,
- mutate `.env`,
- depend on Discord command code,
- silently swallow major source/schema failures,
- create their own dedupe mechanism separate from the central database layer,
- alter hunt lifecycle state,
- change DB schema,
- perform broad side effects during import.

Adapters should be fetch/parse/normalize only.

---

## Runtime filtering philosophy

Runtime filtering remains deterministic.

The LLM may help translate user intent into structured hunt data, but the LLM should not judge scraped listings at runtime.

Correct flow:

```text
user intent
  -> translator creates structured hunt
  -> adapter fetches listings
  -> deterministic Python rules decide pass/fail
```

Incorrect flow:

```text
adapter listing
  -> ask LLM if it is a good match
  -> accept/reject listing
```

Rules should be conservative when source data is incomplete:

- reject when the title or price clearly violates a rule,
- allow ambiguous listings rather than guessing,
- log why a listing was filtered.

---

## Capability metadata

Every registered source should have metadata.

Recommended fields:

```python
{
    "stable": False,
    "experimental": True,
    "requires_browser": False,
    "requires_login": False,
    "supports_location": False,
    "location_control": "unverified",
    "supports_radius": False,
    "supports_price_filter_in_url": False,
    "verticals": [
        "general_marketplace",
        "computer_parts",
        "vehicles",
        "home_theater",
        "gaming",
        "retail",
    ],
}
```

Use truthful metadata. A source can be technically working but still experimental if location, auth, schema stability, or anti-bot behavior is not proven.

### Stable vs experimental

A source should be marked stable only when:

- requests or browser behavior is repeatable,
- listing fields are reliable,
- failure modes are handled,
- location behavior is understood if location matters,
- it has passed real runtime smoke tests,
- it does not cause excessive noise or bad alerts.

A source should stay experimental when:

- location is uncontrolled or unverified,
- schema could change easily,
- login/cookies might be needed,
- anti-bot behavior is uncertain,
- it has only been proven through a probe.

---

## Adapter implementation pattern

A production adapter should usually have this shape:

```python
def search_source(query: str, city: str | None = None, limit: int = 25) -> list[Listing]:
    """Search source and return normalized Listing objects."""
    try:
        html_or_json = fetch_results(query=query, city=city, limit=limit)
    except Exception:
        logger.exception("Source request failed")
        return []

    raw_items = parse_results(html_or_json)
    listings = []

    for item in raw_items:
        listing = normalize_item(item)
        if listing is not None:
            listings.append(listing)
        if len(listings) >= limit:
            break

    return listings
```

Preferred internal helpers:

```text
_build_url()
_fetch()
_parse_results()
_node_to_listing()
_clean_price()
_normalize_link()
```

Keep helpers small and testable.

---

## Error handling expectations

A bad adapter should not crash the whole hunt cycle.

Handle these cases gracefully:

- timeout,
- HTTP error,
- missing expected HTML/JSON block,
- schema change,
- malformed listing,
- missing title,
- missing price,
- bad URL,
- empty results.

Preferred behavior:

```text
log clearly
return []
continue other hunts/sources
```

Use `logger.warning()` for missing expected content or schema drift. Use `logger.error()` or `logger.exception()` for request failures or unexpected exceptions.

---

## Probe-before-promotion workflow

Do not add websites directly as production adapters.

Use this sequence:

```text
1. Create isolated probe under experiments/adapters/
2. Test fetch behavior
3. Confirm title/price/location/link availability
4. Confirm whether browser or login is required
5. Confirm schema stability risks
6. Only then promote to adapters/<source>.py
7. Register the source in adapters/registry.py
8. Keep source experimental until real runtime behavior is proven
```

Probe scripts must not:

- write to DB,
- send Discord alerts,
- import production registry unless absolutely harmless,
- change `.env`,
- change `main.py`,
- create persistent runtime state.

Probe scripts should print:

- query,
- final URL,
- HTTP status,
- page title if available,
- whether JavaScript/browser appears required,
- whether login appears required,
- listing count,
- rough normalized candidate dictionaries,
- observed listing locations.

---

## Recommended probe file location

```text
experiments/adapters/<source>_probe.py
```

For location-specific investigation:

```text
experiments/adapters/<source>_location_probe.py
```

Keep experiments separate from production adapter code until the behavior is proven.

---

## Promotion checklist

A probe can be promoted to `adapters/<source>.py` only when it proves:

- results can be fetched reliably,
- no login is required, or login/session complexity is explicitly accepted,
- browser automation is not required, or the source is intentionally browser-based,
- listing title is available,
- listing price is available or safely nullable,
- listing link is available and stable,
- listing location is available or honestly marked unsupported,
- the source returns normalized `Listing` objects,
- empty results do not crash,
- malformed records do not crash,
- failure modes are logged,
- registry metadata is accurate,
- existing Craigslist behavior still works.

---

## Source location behavior

Location support must be proven, not assumed.

A source can return local-looking listings for several reasons:

- explicit URL city parameter,
- latitude/longitude parameter,
- radius parameter,
- cookie/session state,
- account setting,
- IP geolocation,
- prior browser state,
- server default.

Do not mark `supports_location=True` unless the adapter can intentionally request at least two different target cities and produce different location-appropriate results.

Suggested proof targets:

```text
Houston, TX
Dallas, TX
A non-Texas control city
```

Capability metadata should distinguish:

```python
"supports_location": False,
"location_control": "unverified",
```

from:

```python
"supports_location": True,
"location_control": "verified",
```

---

## Browser automation policy

Prefer `requests` plus HTML/JSON parsing when possible.

Use Playwright/Selenium only when:

- the source cannot be fetched or parsed with requests,
- the source is important enough to justify complexity,
- login/session/browser state is explicitly accepted,
- disk/RAM impact on Raven is acceptable,
- adapter is clearly marked experimental at first.

Raven is a lightweight runtime box. Avoid turning it into a browser farm.

---

## Source selection and verticals

Future Vulture should choose sources by vertical, not a flat website list.

Example future grouping:

```python
VERTICAL_SOURCES = {
    "computer_parts": ["craigslist", "offerup", "ebay"],
    "vehicles": ["craigslist", "offerup", "cars_com", "autotrader"],
    "home_theater": ["craigslist", "offerup", "facebook_marketplace"],
    "gaming": ["craigslist", "offerup", "swappa", "ebay"],
    "general_marketplace": ["craigslist", "offerup", "facebook_marketplace"],
    "retail": ["microcenter", "bestbuy"],
}
```

Do not implement all of this at once. Keep registry and capability metadata ready for it.

---

## Default-source rule

Do not make an experimental adapter a default source for normal translated hunts.

Safe:

```python
source_sites = ["experimental_source"]
```

when manually selected for testing.

Unsafe:

```text
/hunt rtx 3080 under 300
```

silently selecting an experimental source with unverified location behavior.

An adapter may be merged into `main` while experimental, but it should not become a default source until stable enough.

---

## Testing adapters

### Registry smoke test

```bash
python - <<'PY'
from adapters.registry import get_adapter, get_capabilities, list_sources

print(list_sources())
print(get_adapter("craigslist"))
print(get_capabilities("craigslist"))
PY
```

### Direct adapter smoke test

```bash
python - <<'PY'
from adapters.<source> import search_<source>

results = search_<source>("rtx 3080", city="houston", limit=5)
print("Count:", len(results))
for result in results:
    print(result)
PY
```

### Compile check

```bash
python -m compileall -q adapters experiments engine models main.py discord_bot.py
```

### Existing validation

```bash
python scripts/validate_step1.py
```

### One hunt cycle

```bash
VULTURE_HUNT_SOURCE=db python main.py
```

### Raven deploy/test

```bash
cd ~/projects/vulture
bash scripts/update_raven.sh
```

For non-interactive branch deployment:

```bash
BRANCH=<branch-name> bash scripts/update_raven.sh
```

See `docs/current/RAVEN_SYSTEMD_RUNTIME.md` for systemd units, verification commands, and reboot survival notes.

---

## Logging expectations

Adapter logs should answer:

- which source ran,
- what query was requested,
- what city/location was requested,
- whether location control is verified,
- how many raw candidates were found,
- how many normalized listings were returned,
- what actual listing locations were observed,
- why a source returned zero results,
- whether a schema changed.

Do not log secrets, cookies, tokens, or full session headers.

---

## Current good adapter PR boundaries

Good adapter PR:

```text
adapters/<source>.py
adapters/registry.py
experiments/adapters/<source>_probe.py, if useful
small docs/test note
```

Bad adapter PR:

```text
adapter + DB redesign
adapter + Discord UX rewrite
adapter + scheduler changes
adapter + .env handling
adapter + listing model expansion without proof
adapter + LLM runtime filtering
```

Keep adapter PRs small and reviewable.

---

## Merge strategy for experimental adapters

It is acceptable to merge an experimental adapter into `main` when:

- it is not default-selected,
- metadata marks it experimental,
- failure modes are safe,
- existing stable adapters still work,
- no schema or Discord behavior changes were made,
- limitations are documented.

Do not keep a working experimental adapter on a long-running branch forever just because one capability, such as location targeting, still needs improvement. Merge the guarded capability, then improve it with follow-up branches.

---

## Recommended future adapter tasks

Likely future branches:

```text
feature/<source>-location-targeting
feature/<source>-fixture-tests
feature/adapter-context-interface
feature/vertical-source-selection
feature/experimental-source-toggle
```

Do not start `adapter-context-interface` until at least two adapters prove the current `query/city/limit` interface is too limiting.

---

## Most important rules

1. Adapters fetch and normalize only.
2. Runtime filtering stays deterministic.
3. Registry owns source dispatch.
4. Capability metadata must tell the truth.
5. Probe before production adapter.
6. Experimental adapters may exist in `main`, but should not be default-selected.
7. Do not fake location support.
8. Do not let one bad source crash the whole hunt cycle.
9. Do not mix adapter work with DB, Discord, scheduler, or LLM redesigns.
10. Preserve Craigslist behavior as the baseline regression test.
