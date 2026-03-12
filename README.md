# Vulture

Vulture is a deal-hunting tool that scrapes marketplaces for listings matching defined hunts, filters them against configurable rules, persists new finds to a local SQLite database, and sends Discord alerts for anything new.

---

## v1.0 Scope

- **Source:** Craigslist only
- **Alerts:** Discord webhook
- **Storage:** SQLite (local file)
- **Hunt configuration:** YAML-driven (`config/hunts.yaml`)
- **Rule engine:** `max_price`, `include_keywords`, `exclude_keywords`
- **Scheduling:** External scheduler (Windows Task Scheduler or equivalent)

---

## Folder Structure

```
vulture/
├── adapters/
│   └── craigslist.py       # Craigslist scraper
├── config/
│   ├── hunts.yaml          # Hunt definitions
│   └── settings.yaml       # Reserved for future settings
├── data/
│   └── vulture.db          # SQLite database (auto-created on first run)
├── engine/
│   ├── database.py         # DB init, deduplication, listing persistence
│   ├── hunts.py            # YAML hunt loader
│   ├── notifier.py         # Discord webhook alerts
│   └── rules.py            # Rule evaluation logic
├── logs/
│   └── vulture.log         # Log file (auto-created on first run)
├── models/
│   └── listing.py          # Listing dataclass
├── .env                    # Discord webhook URL (not committed)
├── main.py                 # Entry point
└── requirements.txt        # Python dependencies
```

---

## Setup

### 1. Clone the repository

```
git clone <repo-url>
cd vulture
```

### 2. Create a virtual environment

```
python -m venv .venv
```

Activate it:

- **Windows:** `.venv\Scripts\activate`
- **macOS/Linux:** `source .venv/bin/activate`

### 3. Install dependencies

```
pip install -r requirements.txt
```

### 4. Configure the Discord webhook

Create a `.env` file in the project root:

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your/webhook/url
```

If `DISCORD_WEBHOOK_URL` is not set, Discord alerts are skipped and a warning is logged. The rest of the run continues normally.

---

## Configuration

### `config/hunts.yaml`

All hunts are defined in this file. Each hunt is an entry in the top-level `hunts` list.

**Minimal hunt (no rules):**

```yaml
hunts:
  - name: monitor_houston
    source: craigslist
    city: houston
    query: monitor
    limit: 10
    enabled: true
```

**Hunt with rules:**

```yaml
hunts:
  - name: gpu_hunt
    source: craigslist
    city: houston
    query: gpu
    limit: 20
    enabled: true
    rules:
      max_price: 400
      include_keywords:
        - "3080"
        - "3090"
        - "4080"
      exclude_keywords:
        - broken
        - parts only
```

### Hunt fields

| Field | Required | Default | Description |
|---|---|---|---|
| `name` | Yes | — | Unique identifier for the hunt |
| `source` | Yes | — | Marketplace to scrape. Only `craigslist` is supported in v1.0 |
| `city` | No | `houston` | Craigslist subdomain city |
| `query` | Yes | — | Search query string |
| `limit` | No | `10` | Max number of listings to fetch per run |
| `enabled` | No | `true` | Set to `false` to disable without removing the hunt |
| `rules` | No | none | Optional rule block (see below) |

### Rule fields

| Rule | Type | Description |
|---|---|---|
| `max_price` | integer | Listing price must be ≤ this value. Listings with no price are excluded when this rule is set. |
| `include_keywords` | list | At least one keyword must appear in the listing title (case-insensitive, substring match). |
| `exclude_keywords` | list | No excluded keyword may appear in the listing title (case-insensitive, substring match). |

**Note on numeric keywords:** YAML parses unquoted numbers (e.g. `3080`) as integers. Vulture handles this safely, but quoting them (`"3080"`) is recommended for clarity.

### Enabling and disabling hunts

Set `enabled: false` on any hunt to skip it during execution. Its history in the database is preserved. Re-enable by setting `enabled: true` or removing the field entirely (missing `enabled` defaults to `true`).

---

## Runtime Behavior

### How a run works

1. `main.py` initializes logging and the SQLite database.
2. All enabled hunts are loaded from `config/hunts.yaml`.
3. For each hunt, listings are scraped from the configured source.
4. Each listing is evaluated against the hunt's rules (if any). Listings that fail are logged as `FILTERED` and skipped entirely.
5. Listings that pass rules are checked against the database by URL. Duplicates are logged as `OLD` and skipped.
6. New listings are inserted into the database, logged as `NEW`, and sent as Discord alerts.
7. A cycle summary is logged when all hunts complete.

### Deduplication

Listings are deduplicated by their URL (`link` field). A listing is only inserted and alerted once, regardless of how many hunts or runs encounter it.

### Discord alerts

A Discord message is sent for each new listing that passes rules. Alerts include source, title, price, location, and link. If the webhook is unavailable or returns an error, the error is logged and the run continues — the listing is still saved to the database.

### Logging

Logs are written to both the console and `logs/vulture.log`. The log level is `INFO`. Each run produces:

```
2026-03-11 09:00:00,001 [INFO] Starting Vulture hunt cycle
2026-03-11 09:00:00,012 [INFO] Loaded 2 hunt(s)
2026-03-11 09:00:00,015 [INFO] Starting hunt: gpu_hunt (craigslist)
2026-03-11 09:00:03,210 [INFO] NEW: Listing(source='craigslist', ...)
2026-03-11 09:00:04,100 [INFO] Done hunt 'gpu_hunt'. New: 1, Existing: 4, Filtered: 2
2026-03-11 09:00:04,200 [INFO] 1 new listing(s) found
2026-03-11 09:00:04,201 [INFO] Hunt cycle completed
```

`logs/vulture.log` appends across runs and is not rotated automatically in v1.0.

---

## Running Vulture

### Manual run

From the project root with the virtual environment active:

```
python main.py
```

### Windows Task Scheduler

To run Vulture automatically on a schedule:

1. Open **Task Scheduler** and click **Create Basic Task**.
2. Set a name (e.g. `Vulture`) and a trigger (e.g. every 30 minutes).
3. For the action, select **Start a program** and configure:

   | Field | Value |
   |---|---|
   | Program/script | `C:\Users\<you>\vulture\.venv\Scripts\python.exe` |
   | Add arguments | `main.py` |
   | Start in | `C:\Users\<you>\vulture` |

4. Under **Conditions**, uncheck "Start the task only if the computer is on AC power" if running on a laptop.
5. Under **Settings**, check "If the task is already running, do not start a new instance" to prevent overlapping runs.

**Important:** Use the full path to the `.venv` Python executable, not the system Python. The **Start in** directory must be set to the project root so relative paths (`config/`, `data/`, `logs/`) resolve correctly.

---

## v1.0 Limitations / Deferred to v2.0

- **Single source:** Only Craigslist is supported. eBay and other adapters are not implemented.
- **No log rotation:** `logs/vulture.log` grows indefinitely. Manual cleanup or an external log rotation tool is required.
- **No price history:** The database records `first_seen` only. Price changes on existing listings are not tracked.
- **No re-alert logic:** A listing already in the database is never alerted again, even if its price drops.
- **Substring keyword matching only:** `include_keywords` and `exclude_keywords` use simple substring matching. Regular expressions and whole-word matching are not supported.
- **No web UI or CLI:** Hunts are managed by editing `config/hunts.yaml` directly.
- **No multi-city hunts:** Each hunt targets a single Craigslist city.

---

## Troubleshooting

**No listings appear / run finishes with all `OLD`**
The database already contains these listings from a previous run. This is normal behavior. Delete `data/vulture.db` to reset state.

**`FILTERED` listings that you expected to pass**
Check your `max_price` threshold and keyword lists in `config/hunts.yaml`. For `max_price`, listings with no price are always filtered when the rule is set. For keywords, matching is case-insensitive substring — `"monitor"` matches `"27 inch Monitor"`.

**Discord alerts not arriving**
- Confirm `DISCORD_WEBHOOK_URL` is set in `.env` and the `.env` file is in the project root.
- Check `logs/vulture.log` for `WARNING No Discord webhook configured` or any `Failed to send Discord alert` error lines.
- Test the webhook URL manually with a tool like `curl` or Postman.

**`python-dotenv` not found**
Install it manually: `pip install python-dotenv`. It is used by `engine/notifier.py` to load the `.env` file.

**Task Scheduler run does nothing / exits immediately**
- Confirm the **Start in** directory is set to the project root.
- Confirm the **Program/script** points to the `.venv` Python executable, not the system Python.
- Check `logs/vulture.log` for error output — the scheduler suppresses console output but the log file is always written.

**`AttributeError` or unexpected crash**
Check `logs/vulture.log` for lines containing `ERROR` or `CRITICAL`. Hunt-level exceptions are caught and logged with a full traceback; the remaining hunts continue to run.
