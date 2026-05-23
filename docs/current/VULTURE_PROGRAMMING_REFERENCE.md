# Vulture Programming Reference

_Last updated: 2026-05-21_

This file is meant to be the working reference during Vulture programming sessions. It focuses on facts, constraints, and decisions that affect how we code, test, deploy, and debug the system.

---

## 1. Current Project Identity

### Project name
**Vulture**

### Current phase
Treat the current active codebase as **Vulture 2.0**.

Vulture 1.0 should be considered functionally complete for the original goal:

- DB-backed hunts exist.
- Discord slash commands exist for hunt lifecycle control.
- The CLI runner can load hunts from the DB.
- Craigslist scraping works enough to produce listings.
- Discord alerts have worked.
- Scheduler-style operation is conceptually separated from Discord command/control.

The current work should not be framed as “starting over.” It is the next evolution of the existing system.

---

## 2. Operating Model

Vulture has two different runtime roles that should stay mentally separate:

### `discord_bot.py`
Command/control layer.

Purpose:

- Starts the Discord bot.
- Registers slash commands.
- Lets the user create, list, show, pause, resume, and end hunts.
- Should stay running continuously.

Known working slash commands:

- `/hunt_list`
- `/hunt_show`
- `/hunt_create`
- `/hunt_pause`
- `/hunt_resume`
- `/hunt_end`

### `main.py`
One hunt cycle runner.

Purpose:

- Loads active hunts.
- Runs configured adapters.
- Applies normalization/rules/dedupe.
- Sends notifications if matching listings are found.

Important distinction:

- `main.py` is not the always-running Discord bot.
- `main.py` is the repeatable job that should eventually be launched by a scheduler, cron, systemd timer, or similar.

### Scheduler / task layer
Repeats `main.py`.

Current concept:

```text
discord_bot.py = command/control layer
main.py        = one hunt cycle
scheduler/task = repeats main.py
```

This separation matters when debugging. If the Discord bot is running, that does **not** mean hunts are automatically running.

---

## 3. Current Production-ish Host: Raven

### Machine identity

- Hostname/server name: `raven`
- Login shown: `vinnieb58@raven`
- Wired IP shown during setup: `192.168.1.143`
- OS: Ubuntu Server was installed successfully.
- SSH: OpenSSH was enabled during installation.
- Ubuntu Pro: skipped.
- Featured snaps: skipped.
- Install type: standard server.

### Hardware reality

- Machine: ASUS Chromebox 3 / CN65 / TEEMO board.
- Firmware: MrChromebox UEFI Full ROM installed successfully.
- Boot behavior: now behaves like a normal UEFI mini PC.
- Boot drive currently used: original 32 GB **M.2 SATA** SSD.
- Spare Steam Deck-style NVMe was **not detected** by the installer.

Programming impact:

- Do not assume lots of disk space.
- Avoid unnecessary packages, snaps, browser caches, giant logs, and local artifacts.
- Browser automation should be conservative.
- Use wired Ethernet as the normal path.
- Internal boot storage should be treated as small and replaceable.

### Storage warning

The Chromebox accepted the original SATA M.2 drive but did not detect the spare NVMe drive.

Important distinction:

```text
M.2 = physical connector/form factor
SATA / NVMe = storage protocol
```

Future internal SSD upgrades should likely be **M.2 SATA**, not NVMe, unless the exact board is proven to support NVMe.

---

## 4. Development Workflow Assumptions

### Primary coding machine
Windows PC with Cursor.

### Target runtime machine
Raven, accessed over SSH.

### Expected workflow

Use the Windows PC for code editing and Git operations, then deploy/sync/pull onto Raven for runtime testing.

Preferred pattern:

```text
Windows/Cursor: edit, commit, branch management
GitHub: source of truth / backup
Raven: pull latest, install deps, run bot/cycles/scheduler tests
```

### Before major changes
Always check:

```bash
git status
git branch
git log --oneline -5
```

Before shutting down or moving machines, confirm local work is committed and pushed:

```bash
git status
git branch -vv
git log --oneline --decorate -5
git fetch
git status -sb
```

The goal is to avoid having important Vulture changes stranded on one machine.

---

## 5. Branching Context

Known branches from prior work:

- `main`
- `v2.0-dev`
- `feature/title-intelligence-v1`

### `main`
Stable baseline.

### `v2.0-dev`
Expected active development branch for Vulture 2.0-level changes.

### `feature/title-intelligence-v1`
Branch created for improving title-based intelligence without changing core architecture.

Goals of that branch:

- Improve LLM translator structure.
- Add category-specific attribute schemas.
- Expand deterministic title parsing and rules.

Restrictions from that branch:

- No body scraping.
- No DB redesign.
- No adapter changes unless explicitly needed.
- Preserve deterministic execution.

---

## 6. Current Architecture Summary

Current Vulture components:

- Python app.
- SQLite database.
- DB-backed hunt persistence.
- Rule engine.
- Site adapters.
- Listing normalization.
- Dedupe by listing link.
- Discord bot command layer.
- Discord notification path.
- Logging system.
- Runner/scheduler concept.

### Known command flow

```text
discord_bot.py
  -> engine/command_router.py
    -> engine/hunt_service.py
      -> engine/hunt_repository.py
        -> SQLite DB
```

### Known hunt execution flow

```text
main.py
  -> load active hunts
  -> hunt_to_execution_dict()
  -> run_hunt()
  -> adapter search
  -> normalize listings
  -> matches_rules()
  -> save_listing()
  -> notify if new match
```

### Known DB-backed hunt direction

The system moved away from YAML hunts and toward DB-backed hunts.

Important env setting:

```env
VULTURE_HUNT_SOURCE=db
```

Programming impact:

- Treat DB as the normal hunt source.
- YAML may still exist for legacy/dev convenience, but should not be the center of future 2.0 behavior.
- If a test unexpectedly loads YAML hunts, check `VULTURE_HUNT_SOURCE`.

---

## 7. Hunt Model Details That Matter

Known hunt fields/concepts:

- `name`
- `status`
  - active
  - paused
  - ended
- `source_sites`
  - example: `["craigslist"]`
- `search_terms`
  - example: `["TV", "75\"", "4k"]`
- `include_keywords`
- `exclude_keywords`
- `max_price`
- `location`
  - `None` falls back to Houston in current behavior.
- `adapter_options`
  - can pass rule hints such as min/max price or vertical-specific attributes.

### Lifecycle expectations

Active hunts should run.

Paused hunts should remain saved but not run.

Ended hunts should be treated as inactive/retired.

Future cleanup behavior may purge ended-hunt listings, but be careful before deleting historical data automatically.

---

## 8. Adapters and Source Sites

### Craigslist
Current reliable baseline adapter.

Known behavior:

- Craigslist URL assembly has worked with spaces converted to `+`.
- Raw quote characters in search terms have worked in practice.
- Location fallback is Houston when no location is specified.

Programming impact:

- Use Craigslist as the first integration test target.
- Do not use eBay or Facebook as the first proof that the system works.

### eBay
Explored, but anti-bot friction was encountered.

Programming impact:

- Treat eBay as a harder adapter.
- Do not block core architecture on eBay working perfectly.
- Prefer test doubles/unit tests around parsing and rules before live eBay testing.

### Facebook Marketplace
Research found reusable public patterns but high brittleness and policy risk.

Practical notes:

- Browser automation first.
- HTML parsing second.
- Expect session/cookie/login/2FA issues.
- Proxy support may be a later optional layer.
- Keep Facebook Marketplace separate as an experimental adapter.
- Do not make Facebook Marketplace a core stable dependency.

Programming impact:

- Avoid importing random archived scraper repos wholesale.
- Use them only for pattern research.
- Keep policy and brittleness risk visible.

---

## 9. Vertical / Category Direction

Future source selection should be organized by **search vertical/type**, not a flat website list.

Desired mental model:

```text
cars
  -> Facebook Marketplace
  -> Craigslist
  -> AutoTempest / other future sources

computer parts
  -> eBay
  -> Craigslist
  -> Marketplace
  -> Micro Center / other future sources

TVs
  -> Craigslist
  -> Marketplace
  -> eBay
```

Programming impact:

- Do not hard-code UX around a flat list of websites forever.
- Consider vertical/category as a first-class concept or at least a future-compatible field.
- Adapter selection should eventually be category-aware.

---

## 10. LLM Translation Direction

Vulture should eventually support natural-language hunt creation, but deterministic execution should remain separate.

Preferred architecture:

```text
User natural language
  -> LLM translator
  -> structured hunt definition
  -> deterministic rules engine
  -> deterministic adapter execution
```

Do not make live scraping depend on asking an LLM at runtime for every listing.

### Current LLM translation goals

Improve from simple keyword extraction to:

```text
interpret -> expand -> structure
```

The translator should infer:

- category / vertical
- search terms
- include keywords
- exclude keywords
- price limits
- structured attributes
- source recommendations

### Category-specific schemas

Planned examples:

#### TVs

- size inches
- resolution
- panel type
- brand
- smart platform

Example problem from testing:

A hunt for a **75-inch 4K TV** returned some relevant results but also unrelated results. Future translation and filtering should turn that intent into specific structured constraints, not just loose keywords.

#### GPUs

- chipset/model
- VRAM
- desktop GPU vs complete PC/laptop
- brand may matter less than chipset

Important GPU exclusion idea:

- Filter complete systems/laptops when the user wants only a graphics card.
- Example exclusion words: `laptop`, `desktop`, `gaming pc`, `computer`, `system`, `tower`, `prebuilt`.

#### Vehicles

- year min/max
- mileage max
- make/model
- trim
- clean title / salvage terms
- drivetrain
- body style

---

## 11. Rule Engine / Title Intelligence Notes

The system should lean on deterministic parsing and rules after a hunt is created.

Known rule concepts:

- `max_price` is enforced.
- mileage/year/capacity-type filters have been discussed as enforced or intended in newer logic.
- `include_keywords` behavior has been debated.

Important caution:

A past include keyword change from `any()` to `all()` was rolled back or treated carefully. This means keyword semantics are tricky.

Suggested future approach:

- Keep default include behavior flexible.
- Add explicit `require_all_keywords` when the user intent demands strict matching.
- Prefer structured fields for strict constraints instead of overloading loose keywords.

Example:

For “75 inch 4K TV,” do not rely only on:

```text
include_keywords = ["75", "4k", "tv"]
```

Better future representation:

```yaml
category: tv
attributes:
  min_size_inches: 75
  max_size_inches: 75
  resolution: 4k
require_all_keywords: true
```

---

## 12. Discord UX Direction

Current slash commands are functional but verbose.

Future desired UX:

### Simple LLM-driven hunt creation

```text
/hunt find me a 75 inch 4k tv under $500 near Houston
```

### Manual/power-user hunt creation

Potential naming:

```text
/huntmanual_...
```

### Admin/lifecycle commands

Potential naming:

```text
/huntadmin_...
```

Examples:

- change
- start
- stop
- pause
- resume
- end

Programming impact:

- Do not over-optimize current command names as if final.
- Keep command routing/service layers clean so UX can change without rewriting core hunt logic.

---

## 13. Environment and Secrets

### `.env` is required locally/on Raven

The `.env` file should contain real secrets and runtime settings.

Never commit `.env`.

`.env.example` should contain placeholders only and is safe to commit.

Known/likely settings:

```env
DISCORD_TOKEN=...
DISCORD_GUILD_ID=...
DISCORD_WEBHOOK_URL=...
VULTURE_HUNT_SOURCE=db
```

There may be additional tokens later for LLM APIs or site-specific integrations.

Programming impact:

- Code should fail clearly when required env vars are missing.
- Startup logs should summarize config without exposing secrets.
- Mask tokens in logs.
- Keep `load_dotenv()` explicit rather than relying on import side effects.

---

## 14. Raven Runtime Commands

### SSH into Raven

```bash
ssh vinnieb58@192.168.1.143
```

The IP may change unless DHCP reservation/static IP is configured.

### tmux basics

List sessions:

```bash
tmux ls
```

Create bot session:

```bash
tmux new -s bot
```

Create scheduler session:

```bash
tmux new -s scheduler
```

Detach from tmux:

```text
Ctrl+b, then d
```

Attach:

```bash
tmux attach -t bot
```

Kill an empty/unneeded session:

```bash
tmux kill-session -t 1
```

or by name:

```bash
tmux kill-session -t scheduler
```

### Run bot

```bash
python discord_bot.py
```

### Run one hunt cycle

```bash
python main.py
```

If using a virtual environment, activate first:

```bash
source .venv/bin/activate
```

or run directly:

```bash
.venv/bin/python main.py
```

---

## 15. Testing Strategy

Testing should be layered so we are not constantly dependent on live websites.

### 1. Unit tests

Best targets:

- title parsing
- price parsing
- mileage parsing
- year parsing
- keyword matching semantics
- `matches_rules()`
- hunt model validation
- LLM translation output validation
- adapter result normalization

### 2. Repository / DB tests

Test:

- create hunt
- pause hunt
- resume hunt
- end hunt
- list active hunts
- list all hunts
- save listing
- dedupe by link
- do not notify duplicate listings

### 3. Adapter tests with fixtures

Avoid hammering live sites while developing.

Use saved HTML fixtures where possible:

```text
tests/fixtures/craigslist/*.html
```

Test that a known HTML file produces normalized listings.

### 4. Integration smoke tests

Use Craigslist first.

Example smoke flow:

```bash
python reset_dev_db.py
python main.py
python discord_bot.py
```

Known command used successfully on Windows:

```powershell
.\.venv\Scripts\python.exe reset_dev_db.py
.\.venv\Scripts\python.exe main.py
```

### 5. Discord command smoke tests

After bot startup:

- confirm bot logs show token/config loaded without leaking secrets
- confirm guild slash commands sync
- run `/hunt_create`
- run `/hunt_list`
- run `/hunt_show`
- run `/hunt_pause`
- run `/hunt_resume`
- run `/hunt_end`

### 6. Scheduler tests

Important question:

- Is the bot running?
- Is the scheduler running?
- Are hunt cycles actually executing repeatedly?

A working Discord bot alone does not prove scheduled hunts are running.

---

## 16. Logging Expectations

Logs should help answer:

- Did the bot start?
- Did slash commands sync?
- Which hunt source is active?
- How many active DB hunts loaded?
- Which adapters ran?
- How many raw listings were found?
- How many were filtered out?
- Why were listings filtered out?
- How many new listings were saved?
- Were Discord notifications sent?

Avoid logging:

- Discord token
- webhook token
- API keys
- full cookies/session data

Important prior observation:

Session logs were getting unorganized. It is reasonable to put logs into a dedicated folder in the repo or runtime directory, but avoid committing large runtime logs unless they are curated session notes.

Suggested structure:

```text
logs/
  runtime/
  sessions/
  debug/
```

Possible Git policy:

```text
logs/runtime/*.log ignored
logs/sessions/*.md committed if curated
```

---

## 17. Disk and Runtime Constraints on Raven

Because Raven currently boots from a 32 GB SATA M.2 SSD:

Avoid:

- huge local browser caches
- committing virtual environments
- giant Playwright downloads unless necessary
- unbounded logs
- Docker images unless storage is expanded
- large database bloat from duplicate listings

Recommended:

```bash
df -h
ncdu ~
journalctl --disk-usage
```

If Playwright is used on Raven, monitor disk after browser install.

---

## 18. Browser Automation Constraints

Raven is good for lightweight automation, not a browser farm.

### Hardware expectation

- CPU is weak by modern standards but acceptable for light scraping.
- RAM is more important than CPU for browser automation.
- 12 GB RAM should be enough for light/moderate scraping.

### Programming impact

- Keep concurrency low.
- Prefer requests/HTML parsing when possible.
- Use Playwright only where necessary.
- Avoid multiple simultaneous Chromium instances unless tested.
- Close browser contexts cleanly.
- Add timeouts.
- Add retries with backoff.
- Log adapter failures without killing the whole run.

---

## 19. Error Handling Expectations

A single bad site, bad listing, or failed adapter should not crash the entire hunt cycle.

Preferred behavior:

```text
for each hunt:
  for each source:
    try adapter
    normalize valid listings
    skip bad listings with reason
    continue next source/hunt
```

Important failures to handle gracefully:

- network timeout
- site layout change
- missing price
- malformed URL
- missing title
- bad env var
- Discord notification failure
- DB locked or unavailable
- duplicate listing

---

## 20. Database Notes

SQLite is the current correct choice.

Programming impact:

- Keep schema simple.
- Avoid redesign unless needed.
- Use migrations or explicit upgrade scripts if schema changes.
- Do not casually delete DB data during normal app startup.

Dev reset command exists:

```bash
python reset_dev_db.py
```

Known output pattern:

```text
Vulture dev DB reset complete.
  Hunts cleared:    X
  Listings cleared: Y
  Database kept:    data\vulture.db
```

Be careful that reset scripts should be clearly dev-only.

---

## 21. Deployment Direction

Current manual/tmux approach is fine for active development.

Long-term Raven deployment should become systemd-based.

Likely services:

```text
vulture-bot.service       -> runs discord_bot.py
vulture-cycle.service     -> runs one main.py cycle
vulture-cycle.timer       -> repeats vulture-cycle.service
```

This is cleaner than one long-running custom scheduler process if the job is simply periodic.

Alternative:

```text
vulture-scheduler.service -> runs an internal scheduler loop
```

Use systemd when the codebase is stable enough that manual tmux sessions are annoying.

---

## 22. What Not To Break

Do not break these working assumptions without a deliberate migration:

- DB-backed hunts.
- Discord lifecycle commands.
- `main.py` as one hunt cycle.
- `discord_bot.py` as command/control.
- Craigslist as baseline adapter.
- `.env` outside Git.
- deterministic rule execution.
- dedupe by listing link.
- Raven as low-power headless target.

---

## 23. Good Next Programming Targets

Best next targets for Vulture 2.0 work:

1. Clean project docs and session logs.
2. Confirm current repo state on Windows and GitHub.
3. Pull repo onto Raven or update existing clone.
4. Confirm `.env` on Raven.
5. Confirm bot runs from Raven.
6. Confirm `main.py` runs one hunt cycle from Raven.
7. Add or clean up tests around DB hunt lifecycle.
8. Add title intelligence tests.
9. Add structured category schemas.
10. Improve natural-language-to-hunt translation.
11. Build scheduler/systemd setup once manual tests are clean.

---

## 24. Suggested Test Cases for Upcoming 2.0 Work

### TV intent

Input:

```text
Find me a 75 inch 4K TV under $500 near Houston
```

Expected structured output:

```yaml
category: tv
location: houston
max_price: 500
search_terms:
  - 75 inch 4k tv
attributes:
  min_size_inches: 75
  max_size_inches: 75
  resolution: 4k
exclude_keywords:
  - broken
  - parts
```

### GPU intent

Input:

```text
Find an RTX 3080 or better under $400, card only, not a whole PC
```

Expected structured output:

```yaml
category: gpu
max_price: 400
attributes:
  min_gpu_class: RTX 3080
exclude_keywords:
  - laptop
  - desktop
  - gaming pc
  - computer
  - system
  - tower
  - prebuilt
```

### Vehicle intent

Input:

```text
Find a Toyota Sequoia 2016 or newer under 150k miles
```

Expected structured output:

```yaml
category: vehicle
attributes:
  make: Toyota
  model: Sequoia
  min_year: 2016
  max_miles: 150000
```

---

## 25. Programming Style Preferences for This Project

Prefer:

- explicit over clever
- small modules
- clear logs
- deterministic tests
- readable service/repository layers
- simple SQLite schema evolution
- fixture-based adapter tests
- conservative browser automation
- safe env handling

Avoid:

- huge rewrites
- silent failures
- magic global state
- import side effects
- committing secrets
- tying core logic directly to Discord UI
- making LLM output trusted without validation
- making Facebook/eBay brittleness block the core system

---

## 26. Session Start Checklist

At the start of a programming session, run:

```bash
git status -sb
git branch --show-current
git fetch
git status -sb
git log --oneline --decorate -5
```

Check environment:

```bash
python --version
pip --version
```

On Windows venv:

```powershell
.\.venv\Scripts\python.exe --version
```

On Raven venv:

```bash
.venv/bin/python --version
```

Check Raven disk:

```bash
df -h
```

Check tmux:

```bash
tmux ls
```

---

## 27. Current Mental Model

Vulture 2.0 should become:

```text
Discord-first hunt manager
+ DB-backed hunt lifecycle
+ deterministic rules engine
+ modular source adapters
+ category-aware intent translation
+ lightweight Raven deployment
```

The most important design rule:

> Let the LLM help create structured hunts, but let deterministic Python code execute and test those hunts.
