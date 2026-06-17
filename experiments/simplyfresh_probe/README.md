# Sparrow v0 — Simply Fresh Kitchen probe

> **Service name:** [Sparrow](../../docs/current/SPARROW_MEAL_ORDERING.md)  
> **Implementation path:** `experiments/simplyfresh_probe/` (legacy directory name)  
> **Status:** Manual experiment only — no systemd, no scheduled runs.

**This is a feasibility / dry-run probe, not production meal-order automation.**

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

## Setup (one-time on Raven)

From the repository root (`/home/vinnieb58/projects/vulture`):

```bash
cd /home/vinnieb58/projects/vulture
python3 -m venv .venv
.venv/bin/pip install -r experiments/simplyfresh_probe/requirements.txt
.venv/bin/playwright install chromium
```

## Raven operator flow (toddler school meal ordering probe)

Run these steps in order. **Step 1 requires a headed browser** (VNC or Xvfb on Raven). Steps 2–4 can run headless once session state is saved.

### 1. Manual login — save session state

Use VNC, or Xvfb if no display:

```bash
cd /home/vinnieb58/projects/vulture
xvfb-run .venv/bin/python3 experiments/simplyfresh_probe/probe_simplyfresh.py --manual-login --headed
```

Log in in the browser window, then press Enter in the terminal when prompted. Session is saved to:

`experiments/simplyfresh_probe/.auth/simplyfresh_storage_state.json`

### 2. Feasibility probe — verify saved session

```bash
cd /home/vinnieb58/projects/vulture
.venv/bin/python3 experiments/simplyfresh_probe/probe_simplyfresh.py --headless
```

Confirm `login_successful: true`, `calendar_detected: true`, and `meal_options_detected: true` in the report. Review artifacts under `experiments/simplyfresh_probe/artifacts/<run-id>/`.

### 3. Meal-selection probe — inspect only (map calendar, no meal clicks)

```bash
cd /home/vinnieb58/projects/vulture
.venv/bin/python3 experiments/simplyfresh_probe/probe_meal_selection.py --inspect-only --headless \
  --profile-name "Vincent Bergeron" --school "MEADOW MONTESSORI SCHOOL"
```

Review `calendar_map.json`, `after_order_now.*`, `after_select_profile.*`, `visible_text.txt`, and `buttons_links_summary.json` in the run artifacts.

### 4. Meal-selection probe — dry-run select (max 3 weekdays)

```bash
cd /home/vinnieb58/projects/vulture
.venv/bin/python3 experiments/simplyfresh_probe/probe_meal_selection.py --dry-run-select --max-days 3 --headless \
  --profile-name "Vincent Bergeron" --school "MEADOW MONTESSORI SCHOOL"
```

Selects non-vegetarian meals only (e.g. skips `V-Grilled Tofu`, prefers `Baked Chicken Breast`). Never clicks submit, checkout, pay, confirm, or finalize. Stops on `AUTOSAVE_RISK_DETECTED` unless you pass `--continue-after-autosave`.

### 5. Review artifacts before any future promotion

```bash
cd /home/vinnieb58/projects/vulture
ls -lt experiments/simplyfresh_probe/artifacts/ | head
# Inspect latest run:
cat experiments/simplyfresh_probe/artifacts/<run-id>/meal_selection_report.json
```

Do not expand to `--max-days 31` or promote beyond this experiment until artifacts look correct and autosave risk is understood.

---

## Commands (reference)

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

## Meal selection dry-run (`probe_meal_selection.py`)

**Still a probe** — maps the order calendar and can attempt non-vegetarian meal picks for up to N weekdays. Never clicks submit, save order, checkout, pay, confirm, or finalize.

### Logged-in navigation flow

1. Load home (`/`) or profile (`/profile`) with saved session.
2. Click green **Order Now** / **ORDER NOW** (profile reminder box or home hero). Fall back to nav **Place Order** only if Order Now is absent.
3. On checkout **Who are you ordering for?** — click **Select profile** for the configured child (never **Create a new profile**, never disabled **Next**).
4. Wait for **Choose your meals** calendar with date row, day circles, and meal cards.
5. Optionally dry-run select non-vegetarian meal cards (classify by card title/description).

Optional filters (required when multiple profiles exist):

| Flag | Example |
|------|---------|
| `--profile-name` | `"Vincent Bergeron"` |
| `--school` | `"MEADOW MONTESSORI SCHOOL"` |

If omitted and exactly one profile card exists, that profile is selected automatically.

### Commands

Inspect only (map calendar; no meal option clicks):

```bash
python3 experiments/simplyfresh_probe/probe_meal_selection.py --inspect-only --headless \
  --profile-name "Vincent Bergeron" --school "MEADOW MONTESSORI SCHOOL"
```

First safe dry run (default max 3 weekdays):

```bash
python3 experiments/simplyfresh_probe/probe_meal_selection.py --dry-run-select --max-days 3 --headless \
  --profile-name "Vincent Bergeron" --school "MEADOW MONTESSORI SCHOOL"
```

Full visible month (only after reviewing artifacts):

```bash
python3 experiments/simplyfresh_probe/probe_meal_selection.py --dry-run-select --max-days 31 --headless
```

If `AUTOSAVE_RISK_DETECTED` appears after the first selection, the run stops unless you pass `--continue-after-autosave`.

### Dry-run artifacts

Under `artifacts/<run-id>/`:

- `after_order_now.png` / `.html` — after clicking Order Now
- `after_select_profile.png` / `.html` — after profile chooser (if shown)
- `visible_text.txt` — latest page text snapshot
- `buttons_links_summary.json` — visible controls with disabled state
- `current_url.txt` — latest URL
- `before_order_page.png` / `.html`
- `calendar_map.json`
- `each_day_before_*` / `each_day_after_*` (dry-run-select)
- `meal_selection_report.json`

### Meal selection report fields

- `logged_in`, `order_page_reached`, `profile_chooser_seen`, `profile_selected`, `meal_calendar_reached`
- `month_detected`
- `days_detected`, `selectable_days_detected`, `days_attempted`, `days_selected`, `days_skipped`, `uncertain_days`
- `vegetarian_options_detected`, `non_vegetarian_options_detected`
- `forbidden_controls_detected`, `autosave_risk_detected`, `recommended_next_step`

## Current known status

- **Raven headless (2026-06-02):** saved session works; feasibility probe reached calendar and meal options
- Meal dry-run ready for `--inspect-only` then `--dry-run-select --max-days 3` on Raven

## Not in scope (yet)

- Credential auto-fill from environment variables
- Automated monthly meal submission / checkout
- Production scheduling/service deployment
