#!/usr/bin/env bash
# Install Vulture concert watch systemd timer on Raven.
# Separate from vulture-scheduler (marketplace hunts).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${APP_DIR}/deploy/systemd"
SERVICE="vulture-concert-watches.service"
TIMER="vulture-concert-watches.timer"

usage() {
    cat <<'EOF'
Usage: ./scripts/install_concert_watches_timer.sh [--enable]

Copy Vulture concert watch systemd units to /etc/systemd/system/, run daemon-reload,
and optionally enable/start the timer.

Options:
  --enable   Enable and start vulture-concert-watches.timer after install
  --help     Show this help

Manual test after install:
  sudo systemctl start vulture-concert-watches.service
  journalctl -u vulture-concert-watches.service -n 100 --no-pager

Disable / rollback:
  sudo systemctl disable --now vulture-concert-watches.timer
  sudo rm -f /etc/systemd/system/vulture-concert-watches.service /etc/systemd/system/vulture-concert-watches.timer
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
    systemctl list-timers --all | grep vulture-concert || true
else
    echo "Units installed. To enable:"
    echo "  sudo systemctl enable --now ${TIMER}"
fi
