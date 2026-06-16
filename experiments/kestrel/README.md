# Kestrel — Smart Meter Texas probe

Kestrel is an Aviary **read-only** household energy probe. It ingests 15-minute interval usage from [Smart Meter Texas](https://www.smartmetertexas.com), stores normalized rows in a dedicated SQLite database, and prints safe summaries.

Kestrel is independent from the Vulture hunt scheduler and Crow Discord bot.

## What is supported in v0

- Flexible **CSV import** from SMT dashboard exports (`--import-csv`)
- Probe-quality **live fetch** via the residential portal JSON API (username/password; may break if SMT changes)
- SQLite storage with upsert dedupe on `(provider, start_ts, end_ts)`
- Summaries: total kWh, peak 15-minute interval, estimated peak kW, daily totals, hourly shape, missing intervals, top intervals
- Status snapshot at `data/kestrel/kestrel_status.json` after each probe run

## Not implemented yet

- Official registered SMT API (`services.smartmetertexas.net`) integration
- Browser automation / Playwright export path
- Automations, billing projections, breaker-level assumptions, smart-home control
- Discord/Crow commands or Nest dashboard cards
- systemd timer / scheduled collection

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

## Live probe (experimental)

```bash
cd /home/vinnieb58/projects/vulture
source .venv/bin/activate
# Ensure KESTREL_ENABLED=true and SMT credentials in .env
python experiments/kestrel/smart_meter_texas_probe.py --days 7
python experiments/kestrel/smart_meter_texas_probe.py --from 2026-06-01 --to 2026-06-16
python experiments/kestrel/smart_meter_texas_probe.py --summary-only
```

If live fetch fails, use CSV import. The portal JSON endpoints are unofficial and may rate-limit or change.

## Data storage

- SQLite: `data/kestrel/kestrel.db` (table `energy_intervals`)
- Status JSON: `data/kestrel/kestrel_status.json`
- Account/meter identifiers are stored as short hashes only

## Safety

- Read-only probe — no writes to SMT, no hunt DB changes
- Credentials and raw ESIIDs are never logged or printed
- Do not commit `.env`, CSV exports, screenshots, or `data/kestrel/*.db`
