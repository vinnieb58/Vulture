#!/usr/bin/env bash
# raven_post_reboot_check.sh — focused post-reboot verification for Raven.
#
# Thin wrapper around scripts/raven_healthcheck.sh --post-reboot.
#
# Install on Raven (from repo root):
#   cp scripts/raven_post_reboot_check.sh ~/raven_post_reboot_check.sh
#   chmod +x ~/raven_post_reboot_check.sh
#
# Run after reboot:
#   ~/raven_post_reboot_check.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${SCRIPT_DIR}/raven_healthcheck.sh" --post-reboot
