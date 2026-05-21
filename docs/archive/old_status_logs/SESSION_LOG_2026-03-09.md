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
  - all 6 slash commands are fully implemented in `discord_bot.py`
  - Discord hunt creation writes to SQLite `hunts`
  - pause/resume/end/list/show all operate on SQLite
  - `main.py` supports `VULTURE_HUNT_SOURCE=db`
  - current runtime setting is DB-backed hunt execution
  - SQLite contains real hunt rows created from Discord testing
  - SQLite listings table contains real scraped results from execution runs
- Identified one live data issue:
  - one hunt uses `craiglist` instead of `craigslist`
- Established the real next step:
  - do not rebuild hunt persistence
  - instead verify the full Discord → SQLite → `main.py` → Discord alert loop end to end
- Created the idea of maintaining a project status ledger and session log to prevent repeating already-completed steps.
- Stopping point: project truth reconciled against live code; next step is end-to-end DB hunt execution verification.
