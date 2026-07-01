# Concert Source Probe Report

_Last updated: 2026-07-01 (UTC)_

Probe-only reconnaissance for a future **Vulture Concerts** vertical. No production adapters, hunt commands, or marketplace behavior were changed.

Scripts live under `experiments/concerts/`. Sample artifacts are written to `artifacts/concerts/<source>/` (gitignored).

## Executive summary

| Recommendation | Source | Rationale |
|----------------|--------|-----------|
| **Promote (primary)** | Ticketmaster Discovery API | Official JSON API; stable `event.id`; city/state, keyword, classification, and date-range filters; best fit for arena/amphitheater rock/metal tours |
| **Promote (secondary)** | SeatGeek API | Clean JSON aggregator; numeric event IDs; `q`, venue city/state, taxonomy, and datetime filters; useful cross-source dedupe |
| **Keep probing** | Eventbrite browse (JSON-LD) | Works without credentials from cloud IP; good for city/genre/date browse; weak artist precision |
| **Reject for v1** | Songkick HTML, AXS web, Live Nation web, Houston venue pages | Blocked, empty CSR shells, or no parseable listings from this host |

**Top 1–2 for the first Concerts vertical:** **Ticketmaster Discovery API** + **SeatGeek API** (both need free developer credentials). Use **Eventbrite** as a credential-free supplemental feed until keys are provisioned on Raven.

---

## Sources attempted

| Source | Script | Auth | Cloud probe result | Verdict |
|--------|--------|------|-------------------|---------|
| Ticketmaster Discovery API | `probe_ticketmaster.py` | `TICKETMASTER_API_KEY` | Exit 2, clear missing-key message (HTTP 401 without key) | **Promote** |
| SeatGeek API | `probe_seatgeek.py` | `SEATGEEK_CLIENT_ID` | Exit 2, clear missing-key message (HTTP 403 without client) | **Promote** |
| Bandsintown REST | `probe_bandsintown.py` | `BANDSINTOWN_APP_ID` | Exit 2, clear missing-key message (HTTP 401) | Keep probing |
| Songkick | `probe_songkick.py` | None | HTTP 406, 0 events | **Reject** (this IP) |
| Eventbrite | `probe_eventbrite.py` | Optional `EVENTBRITE_TOKEN`; browse works without | 3–20 events per query via JSON-LD | Keep probing |
| Houston venue pages | `probe_static_venues.py` | None | 0 parseable events; CSR/403/redirect | **Reject** for v1 |
| AXS web | _(manual)_ | None | HTTP 403 | **Reject** |
| Live Nation web | _(manual)_ | None | HTTP 200 HTML shell, no `__NEXT_DATA__` / ld+json events | **Reject** |

---

## Capability matrix

| Capability | Ticketmaster API | SeatGeek API | Bandsintown API | Eventbrite browse | Songkick HTML | Venue pages |
|------------|------------------|--------------|-----------------|-------------------|---------------|-------------|
| City/metro search | Yes (`city`, `stateCode`, geo) | Yes (`venue.city`, `venue.state`) | Post-filter only | Yes (browse slug) | Blocked | No |
| Artist search | Yes (`keyword`) | Yes (`q`) | Yes (artist endpoint) | Weak (slug/substring) | Blocked | No |
| Genre / rock-metal | Yes (`classificationName`) | Yes (`taxonomies.name`) | No | Yes (`music/rock`, `music/metal`) | — | No |
| Date range | Yes (`startDateTime` / `endDateTime`) | Yes (`datetime_utc.gte/lte`) | Client-side filter | Yes (`start_date` / `end_date` query params) | — | No |
| Stable event ID | Yes (`id`) | Yes (numeric `id`) | Yes (`id` / offer URL) | Yes (numeric ID in ticket URL) | Would be `/concerts/{id}` | No |
| Venue, city, state, time, ticket URL | Yes | Yes | Yes | Yes (date often date-only) | Partial | No |
| Simple `requests` | Yes | Yes | Yes | Yes | No (406) | No (CSR/403) |
| Browser automation required | No | No | No | No | Likely from residential IP | Yes for Live Nation venues |
| Deterministic dedupe | **Strong** | **Strong** | Good | Good (`eventbrite\|{id}`) | Unknown | Poor |

---

## Auth / API key requirements

| Env var | Source | Registration |
|---------|--------|--------------|
| `TICKETMASTER_API_KEY` | Ticketmaster Discovery v2 | https://developer.ticketmaster.com/ |
| `SEATGEEK_CLIENT_ID` | SeatGeek public API | https://seatgeek.com/account/develop |
| `BANDSINTOWN_APP_ID` | Bandsintown artist events | https://www.bandsintown.com/api/overview |
| `EVENTBRITE_TOKEN` | Eventbrite API v3 (optional) | https://www.eventbrite.com/platform/ |

All probe scripts fail safely with exit code **2** and a clear message when a required credential is missing. No secrets are read from files or hardcoded.

---

## Sample commands and result counts

Run from repo root (or `experiments/concerts/`). Date window defaults to **today → +180 days**.

### Credential-free probes (executed 2026-07-01)

```bash
python experiments/concerts/probe_eventbrite.py --city Houston --state TX --limit 8
# normalized_count=8

python experiments/concerts/probe_eventbrite.py --genre rock --city Austin --state TX --limit 5
# normalized_count=5 (e.g. RippleFest Texas 2026)

python experiments/concerts/probe_eventbrite.py --genre metal --city Houston --state TX --limit 5
# normalized_count=3 (e.g. Destroying Texas Fest 20)

python experiments/concerts/probe_eventbrite.py --city Dallas --state TX --limit 5
# normalized_count=5

python experiments/concerts/probe_eventbrite.py --artist "Breaking Benjamin" --city Houston --state TX --limit 5
# normalized_count=2 after substring filter; browse slug returns unrelated "Breaking Bad" trivia — not true artist matches

python experiments/concerts/probe_songkick.py --city Houston --state TX
# normalized_count=0, HTTP 406 blocked

python experiments/concerts/probe_static_venues.py
# normalized_count=0 across 4 venues (CSR placeholders, 403, or consent redirect)
```

### Credentialed probes (missing-key validation)

```bash
python experiments/concerts/probe_ticketmaster.py --city Houston --state TX --artist "Breaking Benjamin"
# exit 2: Missing TICKETMASTER_API_KEY

python experiments/concerts/probe_seatgeek.py --city Houston --state TX --artist "Shinedown"
# exit 2: Missing SEATGEEK_CLIENT_ID

python experiments/concerts/probe_bandsintown.py --artist "Disturbed"
# exit 2: Missing BANDSINTOWN_APP_ID
```

### Suggested Raven follow-up (with keys)

```bash
export TICKETMASTER_API_KEY=...
python experiments/concerts/probe_ticketmaster.py --city Houston --state TX --genre rock --artist "Breaking Benjamin"

export SEATGEEK_CLIENT_ID=...
python experiments/concerts/probe_seatgeek.py --city Houston --state TX --genre rock --artist "Shinedown"
```

---

## Normalized output shape

Every probe emits:

```json
{
  "source": "eventbrite",
  "provider_event_id": "1977513145050",
  "artist_or_title": "RippleFest Texas 2026 - 2 DAY PASS",
  "venue": "The Far Out Lounge & Stage",
  "city": "Austin",
  "state": "TX",
  "starts_at": "2026-09-18",
  "ticket_url": "https://www.eventbrite.com/e/...",
  "genre_or_classification": "rock",
  "raw_url": "https://www.eventbrite.com/e/...",
  "dedupe_key": "eventbrite|1977513145050"
}
```

`dedupe_key` is `{source}|{provider_event_id}` when an ID exists; otherwise a short hash of title/venue/start.

---

## Blocking, rate limits, and browser issues

| Source | Observation |
|--------|-------------|
| **Ticketmaster Discovery API** | No blocking observed on auth failure path; rate limits apply per developer account (not measured here) |
| **SeatGeek API** | 403 without `client_id`; no rate-limit data without credentials |
| **Bandsintown** | 401 without `app_id` |
| **Eventbrite browse** | HTTP 200 from cloud; ~8–20 JSON-LD events per browse page; no bot wall |
| **Eventbrite API** | Destination search POST returns 401 without session/CSRF; optional `EVENTBRITE_TOKEN` path not tested (no token in env) |
| **Songkick** | HTTP **406** with empty body from cloud IP |
| **Ticketmaster.com / AXS** | HTTP **403** identity/bot challenges on web search |
| **Live Nation web** | HTTP 200 but no embedded event JSON; search API path returns HTML shell |
| **713 Music Hall / House of Blues** | HTTP 200 **"THIS PAGE IS STILL IN SOUND CHECK"** CSR placeholder |
| **White Oak Music Hall** | HTTP **403** Forbidden |
| **Cynthia Woods Mitchell Pavilion** | Redirect to consent/tracking page; no event listings |

---

## Dedupe viability

- **Ticketmaster + SeatGeek:** Both expose stable numeric/string IDs and canonical ticket URLs. Cross-source dedupe can use `(artist_normalized, venue_normalized, starts_at_utc)` with source-specific IDs retained. Strongest pair for production.
- **Eventbrite:** Stable numeric IDs in ticket URLs; good single-source dedupe; overlaps with Ticketmaster/SeatGeek will need fuzzy title+datetime matching (many listings are parties/fests, not arena tours).
- **Bandsintown:** Artist-scoped; good for tour-date alerts per artist; weak for metro-wide discovery.
- **Songkick / venues / AXS / Live Nation web:** Not viable from current probe evidence.

---

## Per-source notes

### Ticketmaster Discovery API — **Promote**

Best-aligned with major rock/hard rock/metal tours (Toyota Center, Cynthia Woods, etc.). Supports `classificationName` (Rock, Hard Rock, Metal), `keyword`, `city`/`stateCode`, `startDateTime`/`endDateTime`, and `radius` when using geo params. Requires API key before any data returns.

### SeatGeek API — **Promote**

Secondary aggregator with similar filter surface (`q`, `venue.city`, `venue.state`, `taxonomies.name`, datetime range). Free client ID. Good for coverage gaps and dedupe cross-checks against Ticketmaster.

### Eventbrite — **Keep probing**

Only source that returned real normalized events without credentials. City browse (`/b/tx--houston/music/`), genre browse (`/b/tx--austin/music/rock/`), and date query params work. **Artist slug search is unreliable** — `breaking-benjamin` returns "Breaking Bad" trivia and unrelated titles. Treat as supplemental (clubs/fests), not primary arena-tour coverage.

### Bandsintown — **Keep probing**

Artist-tour API is simple and useful for watchlist-driven hunts, but requires `app_id`, has no metro/genre search, and is not sufficient alone for a metro Concerts vertical.

### Songkick — **Reject** (for now)

Public metro and artist pages returned HTTP 406 from the cloud probe host. Re-test from Raven residential IP before reconsidering.

### AXS / Live Nation — **Reject** (for now)

No public JSON API probed. Web surfaces blocked or client-rendered without parseable event payloads. Live Nation venue subdomains (713 Music Hall, HOB) mirror the CSR placeholder problem.

### Houston venue pages — **Reject** for v1

Defer venue-specific adapters until Ticketmaster/SeatGeek coverage is validated on Raven. Revisit with Playwright on residential IP if pavilion/warehouse shows are systematically missing from APIs.

---

## Files added

| Path | Purpose |
|------|---------|
| `experiments/concerts/probe_common.py` | Shared CLI, normalization, artifacts |
| `experiments/concerts/probe_ticketmaster.py` | Ticketmaster Discovery API |
| `experiments/concerts/probe_seatgeek.py` | SeatGeek API |
| `experiments/concerts/probe_bandsintown.py` | Bandsintown artist events |
| `experiments/concerts/probe_songkick.py` | Songkick HTML recon |
| `experiments/concerts/probe_eventbrite.py` | Eventbrite JSON-LD (+ optional API) |
| `experiments/concerts/probe_static_venues.py` | Houston venue page recon |
| `docs/current/CONCERT_SOURCE_PROBE_REPORT.md` | This report |

`.gitignore` updated to exclude `artifacts/concerts/`.

---

## Next steps (out of scope for this branch)

1. Provision `TICKETMASTER_API_KEY` and `SEATGEEK_CLIENT_ID` on Raven; re-run probes for Houston/Austin/Dallas + rock/metal + Breaking Benjamin / Shinedown / Disturbed.
2. Compare result overlap and dedupe collision rate between Ticketmaster and SeatGeek.
3. Only after repeated Raven evidence: design a Concerts vertical adapter (still separate from marketplace hunts).
