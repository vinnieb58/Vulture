# Concert Source Probe Report

_Last updated: 2026-07-01 (UTC)_

Probe-only reconnaissance for a future **Vulture Concerts** vertical. No production adapters, hunt commands, or marketplace behavior were changed.

Scripts live under `experiments/concerts/`. Sample artifacts are written to `artifacts/concerts/<source>/` (gitignored).

## Executive summary

| Recommendation | Source | Rationale |
|----------------|--------|-----------|
| **Promote (primary, with caveats)** | Ticketmaster Discovery API | Raven-validated: usable normalized fields, strong metro coverage; requires post-fetch genre filtering and `event_dedupe_key` alert suppression |
| **Promote (secondary)** | SeatGeek API | Clean JSON aggregator; numeric event IDs; useful cross-source dedupe (not yet Raven-validated) |
| **Keep probing** | Eventbrite browse (JSON-LD) | Works without credentials from cloud IP; good for city/genre/date browse; weak artist precision |
| **Reject for v1** | Songkick HTML, AXS web, Live Nation web, Houston venue pages | Blocked, empty CSR shells, or no parseable listings from this host |

**Top 1–2 for the first Concerts vertical:** **Ticketmaster Discovery API** (primary, with deterministic genre filter + event-level dedupe) + **SeatGeek API** (secondary cross-check). Use **Eventbrite** as a credential-free supplemental feed until SeatGeek is validated on Raven.

---

## Raven Ticketmaster evidence (2026-07-01)

Ticketmaster Discovery API was exercised on **Raven** with a live `TICKETMASTER_API_KEY`. Findings:

| Finding | Detail |
|---------|--------|
| **Normalized fields usable** | `provider_event_id`, artist/title, venue, city, state, `starts_at`, ticket URL, and `genre_or_classification` populate reliably from Discovery v2 |
| **`genre=rock` is noisy** | `classificationName=Rock` also returns **Pop**, **Country**, **R&B**, **Other**, and **Alternative** listings — not a pure rock/hard-rock/metal feed |
| **Duplicate same-show rows** | Ticketmaster can return the **same show twice** with **different `provider_event_id` values** (separate ticket types/offerings) |
| **Probe helpers** | `genre_counts` and `duplicate_groups` are emitted in probe stdout and saved artifacts |

### Observed duplicate same-show examples (Raven)

| Artist | Venue | Date | Issue |
|--------|-------|------|-------|
| Black Label Society | Boeing Center at Tech Port | 2026-09-05 | 2 rows, different provider IDs |
| Sevendust | Boeing Center at Tech Port | 2026-09-11 | 2 rows, different provider IDs |
| INOHA | Paper Tiger | 2026-09-20 | 2 rows, different provider IDs |
| Scene Queen | Paper Tiger | 2026-09-27 | 2 rows, different provider IDs |

**v1 implication:** use Ticketmaster as the primary ingest source, but **never alert on `dedupe_key` alone** — suppress duplicate same-show alerts with `event_dedupe_key` (see below).

---

## Proposed dedupe model

Two layers, both deterministic:

| Key | Formula | Use |
|-----|---------|-----|
| **`provider_dedupe_key`** (`dedupe_key` in probe output) | `{source}\|{provider_event_id}` | Track unique provider rows, cache raw fetches, audit which Ticketmaster IDs were seen |
| **`event_dedupe_key`** | `normalize(artist_or_title) + normalize(venue) + normalize(local starts_at)` → `event\|{sha1_16}` | Suppress duplicate **same-show** alerts when provider IDs differ |

**Alert rule:** fire at most **one alert per `event_dedupe_key` per hunt cycle** (or per watch window), even when multiple `provider_event_id` values exist for the same artist/venue/start.

**Cross-source rule:** when Ticketmaster + SeatGeek overlap, prefer matching on `event_dedupe_key` (or equivalent normalized artist + venue + local datetime), not provider IDs.

Probe helpers implementing this model live in `experiments/concerts/probe_common.py`:

- `make_provider_dedupe_key()`
- `make_event_dedupe_key()`
- `summarize_event_duplicates()` — groups with `count > 1`
- `count_by_genre()` — classification histogram

---

## Proposed deterministic filter model (no LLM)

Ticketmaster `classificationName` / genre is **advisory**, not authoritative for broad rock watches.

| Signal | Classifications | Broad rock/metal watch behavior |
|--------|-----------------|--------------------------------|
| **Positive** | Rock, Hard Rock, Metal, Alternative, Punk | Include by default |
| **Negative** | Pop, Country, R&B, Other | Exclude from broad rock watches |
| **Neutral** | Anything else / missing | Include only if other watch rules match (e.g. explicit artist) |

**Rules:**

1. **Broad genre watches** (`genre=rock`, metro scans): apply positive/negative classification filter after fetch. Do **not** trust Ticketmaster `classificationName=Rock` alone.
2. **Explicit artist watches** (Breaking Benjamin, Shinedown, Disturbed, etc.): **override** broad genre filtering — if the artist matches, include regardless of Pop/Country/R&B/Other classification.
3. **No LLM runtime filtering** — all include/exclude decisions must be deterministic (`classify_genre_signal()` in `probe_common.py`).

---

## Sources attempted

| Source | Script | Auth | Probe result | Verdict |
|--------|--------|------|--------------|---------|
| Ticketmaster Discovery API | `probe_ticketmaster.py` | `TICKETMASTER_API_KEY` | Raven: usable fields; noisy rock; duplicate same-show IDs | **Promote (with filters + event dedupe)** |
| SeatGeek API | `probe_seatgeek.py` | `SEATGEEK_CLIENT_ID` | Exit 2 without key on cloud | **Promote** (pending Raven run) |
| Bandsintown REST | `probe_bandsintown.py` | `BANDSINTOWN_APP_ID` | Exit 2 without key | Keep probing |
| Songkick | `probe_songkick.py` | None | HTTP 406, 0 events (cloud) | **Reject** (this IP) |
| Eventbrite | `probe_eventbrite.py` | Optional `EVENTBRITE_TOKEN`; browse works without | 3–20 events per query via JSON-LD (cloud) | Keep probing |
| Houston venue pages | `probe_static_venues.py` | None | 0 parseable events; CSR/403/redirect | **Reject** for v1 |
| AXS web | _(manual)_ | None | HTTP 403 | **Reject** |
| Live Nation web | _(manual)_ | None | HTTP 200 HTML shell, no event JSON | **Reject** |

---

## Capability matrix

| Capability | Ticketmaster API | SeatGeek API | Bandsintown API | Eventbrite browse | Songkick HTML | Venue pages |
|------------|------------------|--------------|-----------------|-------------------|---------------|-------------|
| City/metro search | Yes | Yes | Post-filter only | Yes | Blocked | No |
| Artist search | Yes | Yes | Yes (artist endpoint) | Weak | Blocked | No |
| Genre / rock-metal | Yes (noisy) | Yes | No | Yes | — | No |
| Date range | Yes | Yes | Client-side filter | Yes | — | No |
| Stable provider event ID | Yes | Yes | Yes | Yes | — | No |
| Same-show dedupe without provider ID | **Needs `event_dedupe_key`** | TBD | TBD | TBD | — | No |
| Venue, city, state, time, ticket URL | Yes (Raven) | Yes | Yes | Yes | Partial | No |
| Simple `requests` | Yes | Yes | Yes | Yes | No | No |
| Deterministic dedupe | **Provider strong; event-level required** | Strong | Good | Good | Unknown | Poor |

---

## Auth / API key requirements

| Env var | Source | Registration |
|---------|--------|--------------|
| `TICKETMASTER_API_KEY` | Ticketmaster Discovery v2 | https://developer.ticketmaster.com/ |
| `SEATGEEK_CLIENT_ID` | SeatGeek public API | https://seatgeek.com/account/develop |
| `BANDSINTOWN_APP_ID` | Bandsintown artist events | https://www.bandsintown.com/api/overview |
| `EVENTBRITE_TOKEN` | Eventbrite API v3 (optional) | https://www.eventbrite.com/platform/ |

All credentialed probe scripts fail safely with exit code **2** and a clear message when a required credential is missing.

---

## Sample commands

```bash
# Raven-validated Ticketmaster (requires key)
export TICKETMASTER_API_KEY=...
python experiments/concerts/probe_ticketmaster.py --city San Antonio --state TX --genre rock --limit 50
# stdout includes genre_counts and duplicate_groups

python experiments/concerts/probe_ticketmaster.py --artist "Breaking Benjamin" --city Houston --state TX

# Credential-free Eventbrite
python experiments/concerts/probe_eventbrite.py --city Houston --state TX --limit 8

# Missing-key validation
python experiments/concerts/probe_seatgeek.py --city Houston --state TX  # exit 2
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
  "raw_url": "https://www.ticketmaster.com/...",
  "dedupe_key": "ticketmaster|vvG1JZabc123",
  "event_dedupe_key": "event|a1b2c3d4e5f67890"
}
```

- `dedupe_key` = **provider_dedupe_key** (`source|provider_event_id`)
- `event_dedupe_key` = show-scoped key for alert suppression

Probe stdout (non-`--json`) also prints `genre_counts` and `duplicate_groups` when duplicates exist.

Artifacts are saved as `{label}_{YYYYMMDDTHHMMSSmmm}_{pid}_{uuid8}.json` to avoid same-second overwrite collisions.

---

## Blocking, rate limits, and browser issues

| Source | Observation |
|--------|-------------|
| **Ticketmaster Discovery API** | Works from Raven residential IP; rate limits per developer account |
| **SeatGeek API** | 403 without `client_id` |
| **Eventbrite browse** | HTTP 200 from cloud; no bot wall |
| **Songkick** | HTTP **406** from cloud IP |
| **Ticketmaster.com / AXS web** | HTTP **403** bot challenges |
| **Live Nation / venue pages** | CSR placeholders or 403 |

---

## Updated Ticketmaster v1 recommendation

**Use Ticketmaster Discovery API as the primary Concerts ingest source on Raven**, with these mandatory v1 guardrails:

1. **Fetch** by metro (`city`/`stateCode`) and/or explicit `keyword` artist watches; keep `classificationName` as a broad pre-filter only.
2. **Filter deterministically** after normalization: exclude Pop/Country/R&B/Other from broad rock watches; allow explicit artist watches to override.
3. **Dedupe for alerts** on `event_dedupe_key`, not `dedupe_key` — Ticketmaster duplicate provider rows for the same show are expected.
4. **Log** `genre_counts` and `duplicate_group_count` per hunt cycle for tuning.
5. **Do not** promote to production adapter until SeatGeek overlap is checked and repeated Raven hunt-cycle evidence is recorded.

---

## Files

| Path | Purpose |
|------|---------|
| `experiments/concerts/probe_common.py` | Shared CLI, normalization, dedupe, genre signals, artifacts |
| `experiments/concerts/probe_ticketmaster.py` | Ticketmaster Discovery API |
| `experiments/concerts/probe_seatgeek.py` | SeatGeek API |
| `experiments/concerts/probe_bandsintown.py` | Bandsintown artist events |
| `experiments/concerts/probe_songkick.py` | Songkick HTML recon |
| `experiments/concerts/probe_eventbrite.py` | Eventbrite JSON-LD (+ optional API) |
| `experiments/concerts/probe_static_venues.py` | Houston venue page recon |
| `tests/test_concert_probe_common.py` | Unit tests for shared probe helpers |
| `docs/current/CONCERT_SOURCE_PROBE_REPORT.md` | This report |

---

## Next steps (out of scope for this branch)

1. Run SeatGeek probe on Raven; compare overlap and `event_dedupe_key` collision rate with Ticketmaster.
2. Tune positive/negative genre lists from repeated Raven `genre_counts` histograms.
3. Only after repeated Raven evidence: design a Concerts vertical adapter (still separate from marketplace hunts).
