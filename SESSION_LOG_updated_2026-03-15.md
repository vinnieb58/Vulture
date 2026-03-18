# Vulture Session Log

## 2026-03-09
- Confirmed understanding of the architecture and current project files.
- Continued v1.0 implementation work through Cursor.
- Verified scheduler setup and unattended execution path.
- Confirmed logs were working.
- Identified next work after scheduler: continue project evolution toward the next version.
- Stopping point: v1.0 scheduler path verified and project paused for next-version planning.

## 2026-03-11
- Mapped the next version of Vulture.
- Defined the v2.0 direction around Discord-based hunt control and LLM-assisted hunt translation.
- Created project spec materials for Vulture v2.0 and future roadmap documents.
- Clarified that hunt lifecycle control from Discord belongs in v2.0, while YAML-driven hunts remained part of v1.0 planning docs.
- Stopping point: v2.0 concept and spec docs created.

## 2026-03-12
- Resumed Vulture work and reviewed transfer/setup considerations for the final machine.
- Continued implementation and integration work through Cursor.
- Completed initial Git setup and pushed repository state to GitHub.
- Paused after implementing lifecycle flags, rule engine, logging system, and numeric YAML keyword fix.
- Researched public Facebook Marketplace scraping approaches for future adapter work.
- Captured reusable patterns from that research:
  - browser automation first
  - HTML parsing second
  - session/cookie handling likely required
  - proxy support optional later
  - keep Facebook Marketplace as an experimental adapter due to brittleness and policy risk
- Stopping point: v1.0 core stable, future adapter research saved.

## 2026-03-13
- Continued planning for Vulture 2.0 file structure and service/repository concepts.
- Discussed avoiding unnecessary architecture merges and staying aligned with the intended service layer.
- Began Discord bot setup work:
  - bot token planning
  - bot naming
  - invite URL flow
  - guild ID retrieval
  - channel targeting behavior
- Added the bot to the Discord server and connected the environment/config needed to run it.
- Identified next move at the time as continuing Discord bot setup/testing rather than committing too early.
- Stopping point: bot connected and ready for live testing.

## 2026-03-14 — Discord and bot hardening session
- Restarted Discord-side testing and clarified bot behavior.
- Confirmed hunts could be tested through Discord.
- Observed hunt quality issue:
  - a hunt for 75-inch 4K TVs produced some relevant and some unrelated results
  - captured as a future LLM translation specificity problem
- Observed UX/architecture issue:
  - source website selection should eventually be grouped by vertical/type rather than shown as one flat list
- Noted immediate operational concern around secrets:
  - avoid touching `.env`
  - ensure keys are protected
- Hardened startup/config behavior:
  - verified `.env` is not tracked in git
  - confirmed code does not write to `.env`
  - added explicit `load_dotenv()` coverage in `main.py`
  - added startup config summary logging in `discord_bot.py`
  - created `.env.example`
  - improved `.gitignore` cross-platform hygiene
- Confirmed bot startup path:
  - bot token validation fails clearly
  - startup is clean
  - slash commands sync successfully
- Initially hit confusion because older uploaded docs still described YAML as hunt source of truth.
- Resolved that confusion by inspecting the live codebase instead of trusting stale planning docs.
- Confirmed current implemented reality:
  - Discord hunt creation writes to SQLite `hunts`
  - pause/resume/end/list/show all operate on SQLite
  - `main.py` supports `VULTURE_HUNT_SOURCE=db`
  - current runtime setting is DB-backed hunt execution
  - SQLite contains real hunt rows created from Discord testing
  - SQLite listings table contains real scraped results from execution runs
- Created the idea of maintaining a project status ledger and session log to prevent repeating already-completed steps.
- Stopping point: project truth reconciled against live code; next step is end-to-end DB hunt execution verification.

## 2026-03-15 — Translator hardening and runtime constraint session
- Created `PROJECT_STATUS.md` and `SESSION_LOG.md` as living project-history files to prevent repeating already-completed work.
- Confirmed old docs were stale and shifted the working truth source to live code + repo tracking files.
- Verified Discord slash-command hunt creation is already DB-backed and active in the v2.0 path.
- Tested the DB-backed execution flow and identified hunt quality issues rather than pipeline issues.
- Built and iterated on a rules-based translator for intent-driven Discord hunt creation.
- Added translation improvements across several passes:
  - shorthand numeric parsing such as `10k` → `10000`
  - extraction/storage of `max_miles`, `min_capacity_gb`, and other structured hints
  - Craigslist-safe location validation
  - better vehicle vertical detection
  - better vehicle make/model extraction
  - alphanumeric model support like `rav4`, `4runner`, and `f-150`
  - RAM-specific translation behavior
  - stronger vehicle excludes for parts, wheels/tires, and collectibles
- Found and fixed a live failure where an invalid location like `mandeville louisiana` caused a bad Craigslist subdomain attempt.
- Improved the UX so rejected invalid locations are visible back in Discord instead of only being logged on the server.
- Added runtime rule support for:
  - `min_price`
  - `max_miles`
  - `min_capacity_gb`
- Implemented conservative title-based enforcement:
  - listings are only rejected when mileage or RAM capacity can be clearly parsed from the title
  - ambiguous or missing data is allowed through rather than guessed
- Ran live Discord and runtime tests that showed:
  - TV hunts improved from earlier noisy behavior
  - DDR4/DDR5 RAM hunts filtered junk better
  - vehicle hunts became more specific
  - Porsche intent stopped degrading into malformed generic query text
  - RAV4-style model parsing now remains make+model instead of collapsing to make-only
- Confirmed the lingering `mandeville louisiana` warning was user-entered location input, not hidden state corruption.
- Added future UX note for command naming:
  - `/hunt ...` for fast LLM-based hunting
  - `/huntmanual_...` for manual hunts
  - `/huntadmin_...` for admin/lifecycle commands
- Added runtime/project hygiene note:
  - line-ending warnings after commit were just Git LF/CRLF normalization warnings, not code problems
- Committed translator/model-parsing/location-validation work and then committed runtime constraint enforcement work.
- Stopping point:
  - DB-backed Discord hunts are live
  - translator is materially improved
  - runtime now enforces mileage and RAM capacity constraints conservatively
  - next step is continued tightening of vertical-specific runtime filtering, likely starting with TVs and further vehicle refinement
