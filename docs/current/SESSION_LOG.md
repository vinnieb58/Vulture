# SESSION LOG - 2026-03-29

## Branch

`feature/title-intelligence-v1`

## Session Goal

Continue Vulture 2.0 title-intelligence refinement without changing architecture, and push quality improvements across multiple verticals.

## Work Completed This Session

### 1) Re-established project state

- Confirmed v1.0-style core plumbing is already behind us
- Confirmed the active focus is Vulture 2.0 quality refinement, not redesign
- Confirmed current success criteria are tied to usable Discord-driven hunts with deterministic execution

### 2) GPU refinement reviewed and validated

- Reviewed prior GPU tightening work
- Confirmed tier-aware GPU matching had improved
- Confirmed non-card system listings were a real issue
- Directed a focused fix to exclude obvious system/laptop listings from GPU-card hunts
- Reviewed Cursor's implementation approach and approved it as the correct narrow fix
- Reviewed live logs and confirmed the previously problematic laptop listing was filtered correctly
- Determined GPU pass was good enough to move forward

### 3) Planned multi-vertical refinement pass

- Prepared a bounded Cursor prompt covering:
  - GPUs
  - TVs
  - vehicles
  - logging clarity
- Intentionally allowed a slightly larger refinement pass while keeping architecture frozen

### 4) Reviewed Cursor's multi-vertical response

- Assessed Cursor's output as a useful TV + vehicle pass
- Noted that it did not actually include additional GPU work
- Approved the direction for live testing instead of immediate commit

### 5) Reviewed live logs from multi-vertical testing

Observed working behavior:
- TV hunts classified into `tv_home_theater`
- 4K TV intents produced explicit resolution include keywords
- `toyota prius` translated into `vehicles` and returned valid-looking results

Observed remaining problems:
- `hyndai elantra under 15000` translated into `general` instead of `vehicles`
- `kia telluride` hunt still accepted a headlight parts listing as a new result

Conclusion from that review:
- good progress, but not commit-clean yet
- vehicle typo handling and parts filtering still needed one more focused pass

### 6) Reviewed Cursor's final proposed vehicle cleanup pass

Cursor proposed:
- a curated make-alias normalization layer for common vehicle typos
- expanded vehicle-parts exclusions in the default vehicle exclude list
- updated benchmark coverage for typo recognition and parts rejection
- retained deterministic behavior without changing architecture

Cursor reported benchmark success after those edits.

## Outcome at End of Session

The project ended the session in a good refinement state, but not yet fully wrapped into a final accepted / committed batch during the chat.

The latest identified path forward is clear:
- verify the new vehicle typo-normalization + parts-exclusion changes in live runs
- if live behavior matches the benchmark results, commit the batch
- continue to the next refinement pass afterward

## Key Findings

### Confirmed improvements

- GPU hunts can now reject obvious laptops / systems when configured as card hunts
- TV translation quality improved for size + 4K intent handling
- vehicle translation works for at least some direct make/model cases like Prius

### Confirmed gaps

- vehicle make typos still needed normalization based on live testing
- vehicle hunts still needed a stronger parts denylist
- broad/base-title hunts remain inherently noisier than tightly structured ones

## Recommended Next Step

1. Apply or verify Cursor's latest vehicle cleanup changes
2. Run live tests for:
   - `hyundai elantra under 15000`
   - `kia telluride newer than 2020 under 35000`
   - at least one additional vehicle with a common typo or alias
3. Confirm that:
   - typo intent lands in `vehicles`
   - parts listings are rejected with explicit log reasons
   - whole vehicles still pass
4. Commit if clean

## Suggested Commit Boundary

This batch should be committed only after live confirmation that:
- vehicle typo normalization is working
- parts like headlights / liftgates / catalytic converters are being rejected
- no obvious vehicle regressions were introduced

---

# SESSION LOG - 2026-05-28

## Branch/context observed

- Inspected baseline branch: `main` at commit `8e82b42`
- Recent commit trail includes:
  - vertical-aware hunt processing updates
  - vehicle translator v2 fixes
  - Cars.com experimental adapter work
  - eBay recon completion

## What code exists now

- Two runtime entrypoints:
  - `main.py` hunt cycle worker
  - `discord_bot.py` slash-command control runtime
- Adapter registry is live and used by `main.py`:
  - `craigslist` (stable)
  - `offerup` (experimental)
  - `carsdotcom` (experimental)
  - `microcenter` (experimental, Playwright, opt-in only — not in default translated sources)
- DB-backed hunt lifecycle is implemented in service/repository layers.
- Multi-source hunt fan-out is implemented (`source_sites` expansion per source run).
- Deterministic rules engine enforces price, keyword, TV/GPU/RAM/vehicle structured constraints.

## What changed since previous docs

- Updated docs to reflect that adapter registry is already implemented (not planned future work).
- Updated docs to reflect live runtime adapters and probe-only sources.
- Added `docs/current/CODEBASE_STATUS.md` as an implementation-grounded status map.
- Updated architecture/current status/roadmap/programming reference to align with code reality and remove stale branch/planning assumptions.
- Added current verification command sections for Windows and Raven.

## Confirmed working (code-confirmed)

- Discord command dispatch and hunt lifecycle paths exist and are wired.
- Hunt-source mode handling (`yaml`, `db`, `mixed`) is implemented in `main.py`.
- Multi-source hunt execution fan-out is implemented.
- Link-based dedupe in SQLite listings is implemented.
- Translator + rules test suites exist and are runnable via pytest.

## Unconfirmed / caution areas

- OfferUp location targeting remains GeoIP-only (city input is advisory).
- Cars.com reliability across varied network/runtime environments remains experimental.
- Micro Center: runtime adapter registered **experimental**; requires Playwright on Raven; plain HTTP still blocked; not promoted to stable until repeated hunt smoke passes.
- eBay scraping remains probe-only with documented blocking; no runtime adapter.

## 2026-05-31 — Micro Center experimental adapter

- Added `adapters/microcenter.py` (Playwright, `storeid` scoping, returns `[]` on Cloudflare block).
- Registered in `adapters/registry.py` as experimental / `requires_browser` / `location_control: storeid`.
- **Not** added to `engine/source_selection.py` vertical defaults — hunts must set `source_sites: ["microcenter"]` explicitly.
- Raven Playwright probe smoke (May 2026): HTTP 200, `#productGrid li.product_wrapper`, in-stock 7800X3D at storeid 115/141.
- `scripts/smoke_microcenter_adapter.py` for on-host verification.
- Live production reliability of non-craigslist adapters is not asserted as stable.

## What should happen next

1. Keep adapter status evidence-based and avoid optimistic promotion labels.
2. Add repeatable adapter smoke checks and log outcomes in future session entries.
3. Expand tests around runtime loading/fan-out behavior without changing architecture.

## Test execution in this session

- `python3 -m pytest` (repo root): **failed** due to test collection hitting `scripts/smoke_multi_source.py` predecessor `scripts/test_multi_source.py` (script exited via `SystemExit` at import time).
- `python3 -m pytest tests`: **passed** (`210 passed`).

Interpretation:
- Primary maintained test suite under `tests/` is currently green.
- Root-level pytest invocation is currently not clean because script-style files under `scripts/` are collected and exit during import.
