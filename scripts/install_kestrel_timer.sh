#!/usr/bin/env bash
# Install Kestrel Smart Meter Texas daily refresh systemd timer on Raven.
# Does not print or read .env contents.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${APP_DIR}/deploy/systemd"
SERVICE="kestrel-smt-refresh.service"
TIMER="kestrel-smt-refresh.timer"

usage() {
    cat <<'EOF'
Usage: ./scripts/install_kestrel_timer.sh [--enable]

Copy Kestrel systemd units to /etc/systemd/system/, run daemon-reload,
and optionally enable/start the daily timer.

Options:
  --enable   Enable and start kestrel-smt-refresh.timer after install
  --help     Show this help

Manual test after install:
  sudo systemctl start kestrel-smt-refresh.service
  journalctl -u kestrel-smt-refresh.service -n 100 --no-pager

Disable / rollback:
  sudo systemctl disable --now kestrel-smt-refresh.timer
  sudo rm -f /etc/systemd/system/kestrel-smt-refresh.service /etc/systemd/system/kestrel-smt-refresh.timer
  sudo systemctl daemon-reload
EOF
}

ENABLE=0
for arg in "$@"; do
    case "$arg" in
        --enable) ENABLE=1 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage; exit 1 ;;
    esac
done

for unit in "$SERVICE" "$TIMER"; do
    if [[ ! -f "${UNIT_DIR}/${unit}" ]]; then
        echo "ERROR: missing unit file ${UNIT_DIR}/${unit}" >&2
        exit 1
    fi
done

echo "Installing ${SERVICE} and ${TIMER} from ${UNIT_DIR}"
sudo cp "${UNIT_DIR}/${SERVICE}" "/etc/systemd/system/${SERVICE}"
sudo cp "${UNIT_DIR}/${TIMER}" "/etc/systemd/system/${TIMER}"
sudo systemctl daemon-reload
echo "systemd daemon-reload complete"

if [[ "$ENABLE" -eq 1 ]]; then
    sudo systemctl enable --now "${TIMER}"
    echo "Enabled and started ${TIMER}"
    systemctl list-timers --all | grep kestrel || true
else
    echo "Units installed. To enable:"
    echo "  sudo systemctl enable --now ${TIMER}"
fi
