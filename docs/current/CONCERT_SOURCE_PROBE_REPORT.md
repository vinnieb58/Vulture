# Concert Source Probe Report

_Last updated: 2026-07-01 (UTC) — final hardening pass_

Probe-only reconnaissance for a future **Vulture Concerts** vertical. No production adapters, hunt commands, or marketplace behavior were changed.

Scripts live under `experiments/concerts/`. Sample artifacts are written to `artifacts/concerts/<source>/` (gitignored).

---

## Final recommendations

| Role | Source | Verdict |
|------|--------|---------|
| **Primary** | Ticketmaster Discovery API | **Use for v1** with deterministic genre filter + `event_dedupe_key` alert suppression |
| **Secondary** | SeatGeek API | **Use for cross-check/dedupe** after performer-ID workflow; not sufficient alone for broad rock watches |
| **Supplemental** | Eventbrite browse | Credential-free; clubs/fests; weak artist precision |
| **Defer / reject** | Songkick HTML, AXS, Live Nation web, Houston venue pages, Bandsintown-only metro | Blocked, CSR shells, or artist-scoped-only |

**Blockers before Vulture Concerts v1 implementation:**

1. Re-run improved `probe_seatgeek.py --experiment --days 365` on Raven and confirm performer-ID artist watch viability.
2. Tune positive/negative genre/taxonomy lists from Raven histograms.
3. Define hunt-cycle alert policy using `event_dedupe_key` (not provider ID).
4. SeatGeek overlap study vs Ticketmaster for Texas metros (dedupe collision rate).

---

## 1. Raven Ticketmaster validation

### Strengths

- Discovery v2 returns **usable normalized fields** on Raven: `provider_event_id`, artist/title, venue, city, state, `starts_at`, ticket URL, classification/genre.
- Supports metro search (`city`/`stateCode`), artist keyword, `classificationName`, and date range.
- Best coverage for arena/amphitheater rock/hard-rock/metal tours in Texas metros.
- Provider IDs are stable per Ticketmaster row.

### Weaknesses

- **`classificationName=Rock` is noisy** — Raven results include **Pop**, **Country**, **R&B**, **Other**, and **Alternative** alongside rock listings. Not a pure rock feed.
- **Duplicate same-show rows** — Ticketmaster can return the **same show twice** with **different `provider_event_id` values** (separate ticket types/offerings).

### Observed duplicate examples (Raven)

| Artist | Venue | Date | Issue |
|--------|-------|------|-------|
| Black Label Society | Boeing Center at Tech Port | 2026-09-05 | 2 rows, different provider IDs |
| Sevendust | Boeing Center at Tech Port | 2026-09-11 | 2 rows, different provider IDs |
| INOHA | Paper Tiger | 2026-09-20 | 2 rows, different provider IDs |
| Scene Queen | Paper Tiger | 2026-09-27 | 2 rows, different provider IDs |

### v1 suitability: **Primary source (with guardrails)**

Use Ticketmaster as the main ingest, but:

1. Treat classification as **advisory** — apply deterministic post-fetch filter (see §5).
2. Suppress alerts on **`event_dedupe_key`**, not `dedupe_key`.
3. Log `genre_counts` and `duplicate_group_count` each cycle.

---

## 2. Raven SeatGeek validation

### Prior Raven findings (original probe)

| Query style | Result |
|-------------|--------|
| `venue.city` + `venue.state` | **Works** — returns events with stable numeric IDs, venue/city/date/url |
| `taxonomies.name=rock` | **0 results** |
| `q=Breaking Benjamin` / `Shinedown` / `Disturbed` | **0 results** |

### Root cause analysis (probe limitation, not necessarily API limitation)

The original `probe_seatgeek.py` had two probe-design gaps:

1. **Wrong taxonomy for concerts** — SeatGeek's top-level music taxonomy is `concert`, not `rock`. Sub-genres like `rock` may exist under `concert` but `taxonomies.name=rock` alone returned zero. SeatGeek docs show filtering with `taxonomies.name=concert` and adding multiple `taxonomies.name` params (not comma-separated).
2. **Artist search skipped performer resolution** — SeatGeek docs recommend `/2/performers?q={artist}&taxonomies.name=concert` to obtain `performers.id` / `performers.slug`, then `/2/events?performers.id={id}`. Using `q` on `/2/events` alone is unreliable for artist watches.

### Improved probe (this branch)

`probe_seatgeek.py` now:

1. Looks up performers via `/2/performers` before event queries.
2. Tries multiple strategies per request and records counts/params in artifacts:
   - `city_only`, `city_concert_taxonomy`
   - `events_q_artist`, `events_q_artist_city`
   - `performers_id`, `performers_slug`, `performers_id_concert_taxonomy`
   - genre/taxonomy variants
3. Supports `--experiment` matrix:

```bash
export SEATGEEK_CLIENT_ID=...
python experiments/concerts/probe_seatgeek.py --experiment --days 365 --limit 20
```

**Artists:** Breaking Benjamin, Shinedown, Disturbed, Three Days Grace, Papa Roach  
**Cities:** Houston, Dallas, Austin, San Antonio  
**Window:** 365 days

Experiment output is saved to `payload.rows[]` with per-strategy `result_count`, `params`, and `api_total`.

### Expected Raven outcomes (to confirm on next run)

| Workflow | Expected viability |
|----------|-------------------|
| **Artist watch** via `performers.id` | **Likely viable** — if performer resolves, events should appear even when `q`-only returned 0 |
| **Broad rock watch** via `taxonomies.name=concert` + city | **Partially viable** — returns concerts but mixes genres; sports/comedy/theater appear in city-only searches |
| **Rock-only** via `taxonomies.name=rock` | **Unlikely** as sole filter — use concert + deterministic taxonomy token filter |

### Strengths

- Clean JSON, stable numeric `id`, good venue/city/datetime/url normalization.
- Performer endpoint enables artist-scoped discovery when two-step flow is used.
- Useful for **cross-source dedupe** against Ticketmaster.

### Weaknesses

- City-only search is **high-noise** (sports, comedy, theater mixed in).
- `taxonomies.name=rock` alone is not a practical broad filter.
- `q` on events alone is **not** a reliable artist watch.
- Comma-separated taxonomy values silently return zero (SeatGeek API quirk).

### v1 suitability: **Secondary source**

Use SeatGeek to cross-check Ticketmaster coverage and dedupe collisions. Do **not** rely on SeatGeek alone for broad rock watches without `taxonomies.name=concert` + deterministic taxonomy filtering.

---

## 3. Artifact collision investigation

### Observed on Raven (stale run)

```
artifacts/concerts/seatgeek/probe_20260701T204445Z.json
```

Format: `{label}_{YYYYMMDDTHHMMSSZ}.json` — second-level timestamp with trailing `Z`.

### Root cause

**Raven executed a commit before the collision-resistant filename landed** (pre-`artifact_filename()` helper, commit `e6eb967`). Investigation confirmed:

| Check | Result |
|-------|--------|
| `probe_seatgeek.py` uses shared `save_artifact()`? | **Yes** — imports from `probe_common.py` |
| Any probe bypasses helper? | **No** — all six probes call `save_artifact()` |
| Old format still in current code? | **No** |

The bug was **stale Raven checkout**, not a SeatGeek-specific code path.

### Fix (current branch)

**Before:** `probe_20260701T204445Z.json`  
**After:** `probe_20260701T174415433_14394_67f80a94.json`

Format: `{label}_{YYYYMMDDTHHMMSSmmm}_{pid}_{uuid8}.json`

- `artifact_filename()` validates against `ARTIFACT_FILENAME_RE`
- Rejects legacy `...Z.json` pattern via `LEGACY_ARTIFACT_FILENAME_RE`
- Artifacts include `artifact_filename_version: 2` and `artifact_basename`
- `saved_at` in JSON body remains ISO UTC (separate from filename)

### Verification

```bash
python experiments/concerts/probe_eventbrite.py --city Houston --state TX --limit 3
# artifact=artifacts/concerts/eventbrite/probe_YYYYMMDDTHHMMSSmmm_PID_UUID8.json
```

---

## 4. Duplicate analysis

### Ticketmaster

| Pattern | Behavior | Mitigation |
|---------|----------|------------|
| Same show, multiple provider IDs | Common (observed 4 pairs on Raven) | Alert on `event_dedupe_key` only |
| Same provider ID resurfacing | Stable `dedupe_key` | Provider row tracking / seen-cache |
| Cross-source overlap with SeatGeek | Expected for major shows | `event_dedupe_key` or normalized artist+venue+datetime |

Probe output: `duplicate_groups[]` groups by `event_dedupe_key` with `provider_event_ids` list.

### SeatGeek

| Pattern | Expected behavior |
|---------|-------------------|
| Same show, multiple provider IDs | Possible — verify on Raven experiment |
| City search duplicates | Same artist/venue/datetime may appear once per query strategy — dedupe before alert |
| Cross-source with Ticketmaster | Use `event_dedupe_key` for alert suppression; retain both provider IDs |

---

## 5. Classification analysis

### Ticketmaster `genre_or_classification`

| Field reliability | Guidance |
|-------------------|----------|
| `provider_event_id` | **Reliable** — unique per TM row |
| Artist/title, venue, city, state, starts_at, ticket URL | **Reliable** on Raven |
| `classificationName` / genre | **Advisory only** for broad rock watches |

**Deterministic filter (no LLM):**

| Signal | Classifications | Broad rock watch |
|--------|-----------------|------------------|
| Positive | Rock, Hard Rock, Metal, Alternative, Punk | Include |
| Negative | Pop, Country, R&B, Other | Exclude |
| Neutral | Unknown | Include only via explicit artist watch |
| **Artist watch override** | Any | Include when artist matches watchlist |

Implemented in `classify_genre_signal()` (`probe_common.py`).

### SeatGeek `genre_or_classification` (comma-joined taxonomies)

| Field reliability | Guidance |
|-------------------|----------|
| Numeric `id` | **Reliable** |
| Venue/city/datetime/url | **Reliable** |
| Taxonomy tokens (`concert, rock, sports, comedy`) | **Advisory** — use token-level filter |

**City-only search noise (Raven):** includes **sports**, **comedy**, **theater** alongside concerts.

**Deterministic filter (no LLM):**

| Signal | Taxonomy tokens | Broad concert/rock watch |
|--------|-----------------|--------------------------|
| Positive | concert, rock, metal, indie, alternative, punk | Include |
| Negative | sports, nfl, nba, comedy, theater, family | Exclude |
| Neutral | other tokens | Case-by-case; prefer artist watch |

Implemented in `classify_seatgeek_taxonomies()` and `count_by_taxonomy_token()`.

---

## Proposed dedupe model

| Key | Formula | Use |
|-----|---------|-----|
| `provider_dedupe_key` (`dedupe_key`) | `{source}\|{provider_event_id}` | Row tracking, seen-cache |
| `event_dedupe_key` | `normalize(artist) + normalize(venue) + normalize(local starts_at)` → `event\|{sha1_16}` | **Alert suppression** for same-show duplicates |

**Alert rule:** at most one alert per `event_dedupe_key` per hunt cycle, even when multiple provider IDs exist.

---

## Sources summary

| Source | Script | Auth | Verdict |
|--------|--------|------|---------|
| Ticketmaster Discovery API | `probe_ticketmaster.py` | `TICKETMASTER_API_KEY` | **Primary** |
| SeatGeek API | `probe_seatgeek.py` | `SEATGEEK_CLIENT_ID` | **Secondary** |
| Eventbrite browse | `probe_eventbrite.py` | Optional token | Supplemental |
| Bandsintown | `probe_bandsintown.py` | `BANDSINTOWN_APP_ID` | Artist watchlists only |
| Songkick / venues / AXS / LN web | various | None | Reject for v1 |

---

## Sample commands

```bash
# Ticketmaster (Raven-validated)
export TICKETMASTER_API_KEY=...
python experiments/concerts/probe_ticketmaster.py --city San Antonio --state TX --genre rock --days 365 --limit 50

# SeatGeek multi-strategy + experiment matrix
export SEATGEEK_CLIENT_ID=...
python experiments/concerts/probe_seatgeek.py --artist "Shinedown" --days 365
python experiments/concerts/probe_seatgeek.py --experiment --days 365 --limit 20

# Credential-free Eventbrite
python experiments/concerts/probe_eventbrite.py --city Houston --state TX --limit 8
```

---

## Normalized output shape

```json
{
  "source": "ticketmaster",
  "provider_event_id": "vvG1JZabc123",
  "artist_or_title": "Sevendust",
  "venue": "Boeing Center at Tech Port",
  "city": "San Antonio",
  "state": "TX",
  "starts_at": "2026-09-11T20:00:00Z",
  "ticket_url": "https://www.ticketmaster.com/...",
  "genre_or_classification": "Rock",
  "dedupe_key": "ticketmaster|vvG1JZabc123",
  "event_dedupe_key": "event|a1b2c3d4e5f67890"
}
```

Non-`--json` stdout also includes `genre_counts`, `duplicate_group_count`, `duplicate_groups`, and (SeatGeek experiment) `taxonomy_token_counts`.

---

## Files

| Path | Purpose |
|------|---------|
| `experiments/concerts/probe_common.py` | Shared helpers, dedupe, genre/taxonomy signals, artifact v2 filenames |
| `experiments/concerts/probe_ticketmaster.py` | Ticketmaster Discovery API |
| `experiments/concerts/probe_seatgeek.py` | SeatGeek multi-strategy + `--experiment` matrix |
| `experiments/concerts/probe_eventbrite.py` | Eventbrite JSON-LD |
| `experiments/concerts/probe_bandsintown.py` | Bandsintown artist events |
| `experiments/concerts/probe_songkick.py` | Songkick HTML recon |
| `experiments/concerts/probe_static_venues.py` | Houston venue pages |
| `tests/test_concert_probe_common.py` | Unit tests for shared helpers |
| `docs/current/CONCERT_SOURCE_PROBE_REPORT.md` | This report |

---

## Next steps (out of scope)

1. Raven: `python experiments/concerts/probe_seatgeek.py --experiment --days 365` — paste `payload.rows` summary into SESSION_LOG.
2. Raven: compare Ticketmaster vs SeatGeek `event_dedupe_key` overlap for Texas metros.
3. Only after repeated Raven hunt-cycle evidence: Concerts vertical adapter design.
