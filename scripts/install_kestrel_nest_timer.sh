#!/usr/bin/env bash
# Install Kestrel Nest SDM poll systemd timer on Raven.
# Does not print or read .env contents.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${APP_DIR}/deploy/systemd"
SERVICE="kestrel-nest-poll.service"
TIMER="kestrel-nest-poll.timer"

usage() {
    cat <<'EOF'
Usage: ./scripts/install_kestrel_nest_timer.sh [--enable]

Copy Kestrel Nest poll systemd units to /etc/systemd/system/, run daemon-reload,
and optionally enable/start the 5-minute timer.

Options:
  --enable   Enable and start kestrel-nest-poll.timer after install
  --help     Show this help

Manual test after install:
  sudo systemctl start kestrel-nest-poll.service
  journalctl -u kestrel-nest-poll.service -n 100 --no-pager

Disable / rollback:
  sudo systemctl disable --now kestrel-nest-poll.timer
  sudo rm -f /etc/systemd/system/kestrel-nest-poll.service /etc/systemd/system/kestrel-nest-poll.timer
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
    systemctl list-timers --all | grep kestrel-nest || true
else
    echo "Units installed. To enable:"
    echo "  sudo systemctl enable --now ${TIMER}"
fi
