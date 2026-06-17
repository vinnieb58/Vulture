# Sparrow — toddler school meal ordering (v0 experiment)

_Last updated: 2026-06-17_

**Sparrow** is an Aviary household service for **assisted Simply Fresh Kitchen school meal ordering** on Raven. It automates navigation, profile selection, and dry-run meal picks for a configured child profile — **never checkout, payment, or order submission**.

Implementation today lives under `experiments/simplyfresh_probe/` (Playwright probes). There is **no systemd service** and **no scheduled execution** yet.

**Site:** [The Simply Fresh Kitchen](https://new.thesimplyfreshkitchen.com/)

---

## Purpose

Sparrow reduces repetitive monthly meal-selection work for toddler school lunches:

1. Reuse a saved browser session on Raven (no credentials in git).
2. Navigate the logged-in order flow: **Order Now → profile chooser → meal calendar**.
3. Map the calendar and meal cards (inspect-only mode).
4. Optionally dry-run select **non-vegetarian** meals for up to N weekdays.
5. Capture artifacts (screenshots, HTML, JSON reports) for operator review.

Sparrow is **experiment-only** until promoted beyond manual operator runs.

---

## Login / session handling

| Item | Path / behavior |
|------|-----------------|
| Storage state | `experiments/simplyfresh_probe/.auth/simplyfresh_storage_state.json` |
| Override env | `SIMPLYFRESH_STORAGE_STATE_PATH` (absolute path) |
| Bootstrap | `probe_simplyfresh.py --manual-login` (headed / VNC / Xvfb on Raven) |
| Reuse | Loaded into Playwright context before any navigation; logs `Reusing storage state: ...` |
| Expired session | Detected **after** loading home/profile — prompts re-run of manual login |

Session file is **gitignored**. Usernames/passwords are **never** stored in code or committed artifacts.

---

## Profile selection

Logged-in checkout shows **Who are you ordering for?** with one or more child profiles.

| Flag | Example |
|------|---------|
| `--profile-name` | `"Vincent Bergeron"` |
| `--school` | `"MEADOW MONTESSORI SCHOOL"` |

Behavior:

- Prefer green **Order Now** / **ORDER NOW** (home hero or profile reminder); fallback nav **Place Order** only if absent.
- **Deduplicate** nested DOM containers that describe the same profile (parent + child wrappers).
- Click **Select profile** when exactly one logical profile remains, or when one matches configured name/school.
- **Never** click **Create a new profile** or disabled **Next**.
- Close lingering **Chooser__options--active** dropdown before calendar interaction.

---

## Meal classification

Dry-run selection uses deterministic rules in `meal_classification.py` (no LLM):

| Signal | Class |
|--------|--------|
| `V-` prefix (e.g. `V-Grilled Tofu & Mashed Potatoes`) | vegetarian — skip for non-veg preference |
| `vegetarian`, `veggie`, `vegan`, `plant-based` | vegetarian |
| `chicken`, `beef`, `turkey`, `fish`, etc. | non-vegetarian — preferred for dry-run |
| Ambiguous (e.g. cheese pizza only) | uncertain — day skipped |

Two-option days: prefer the non-vegetarian option when one veg + one meat card is visible.

---

## Safety boundaries

| Allowed | Not allowed |
|---------|-------------|
| Manual session bootstrap | Credentials in repo |
| Inspect calendar / meal cards | Submit / finalize order |
| Dry-run meal card clicks | Checkout / Pay / Confirm |
| Close overlays/modals (X, Cancel, Escape, outside click) | Click forbidden submit controls |
| Local artifacts under `experiments/simplyfresh_probe/artifacts/` | systemd / cron scheduling (not yet) |
| Stop on `AUTOSAVE_RISK_DETECTED` (unless `--continue-after-autosave`) | Production unattended runs |

After each successful day selection, Sparrow closes:

- Profile chooser overlays (`Chooser__options--active`)
- Meal detail modals (`.Modal` / `.Modal__content`) via safe dismiss only

---

## Operator commands (Raven)

From `/home/vinnieb58/projects/vulture`:

### One-time setup

```bash
cd /home/vinnieb58/projects/vulture
python3 -m venv .venv
.venv/bin/pip install -r experiments/simplyfresh_probe/requirements.txt
.venv/bin/playwright install chromium
```

### 1. Manual login (save session)

```bash
xvfb-run .venv/bin/python3 experiments/simplyfresh_probe/probe_simplyfresh.py --manual-login --headed
```

### 2. Feasibility check (session valid)

```bash
.venv/bin/python3 experiments/simplyfresh_probe/probe_simplyfresh.py --headless
```

### 3. Inspect calendar (no meal clicks)

```bash
.venv/bin/python3 experiments/simplyfresh_probe/probe_meal_selection.py --inspect-only --headless \
  --profile-name "Vincent Bergeron" --school "MEADOW MONTESSORI SCHOOL"
```

### 4. Dry-run select (max 3 weekdays)

```bash
.venv/bin/python3 experiments/simplyfresh_probe/probe_meal_selection.py --dry-run-select --max-days 3 --headless \
  --profile-name "Vincent Bergeron" --school "MEADOW MONTESSORI SCHOOL"
```

### 5. Review artifacts

```bash
ls -lt experiments/simplyfresh_probe/artifacts/ | head
cat experiments/simplyfresh_probe/artifacts/<run-id>/meal_selection_report.json
```

### Tests (dev)

```bash
pytest tests/test_simplyfresh_*.py -v
```

Probe-specific reference: [experiments/simplyfresh_probe/README.md](../../experiments/simplyfresh_probe/README.md).

---

## Known limitations

- **Manual login required** when storage state is missing or expired (no credential auto-fill).
- **Experiment path name** — code directory is still `simplyfresh_probe/`; service name in docs is **Sparrow**.
- **No systemd / timer** — operator invokes probes manually; no production scheduling.
- **No messaging bridge** — no Discord, Telegram, or WhatsApp integration.
- **No order submission** — operator must review and finalize in the Simply Fresh web UI or app.
- **Autosave risk** — meal card clicks may persist selections server-side; dry-run stops on detected autosave unless overridden.
- **DOM fragility** — site UI changes may require selector updates; artifacts are the debugging source of truth.
- **Single household scope** — tuned for one child profile (Vincent / Meadow Montessori); multi-child ambiguity requires explicit flags.
- **Headless + overlays** — profile chooser and meal modals must be dismissed explicitly; regression-prone area.

---

## Related docs

- [AVIARY_PROJECT_CONTEXT.md](./AVIARY_PROJECT_CONTEXT.md) — Sparrow in service catalog
- [PROJECT_STATUS.md](./PROJECT_STATUS.md) — current workstream
- [CODEBASE_STATUS.md](./CODEBASE_STATUS.md) — implementation map
