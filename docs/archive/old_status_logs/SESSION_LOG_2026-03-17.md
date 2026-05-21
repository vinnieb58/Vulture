# SESSION LOG --- 2026-03-18

## Branch

feature/title-intelligence-v1

## Focus

Improve title-based intelligence without changing architecture.

## Completed Work

### LLM Translation

-   Implemented interpret → expand → structure approach
-   Fixed GPU normalization (GTX → RTX where needed)
-   Improved intent handling across GPUs and RAM

### GPU Improvements

-   Normalized invalid model inputs
-   Improved AMD/NVIDIA detection
-   Cleaned fallback terms

### RAM Improvements

-   Added min_capacity_gb
-   Added min_speed_mhz
-   Supports phrases like "or greater" and "more than"

### Rules Engine

-   Added deterministic title parsing for RAM attributes
-   Conservative filtering (missing data does not reject)

### End-to-End Flow Verified

LLM → adapter_options → hunt_service → rules → filtering

### Testing

-   Multi-vertical tests (GPU + RAM)
-   Clean logs, correct filtering behavior

## Notes

-   Title-only parsing (no body)
-   Approximate thresholds (\>= instead of \>)
-   Some include keywords still too loose

## Next Steps

-   Tighten GPU include keywords
-   Improve filter logging visibility
