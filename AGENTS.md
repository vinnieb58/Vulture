# AGENTS.md

## Cursor Cloud specific instructions

### Overview

Vulture is a Python CLI deal-hunting tool that scrapes Craigslist, filters listings via a YAML-driven rule engine, deduplicates with SQLite, and sends Discord webhook alerts. Single entry point: `python main.py`.

### Running the application

```bash
source .venv/bin/activate
python main.py
```

The app runs as a one-shot process (not a long-running server). Each invocation completes one "hunt cycle" and exits.

### Key dev commands

| Task | Command |
|---|---|
| Install deps | `source .venv/bin/activate && pip install -r requirements.txt` |
| Run app | `source .venv/bin/activate && python main.py` |
| Lint | `source .venv/bin/activate && ruff check .` |
| Run tests | `source .venv/bin/activate && pytest --ignore=ebay_playwright_test.py --ignore=ebay_test.py --ignore=microcenter_test.py --ignore=craigslist_test.py` |

### Non-obvious caveats

- **No pytest tests exist in the repo.** The `*_test.py` files at the root (`ebay_test.py`, `ebay_playwright_test.py`, `microcenter_test.py`, `craigslist_test.py`) are exploratory scraping scripts, not pytest tests. They execute HTTP requests at module import time and will fail during pytest collection. Always ignore them with `--ignore` flags.
- **SQLite DB is auto-created** at `data/vulture.db` on first run. Delete it to reset deduplication state: `rm -f data/vulture.db`.
- **Discord webhook is optional.** If `DISCORD_WEBHOOK_URL` is not set in `.env`, alerts are skipped with a warning. The app still runs and stores listings.
- **Craigslist access is required.** The app makes live HTTP requests to `houston.craigslist.org`. It will fail if there is no network access.
- **Hunt config** is at `config/hunts.yaml`. Edit this file to add/modify/disable hunts.
- **`python3.12-venv`** system package is required to create the virtual environment (`python3 -m venv .venv`). It may not be pre-installed on Ubuntu.
- **Dev tools** (`ruff`, `pytest`) are not in `requirements.txt`. Install them separately in the venv: `pip install ruff pytest`.
