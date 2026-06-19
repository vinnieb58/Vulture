#!/usr/bin/env bash
# pelican_backup.sh — Pelican v1 Raven recovery bundle backup (Step 1).
#
# Creates a timestamped recovery bundle on the Pelican backup volume.
# Read-only with respect to production services, mounts, and secrets sources.
#
# Run on Raven from the Vulture repo root:
#   bash scripts/pelican_backup.sh
#
# Environment overrides (optional):
#   PELICAN_REPO_ROOT=/home/vinnieb58/projects/vulture
#   PELICAN_BACKUP_TARGET=/mnt/storage/pelican_backup
#   PELICAN_DB_PATH=/home/vinnieb58/projects/vulture/data/vulture.db
#   PELICAN_ENV_PATH=/home/vinnieb58/projects/vulture/.env
#   PELICAN_RETENTION_COUNT=14
#   PELICAN_MOUNT_TIMEOUT=5.0
#
# Security: anyone with read access to the backup target can read Raven secrets
# copied from .env. Restrict Pelican filesystem permissions accordingly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

exec python3 "${SCRIPT_DIR}/pelican_backup.py" \
  --repo-root "${PELICAN_REPO_ROOT:-${REPO_ROOT}}" \
  --backup-target "${PELICAN_BACKUP_TARGET:-/mnt/storage/pelican_backup}" \
  --db-path "${PELICAN_DB_PATH:-${PELICAN_REPO_ROOT:-${REPO_ROOT}}/data/vulture.db}" \
  --env-path "${PELICAN_ENV_PATH:-${PELICAN_REPO_ROOT:-${REPO_ROOT}}/.env}" \
  --retention-count "${PELICAN_RETENTION_COUNT:-14}" \
  "$@"
