# PROJECT STATUS - 2026-03-29

## Overall Status

Vulture 2.0 refinement work is actively in progress.

The core 2.0 flow remains working:
- Discord hunt creation and lifecycle
- DB-backed hunt storage
- Rules-based LLM translation
- Deterministic execution / filtering
- Craigslist search execution
- Logging and notifications

The project is now in a refinement phase focused on vertical-specific title intelligence rather than architecture work.

## Current Branch / Workstream

- Branch: `feature/title-intelligence-v1`
- Workstream: title-intelligence refinement across multiple verticals

## What Was Accomplished Recently

### GPU vertical

- Tightened GPU title matching for tier-specific models
- Improved handling of NVIDIA / AMD naming variants
- Added standalone-GPU protection so obvious non-card listings are rejected
- Confirmed laptop/system contamination can now be filtered with explicit reasons in logs

### TV vertical

- Improved TV translation for screen-size + resolution hunts
- 4K TV hunts now generate explicit resolution include keywords
- Translation is better for intents like 75-inch / 85-inch 4K TV searches

### Vehicle vertical

- Expanded vehicle coverage for more common makes/models
- Identified remaining gaps through live testing:
  - common make misspellings can still fall through to `general`
  - parts listings can still slip through some vehicle hunts

### Logging / validation

- Filter logging is more useful for title-based rejections
- Live testing confirmed that real false positives can be traced from logs
- Benchmark / smoke coverage has been expanded during this pass

## Current System Behavior

### Working well

- End-to-end hunt creation from Discord still works
- Multi-vertical translation works for GPUs, TVs, and many vehicles
- Deterministic runtime filtering remains intact
- GPU laptop/system false positives are being blocked

### Still imperfect

- Vehicle typo robustness is not finished
- Vehicle parts filtering is incomplete
- Broad/base-model hunts can still be noisier than tier-specific hunts
- Some verticals still depend heavily on title quality from the seller

## Current Assessment

The project is not blocked by architecture.

The main remaining work is quality refinement:
- improve translation robustness for messy real-world intent text
- reduce false positives within each major vertical
- preserve conservative deterministic behavior

## Latest Outcome

Recent live logs showed:
- TV translation behaving correctly for 4K intents
- Prius vehicle translation behaving correctly
- a Hyundai/Elantra typo miss falling into `general`
- a Kia Telluride headlight parts listing slipping through a vehicle hunt

Cursor's latest proposed fix adds:
- curated vehicle make aliases / typo normalization
- expanded vehicle parts exclusions
- updated benchmark coverage for those cases

That refinement pass has been prepared but has not yet been fully carried through to a final reviewed project state at the time of this status file. Based on Cursor's summary, it targets the two remaining live-test problems directly. See latest captured Cursor summary for details. 

## Immediate Next Priorities

1. Apply / verify the latest vehicle typo-normalization and parts-exclusion pass
2. Run live vehicle tests again
3. Confirm logs show clear rejection reasons for parts listings
4. Commit this refinement batch if live behavior matches expectations
5. Continue with the next vertical-specific refinement pass

## Definition of Success for This Phase

This refinement phase is successful when:
- common Discord hunt intents usually translate into the correct vertical
- GPU hunts return cards rather than laptops/systems
- TV hunts respect requested resolution/size constraints
- vehicle hunts return actual vehicles rather than common parts listings
- logs clearly explain why listings were rejected
- the system remains deterministic and stable
