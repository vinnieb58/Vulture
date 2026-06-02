# Simply Fresh Kitchen Probe

**This is a feasibility probe, not meal-order automation.**

Household experiment for The Aviary ecosystem (Raven server). The goal is to learn whether Playwright/Chromium can load [The Simply Fresh Kitchen](https://new.thesimplyfreshkitchen.com/), authenticate, and reach account/order/calendar areas **without placing orders**.

## Purpose

- Prove Raven can run Chromium/Playwright against the site
- Bootstrap login via manual session capture (`--manual-login`)
- Reuse saved session state on later runs
- Capture screenshots/HTML at key steps for selector mapping
- Emit a concise feasibility report

## Safety boundaries

- Does **not** submit meal orders, checkout, pay, or confirm purchases
- Does **not** store usernames/passwords in code
- Does **not** click forbidden actions (`Submit`, `Checkout`, `Pay`, `Confirm`, etc.)
- Skips uncertain actions with `UNCERTAIN_ACTION_SKIPPED` and saves a screenshot
- Uses normal pacing (about 1 second between actions)
- Session/cookie files are gitignored and must not be committed

## Setup

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r experiments/simplyfresh_probe/requirements.txt
.venv/bin/playwright install chromium
```

On Raven without a physical display, use Xvfb for headed/manual login:

```bash
xvfb-run python3 experiments/simplyfresh_probe/probe_simplyfresh.py --manual-login --headed
```

## Commands

Manual login (first run — saves session):

```bash
python3 experiments/simplyfresh_probe/probe_simplyfresh.py --manual-login
```

Normal probe run (reuses saved session if present):

```bash
python3 experiments/simplyfresh_probe/probe_simplyfresh.py
```

Optional flags:

```bash
python3 experiments/simplyfresh_probe/probe_simplyfresh.py --headed
python3 experiments/simplyfresh_probe/probe_simplyfresh.py --headless
python3 experiments/simplyfresh_probe/probe_simplyfresh.py --trace
```

## Session storage

Saved Playwright storage state:

`experiments/simplyfresh_probe/.auth/simplyfresh_storage_state.json`

This file contains cookies/local storage and is **gitignored**. Delete it to force a fresh login bootstrap.

## Artifacts

Each run writes to:

`experiments/simplyfresh_probe/artifacts/<run-id>/`

Includes:

- Step-numbered PNG screenshots
- Matching HTML snapshots
- Optional `trace.zip` when `--trace` is used

## Feasibility report fields

At the end of each run:

- `site_loaded`
- `login_required`
- `login_successful` (`true` / `false` / `unknown`)
- `account_page_accessible`
- `order_page_accessible`
- `calendar_detected`
- `meal_options_detected`
- `submit_controls_detected`
- `blockers_detected` (`captcha` / `cloudflare` / `2fa` / `unknown` / `none`)
- `recommended_next_step`

## Current known status

- **Live headless probe (2026-05-31):** homepage loads; nav links `MY ACCOUNT` (`/profile`) and `Place Order` are visible on the public homepage
- Clicking `MY ACCOUNT` without a session lands on a login form (email/password); manual login is required to proceed further
- No Cloudflare/captcha/2FA blockers observed on initial load in headless Chromium
- Calendar and meal-selection UI were not reachable without an authenticated session
- Next live step: run `--manual-login` on Raven/VNC, then rerun normally and inspect artifacts for stable selectors

## Not in scope (yet)

- Credential auto-fill from environment variables
- Automated monthly meal selection
- Production scheduling/service deployment
