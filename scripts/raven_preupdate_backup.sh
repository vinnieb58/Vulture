#!/usr/bin/env bash
# raven_preupdate_backup.sh — small pre-update backup of critical mutable state.
#
# Writes timestamped snapshots under:
#   /mnt/storage/pelican_backup/raven-preupdate/
#
# Security: backups include .env. Output lists filenames/counts only — never secret values.
#
# Environment overrides (optional):
#   RAVEN_PREUPDATE_REPO_ROOT
#   RAVEN_PREUPDATE_PELICAN_TARGET=/mnt/storage/pelican_backup
#   RAVEN_PREUPDATE_RETENTION_COUNT=20

set -euo pipefail

RAVEN_PREUPDATE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

run_raven_preupdate_backup() {
    local app_dir="${1:?APP_DIR required}"

    section "Pre-update backup (critical mutable state)"

    local python_args=(
        "${RAVEN_PREUPDATE_SCRIPT_DIR}/raven_preupdate_backup.py"
        --repo-root "${RAVEN_PREUPDATE_REPO_ROOT:-${app_dir}}"
        --pelican-target "${RAVEN_PREUPDATE_PELICAN_TARGET:-/mnt/storage/pelican_backup}"
        --retention-count "${RAVEN_PREUPDATE_RETENTION_COUNT:-20}"
    )

    local output
    local status=0
    if ! output="$(python3 "${python_args[@]}" 2>&1)"; then
        status=1
    fi

    if [[ $status -eq 0 ]]; then
        while IFS= read -r line; do
            case "$line" in
                *"Backup path:"*)
                    echo "  ${line#raven-preupdate-backup: INFO: }"
                    ;;
                *"Files included:"*)
                    echo "  ${line#raven-preupdate-backup: INFO: }"
                    ;;
                *"Pruned older backups:"*)
                    echo "  ${line#raven-preupdate-backup: INFO: }"
                    ;;
            esac
        done <<<"$output"
        return 0
    fi

    local warning_line
    warning_line="$(printf '%s\n' "$output" | sed -n 's/^raven-preupdate-backup: WARNING: //p' | head -n 1)"
    if [[ -n "$warning_line" ]]; then
        echo "  WARNING: pre-update backup skipped: ${warning_line}"
    else
        echo "  WARNING: pre-update backup skipped (backup target unavailable or copy failed)"
    fi
    return 0
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    section() {
        echo ""
        echo "========================================"
        echo "  $*"
        echo "========================================"
    }

    APP_DIR="${APP_DIR:-$HOME/projects/vulture}"
    run_raven_preupdate_backup "$APP_DIR"
fi
