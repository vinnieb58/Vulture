"""
Robin daycare photo probe
=========================
Observe/download/organize daycare portal photos locally. Read-only on the portal:
no social sharing, posting, or destructive actions.

Does NOT modify Vulture scheduler, Crow commands, Canary, or hunt runtime.

Usage:
    python experiments/robin/daycare_photo_probe.py --headful --dry-run
    python experiments/robin/daycare_photo_probe.py --headful --limit 10
    python experiments/robin/daycare_photo_probe.py --summary-only

Environment (repo-root .env):
    ROBIN_DAYCARE_USERNAME
    ROBIN_DAYCARE_PASSWORD
    ROBIN_DAYCARE_PORTAL_URL
    ROBIN_SESSION_DIR
    ROBIN_OUTPUT_DIR
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robin.config import RobinConfigError, load_config, setup_logging  # noqa: E402
from robin.portal import PhotoCandidate, RobinPortalError, discover_portal_photos  # noqa: E402
from robin.redact import safe_url_for_log  # noqa: E402
from robin.storage import (  # noqa: E402
    STATUS_DOWNLOADED,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_SKIPPED_DUPLICATE,
    count_manifest_by_status,
    download_and_store,
    fetch_manifest_entries,
    load_known_hashes,
)


@dataclass
class ProbeSummary:
    candidates_found: int = 0
    downloaded: int = 0
    skipped_duplicate: int = 0
    failed: int = 0
    dry_run_listed: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "candidates_found": self.candidates_found,
            "downloaded": self.downloaded,
            "skipped_duplicate": self.skipped_duplicate,
            "failed": self.failed,
            "dry_run_listed": self.dry_run_listed,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Robin daycare photo probe (observe/download/organize only)"
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Run browser headed for manual login/debug and session capture",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and list photo candidates without downloading",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Maximum number of photo candidates to process",
    )
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        default=None,
        help="Only include photos on or after this date when dates are detectable",
    )
    parser.add_argument(
        "--output-dir",
        metavar="PATH",
        default=None,
        help="Override ROBIN_OUTPUT_DIR for this run",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print manifest summary without portal access or downloads",
    )
    return parser.parse_args()


def _parse_since_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"Invalid --since date {value!r}; use YYYY-MM-DD.") from exc


def _print_summary_only(manifest_path: Path) -> int:
    counts = count_manifest_by_status(manifest_path)
    entries = fetch_manifest_entries(manifest_path)
    payload = {
        "manifest_path": str(manifest_path),
        "total_entries": len(entries),
        "by_status": counts,
        "recent": [
            {
                "detected_date": entry.detected_date,
                "status": entry.status,
                "sha256_prefix": entry.sha256[:16] if entry.sha256 else None,
                "downloaded_path": entry.downloaded_path,
            }
            for entry in entries[-10:]
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _log_candidate(candidate: PhotoCandidate, index: int) -> None:
    import logging

    logging.getLogger("robin").info(
        "Candidate %d: date=%s url=%s",
        index,
        candidate.detected_date.isoformat() if candidate.detected_date else "unknown",
        safe_url_for_log(candidate.url),
    )


def _process_candidates(
    candidates: list[PhotoCandidate],
    *,
    config,
    dry_run: bool,
    logger,
) -> ProbeSummary:
    summary = ProbeSummary(candidates_found=len(candidates))
    known_hashes = load_known_hashes(config.manifest_path)

    for candidate in candidates:
        original_name = None
        if candidate.label:
            original_name = f"{candidate.label}.jpg"

        result = download_and_store(
            url=candidate.url,
            output_dir=config.output_dir,
            manifest_path=config.manifest_path,
            source_portal=config.portal_source,
            detected_date=candidate.detected_date,
            known_hashes=known_hashes,
            dry_run=dry_run,
            original_filename=original_name,
        )

        if result.status == STATUS_DOWNLOADED:
            summary.downloaded += 1
            if result.entry:
                known_hashes.add(result.entry.sha256)
            logger.info("Downloaded photo (sha256_prefix=%s)", result.entry.sha256[:16] if result.entry else "?")
        elif result.status == STATUS_SKIPPED_DUPLICATE:
            summary.skipped_duplicate += 1
            logger.info("Skipped duplicate (sha256_prefix=%s)", result.entry.sha256[:16] if result.entry else "?")
        elif result.status == STATUS_DRY_RUN:
            summary.dry_run_listed += 1
        elif result.status == STATUS_FAILED:
            summary.failed += 1
            logger.warning("Download failed: %s", result.error or "unknown")

    return summary


def main() -> int:
    args = parse_args()
    since_date = _parse_since_date(args.since)

    try:
        config = load_config(require_portal_url=not args.summary_only)
    except RobinConfigError as exc:
        import logging

        logging.getLogger("robin").error("%s", exc)
        return 1

    if args.output_dir:
        from dataclasses import replace

        output_dir = Path(args.output_dir).resolve()
        config = replace(
            config,
            output_dir=output_dir,
            manifest_path=output_dir / "manifest.db",
        )

    logger = setup_logging(config.log_level)

    if args.summary_only:
        return _print_summary_only(config.manifest_path)

    if not config.has_portal_url:
        logger.error("ROBIN_DAYCARE_PORTAL_URL is required.")
        return 1

    if args.headful:
        from dataclasses import replace

        config = replace(config, headless=False)

    try:
        candidates = discover_portal_photos(
            config,
            headful=args.headful,
            since_date=since_date,
            limit=args.limit,
        )
    except RobinPortalError as exc:
        logger.error("%s", exc)
        return 1

    if args.dry_run:
        for index, candidate in enumerate(candidates, start=1):
            _log_candidate(candidate, index)
        summary = ProbeSummary(
            candidates_found=len(candidates),
            dry_run_listed=len(candidates),
        )
    else:
        if args.limit is not None:
            candidates = candidates[: max(args.limit, 0)]
        for index, candidate in enumerate(candidates, start=1):
            _log_candidate(candidate, index)
        summary = _process_candidates(candidates, config=config, dry_run=False, logger=logger)

    logger.info(
        "Robin probe complete: candidates=%d downloaded=%d skipped_duplicate=%d failed=%d dry_run=%d",
        summary.candidates_found,
        summary.downloaded,
        summary.skipped_duplicate,
        summary.failed,
        summary.dry_run_listed,
    )
    print(json.dumps({"summary": summary.to_dict()}, indent=2))
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
