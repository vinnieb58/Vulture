# Kestrel — Smart Meter Texas probe

Kestrel is an Aviary **read-only** household energy probe. It ingests 15-minute interval usage from [Smart Meter Texas](https://www.smartmetertexas.com), stores normalized rows in a dedicated SQLite database, and prints safe summaries.

Kestrel is independent from the Vulture hunt scheduler and Crow Discord bot.

## What is supported in v0

- Flexible **CSV import** from SMT dashboard exports (`--import-csv`)
- Probe-quality **live refresh** via portal JSON API with Playwright CSV fallback (`--live-refresh`)
- SQLite storage with upsert dedupe on `(provider, start_ts, end_ts)`
- Summaries: total kWh, peak 15-minute interval, estimated peak kW, daily totals, hourly shape, missing intervals, top intervals
- Status snapshot at `data/kestrel/kestrel_status.json` after each probe run
- Optional **daily systemd timer** for API-only refresh (`kestrel-smt-refresh.timer`)

## Not implemented yet

- Official registered SMT API (`services.smartmetertexas.net`) integration
- Browser automation in unattended/scheduled mode (timer uses API only)
- Automations, billing projections, breaker-level assumptions, smart-home control
- Discord/Crow commands or Nest dashboard cards

## Environment variables

Add these to repo-root `.env` on Raven (never commit values):

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `KESTREL_ENABLED` | for live fetch | `false` | Set `true` to allow live portal fetch |
| `KESTREL_SMT_USERNAME` | for live fetch | — | SMT portal user id |
| `KESTREL_SMT_PASSWORD` | for live fetch | — | SMT portal password |
| `KESTREL_SMT_ACCOUNT_ID` | optional | — | ESIID; auto-detected when only one meter |
| `KESTREL_DATA_DIR` | optional | `data/kestrel` | Probe data directory |
| `KESTREL_DB_PATH` | optional | `data/kestrel/kestrel.db` | SQLite path |
| `KESTREL_LOOKBACK_DAYS` | optional | `7` | Default `--days` lookback |
| `KESTREL_HEADLESS` | optional | `true` | Reserved for future browser probe |
| `KESTREL_LOG_LEVEL` | optional | `INFO` | Probe log level |

Missing Kestrel variables do **not** affect normal Vulture/Crow startup.

## Manual CSV import (recommended v0 path)

1. Log in to Smart Meter Texas → Dashboard → **Energy Data 15 Min Interval**
2. Choose start/end dates → **Export My Report** (CSV)
3. Copy the CSV to Raven (outside git), e.g. under `~/imports/`
4. Run:

```bash
cd /home/vinnieb58/projects/vulture
source .venv/bin/activate
python experiments/kestrel/smart_meter_texas_probe.py --import-csv ~/imports/smt_15min.csv
```

## Live refresh (v0)

```bash
cd /home/vinnieb58/projects/vulture
source .venv/bin/activate
# Ensure KESTREL_ENABLED=true and SMT credentials in .env
python experiments/kestrel/smart_meter_texas_probe.py --live-refresh --days 3 --no-browser-fallback
python experiments/kestrel/smart_meter_texas_probe.py --live-refresh --days 2 --dry-run
python experiments/kestrel/smart_meter_texas_probe.py --live-refresh --debug-safe
python experiments/kestrel/smart_meter_texas_probe.py --summary-only
```

`--live-refresh` tries the residential portal JSON API first (`/api/adhoc/intervalsynch`), then optionally falls back to Playwright CSV export. Use `--no-browser-fallback` for timer-safe API-only runs.

By default, `--live-refresh --days N` excludes the current local day (America/Chicago) because SMT interval data may lag 24–48 hours. Use `--include-current-day` to request today's unpublished data.

If live refresh fails, use CSV import. The portal JSON endpoints are unofficial and may rate-limit or change.

## Daily systemd timer (v0)

See `docs/current/KESTREL_OPERATIONS.md` for install, manual test, journal, and rollback commands.

Quick install:

```bash
./scripts/install_kestrel_timer.sh --enable
```

The timer runs `kestrel-smt-refresh.service` daily (~08:30, API-only, last 3 completed days). Isolated from Vulture/Crow units.

## Legacy live probe (experimental)

```bash
python experiments/kestrel/smart_meter_texas_probe.py --days 7
```

Same API path as `--live-refresh` but without refresh status tracking or browser fallback.

## Data storage

- SQLite: `data/kestrel/kestrel.db` (table `energy_intervals`)
- Status JSON: `data/kestrel/kestrel_status.json`
- Account/meter identifiers are stored as short hashes only

## Safety

- Read-only probe — no writes to SMT, no hunt DB changes
- Credentials and raw ESIIDs are never logged or printed
- Do not commit `.env`, CSV exports, screenshots, or `data/kestrel/*.db`
