# Robin daycare photos (experiment probe)

Robin is Aviary's **read-only** daycare photo collection and archive probe. It observes the daycare portal, downloads photos locally, deduplicates by content hash, and organizes files for later iCloud staging.

Robin does **not** post to social media, share photos through the portal, or perform destructive portal actions.

## Scope (this PR)

- Experiment probe only (`experiments/robin/daycare_photo_probe.py`)
- No systemd timer, Docker service, Discord alerts, or iCloud upload yet

## Purpose

1. Log into the daycare portal (credentials or saved session)
2. Discover photo/image candidates
3. Download to local storage with SHA-256 deduplication
4. Organize by date and record a SQLite manifest

## Environment variables

Set these in repo-root `.env` (never commit real values):

| Variable | Purpose |
|----------|---------|
| `ROBIN_DAYCARE_USERNAME` | Portal login username |
| `ROBIN_DAYCARE_PASSWORD` | Portal login password |
| `ROBIN_DAYCARE_PORTAL_URL` | Portal home or photos URL |
| `ROBIN_SESSION_DIR` | Saved Playwright session state (default `data/robin/session`) |
| `ROBIN_OUTPUT_DIR` | Local output root (default `data/robin`) |
| `ROBIN_MANIFEST_PATH` | SQLite manifest path (default `data/robin/manifest.db`) |
| `ROBIN_LOG_LEVEL` | Logging level (default `INFO`) |
| `ROBIN_HEADFUL` | Set `true` for headed browser without CLI flag |

## Safe manual run commands

```bash
cd /home/vinnieb58/projects/vulture
source .venv/bin/activate

# First-time headed dry-run (manual login + discovery only)
python experiments/robin/daycare_photo_probe.py --headful --dry-run

# Headed download of a small batch
python experiments/robin/daycare_photo_probe.py --headful --limit 10

# Manifest summary from prior runs (no browser)
python experiments/robin/daycare_photo_probe.py --summary-only

# Unit tests (no browser)
pytest tests/test_robin_*.py -q
```

On Raven without a physical display, use Xvfb for headed/manual login:

```bash
xvfb-run python experiments/robin/daycare_photo_probe.py --headful --dry-run
```

### Playwright prerequisites

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Storage layout

```text
data/robin/
  manifest.db
  session/
    daycare_storage_state.json
  photos/
    YYYY-MM-DD/
      <sha256_prefix>_<optional_safe_name>.jpg
```

### Manifest fields (SQLite `photo_manifest`)

- `source_portal` — logical portal identifier
- `detected_date` — `YYYY-MM-DD` when parsed from page context
- `downloaded_path` — local file path after successful download
- `sha256` — content hash (unique dedupe key)
- `original_url` — log-safe URL shape (query params redacted)
- `first_seen` — UTC ISO timestamp
- `status` — `downloaded`, `skipped_duplicate`, `failed`, or `dry_run`

## Logging safety

Robin logs aggregate counts and redacted URL shapes. It does **not** log passwords, auth cookies, tokens, or full private photo URLs with secret query parameters.

## Next steps

1. Run `--headful --dry-run` against the real daycare portal and capture stable selectors in `robin/portal.py`
2. Confirm dedupe and date-folder layout with a small `--limit` batch
3. Add optional iCloud staging once the probe is reliable
4. Consider a systemd timer or Nest visibility after production validation
