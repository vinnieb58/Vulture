#!/usr/bin/env bash
# Pelican backup monitor — checks all enabled Pelican-managed backup definitions.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${APP_DIR}"

PYTHON="${APP_DIR}/.venv/bin/python3"
if [[ ! -x "${PYTHON}" ]]; then
    PYTHON="$(command -v python3)"
fi

exec "${PYTHON}" -m pelican_monitor "$@"
