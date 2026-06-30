#!/usr/bin/env bash
# pelican_db_snapshot.sh — Pelican Raven database snapshot backup.
#
# Lightweight twice-daily SQLite snapshots to the Pelican backup volume.
# Independent of deploy/update scripts and the daily recovery bundle.
#
# Usage (from repo root on Raven):
#   bash scripts/pelican_db_snapshot.sh
#
# Optional environment overrides:
#   PELICAN_DB_SNAPSHOT_TARGET=/mnt/storage/pelican_backup/raven-db-snapshots
#   PELICAN_DB_SNAPSHOT_RETENTION_DAYS=14
#   PELICAN_REPO_ROOT=/home/vinnieb58/projects/vulture
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

exec python3 "${SCRIPT_DIR}/pelican_db_snapshot.py" \
  --repo-root "${PELICAN_REPO_ROOT:-${REPO_ROOT}}" \
  --snapshot-target "${PELICAN_DB_SNAPSHOT_TARGET:-/mnt/storage/pelican_backup/raven-db-snapshots}" \
  "$@"
