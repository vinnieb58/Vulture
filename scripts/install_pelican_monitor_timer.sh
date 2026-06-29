#!/usr/bin/env bash
# Install Pelican backup monitor systemd timer on Raven.
# Does not run a monitor check during install.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${APP_DIR}/deploy/systemd"
SERVICE="pelican-monitor.service"
TIMER="pelican-monitor.timer"

usage() {
    cat <<'EOF'
Usage: ./scripts/install_pelican_monitor_timer.sh [--enable]

Copy Pelican monitor systemd units to /etc/systemd/system/, run daemon-reload,
and optionally enable/start the six-hour monitor timer.

Options:
  --enable   Enable and start pelican-monitor.timer after install
  --help     Show this help

Manual one-shot monitor run after install:
  sudo systemctl start pelican-monitor.service
  journalctl -u pelican-monitor.service -n 100 --no-pager

Inspect aggregate status:
  cat data/backup_monitor_status.json

Disable / rollback:
  sudo systemctl disable --now pelican-monitor.timer
  sudo rm -f /etc/systemd/system/pelican-monitor.service /etc/systemd/system/pelican-monitor.timer
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
    echo "Note: ${SERVICE} is oneshot and must remain disabled; the timer triggers it."
    systemctl is-enabled "${SERVICE}" 2>/dev/null || true
    systemctl list-timers --all | grep pelican-monitor || true
else
    echo "Units installed. To enable the six-hour monitor timer:"
    echo "  sudo systemctl enable --now ${TIMER}"
fi
