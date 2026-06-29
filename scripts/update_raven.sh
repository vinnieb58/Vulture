#!/usr/bin/env bash
# update_raven.sh
#
# Deploy / update Vulture on Raven.
# Run from the repo root, or override APP_DIR to point elsewhere.
#
# Runtime model (production):
#   vulture-bot.service        — long-running Discord control (discord_bot.py)
#   vulture-scheduler.service  — oneshot hunt cycle (main.py); triggered by timer
#   vulture-scheduler.timer    — schedules hunt cycles every 15 minutes
#
# systemd owns bot/scheduler lifecycle on Raven. tmux is deprecated for normal
# production startup; use it only for optional manual debugging.
#
# Usage (non-interactive — branch picker skipped):
#   APP_DIR="$PWD" BRANCH=main bash scripts/update_raven.sh
#
# Usage (interactive — branch is prompted):
#   bash scripts/update_raven.sh
#
# Environment overrides (all optional):
#   APP_DIR                    — absolute path to the Vulture repo root
#                                (default: $HOME/projects/vulture)
#   BRANCH                     — git branch to deploy; skips branch picker when set
#   PYTHON                     — Python executable inside venv (default: .venv/bin/python)
#   VULTURE_BOT_SERVICE        — systemd unit for the bot (default: vulture-bot.service)
#   VULTURE_SCHEDULER_SERVICE  — oneshot scheduler unit (default: vulture-scheduler.service)
#   VULTURE_SCHEDULER_TIMER    — scheduler timer unit (default: vulture-scheduler.timer)
#   SKIP_SYSTEMD_RESTART       — set to 1 to skip service restarts (tests / dry run)
#   SKIP_DASHBOARD_RESTART     — set to 1 to skip dashboard Docker compose up (tests / dry run)
#   SKIP_PREUPDATE_BACKUP      — set to 1 to skip pre-update mutable-state backup
#
# Options:
#   --no-preupdate-backup      — skip pre-update backup of .env and critical data files
#   FINCH_API_SERVICE          — Finch API unit (default: finch-api.service)
#   FINCH_TELEGRAM_SERVICE     — Finch Telegram unit (default: finch-telegram.service)

set -euo pipefail

SKIP_PREUPDATE_BACKUP="${SKIP_PREUPDATE_BACKUP:-0}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-preupdate-backup)
            SKIP_PREUPDATE_BACKUP=1
            ;;
        *)
            echo "ERROR: unknown option: $1" >&2
            echo "Usage: bash scripts/update_raven.sh [--no-preupdate-backup]" >&2
            exit 2
            ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# Config — detect which values were explicitly provided before applying defaults
# ---------------------------------------------------------------------------
APP_DIR="${APP_DIR:-$HOME/projects/vulture}"
PYTHON="${PYTHON:-.venv/bin/python}"
VULTURE_BOT_SERVICE="${VULTURE_BOT_SERVICE:-vulture-bot.service}"
VULTURE_SCHEDULER_SERVICE="${VULTURE_SCHEDULER_SERVICE:-vulture-scheduler.service}"
VULTURE_SCHEDULER_TIMER="${VULTURE_SCHEDULER_TIMER:-vulture-scheduler.timer}"
FINCH_API_SERVICE="${FINCH_API_SERVICE:-finch-api.service}"
FINCH_TELEGRAM_SERVICE="${FINCH_TELEGRAM_SERVICE:-finch-telegram.service}"

RAVEN_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/raven_finch_services.sh
source "${RAVEN_SCRIPTS_DIR}/raven_finch_services.sh"
# shellcheck source=scripts/raven_preupdate_backup.sh
source "${RAVEN_SCRIPTS_DIR}/raven_preupdate_backup.sh"

# Track whether the caller supplied BRANCH so we know whether to show prompts.
BRANCH_PROVIDED=0
if [[ -n "${BRANCH:-}" ]]; then BRANCH_PROVIDED=1; fi

# Apply defaults — may be overwritten by interactive prompts below.
BRANCH="${BRANCH:-main}"

PIP="${APP_DIR}/.venv/bin/pip"
PYTHON_BIN="${APP_DIR}/${PYTHON}"

# systemd unit names without suffix (for status/journalctl commands)
BOT_UNIT="${VULTURE_BOT_SERVICE%.service}"
SCHEDULER_UNIT="${VULTURE_SCHEDULER_SERVICE%.service}"
SCHEDULER_TIMER_UNIT="${VULTURE_SCHEDULER_TIMER%.timer}"

SYSTEMD_SRC="${APP_DIR}/deploy/systemd"
SYSTEMD_UNITS=(
    vulture-bot.service
    vulture-scheduler.service
    vulture-scheduler.timer
    finch-api.service
    finch-telegram.service
    pelican-backup.service
    pelican-backup.timer
    pelican-monitor.service
    pelican-monitor.timer
)

# ---------------------------------------------------------------------------
# Section helper
# ---------------------------------------------------------------------------
section() {
    echo ""
    echo "========================================"
    echo "  $*"
    echo "========================================"
}

print_service_diagnostics() {
    local unit="$1"
    echo ""
    echo "  --- systemctl status ${unit} ---"
    systemctl status "$unit" --no-pager -l 2>&1 || true
    echo ""
    echo "  --- journalctl -u ${unit} (last 100 lines) ---"
    journalctl -u "$unit" -n 100 --no-pager 2>&1 || true
}

install_systemd_units() {
    section "Installing systemd units"

    for unit in "${SYSTEMD_UNITS[@]}"; do
        if [[ ! -f "${SYSTEMD_SRC}/${unit}" ]]; then
            echo "  ERROR: missing ${SYSTEMD_SRC}/${unit}"
            exit 1
        fi
        sudo cp "${SYSTEMD_SRC}/${unit}" /etc/systemd/system/
        echo "  Installed: ${unit} -> /etc/systemd/system/${unit}"
    done

    sudo systemctl daemon-reload
    echo "  daemon-reload complete"

    sudo systemctl enable "$VULTURE_BOT_SERVICE"
    echo "  Enabled: $VULTURE_BOT_SERVICE"

    sudo systemctl enable "$VULTURE_SCHEDULER_TIMER"
    echo "  Enabled: $VULTURE_SCHEDULER_TIMER"
    echo "  Note: $VULTURE_SCHEDULER_SERVICE is oneshot; the timer triggers it."
    echo "  Note: pelican-backup.timer is installed but not enabled by deploy; use:"
    echo "        ./scripts/install_pelican_timer.sh --enable"
    echo "  Note: pelican-monitor.timer is installed but not enabled by deploy; use:"
    echo "        ./scripts/install_pelican_monitor_timer.sh --enable"
}

restart_systemd_services() {
    section "Restarting systemd services"

    if ! sudo systemctl restart "$VULTURE_BOT_SERVICE"; then
        echo "  ERROR: failed to restart $VULTURE_BOT_SERVICE"
        print_service_diagnostics "$BOT_UNIT"
        exit 1
    fi
    echo "  Restarted: $VULTURE_BOT_SERVICE"

    if ! sudo systemctl restart "$VULTURE_SCHEDULER_TIMER"; then
        echo "  ERROR: failed to restart $VULTURE_SCHEDULER_TIMER"
        print_service_diagnostics "$SCHEDULER_TIMER_UNIT"
        exit 1
    fi
    echo "  Restarted: $VULTURE_SCHEDULER_TIMER"

    if ! restart_finch_services; then
        exit 1
    fi
}

STORAGE_MOUNTPOINT_PARENT="/mnt/storage"

# Optional per-drive paths under /mnt/storage. Unplugged or autofs-managed drives
# must not block dashboard deploy — docker-compose only bind-mounts the parent.
OPTIONAL_STORAGE_MOUNTPOINTS=(
    /mnt/storage/microsd
    /mnt/storage/toshiba_ext
    /mnt/storage/portable_beast
    /mnt/storage/pelican_backup
    /mnt/storage/raven_nvme
    /mnt/storage/roost_spinning_0
)

ensure_storage_mountpoints() {
    local path

    section "Ensuring stable storage mountpoint directories"

    if ! sudo mkdir -p "$STORAGE_MOUNTPOINT_PARENT"; then
        echo "  ERROR: failed to create required mount parent: ${STORAGE_MOUNTPOINT_PARENT}" >&2
        exit 1
    fi
    echo "  Required: ${STORAGE_MOUNTPOINT_PARENT} present"

    for path in "${OPTIONAL_STORAGE_MOUNTPOINTS[@]}"; do
        if [[ -e "$path" && ! -d "$path" ]]; then
            echo "  WARNING: ${path} exists but is not a directory; skipping (optional drive)"
            continue
        fi
        if [[ -d "$path" ]]; then
            echo "  Optional: ${path} already present"
            continue
        fi
        if sudo mkdir -p "$path" 2>/dev/null; then
            echo "  Optional: ${path} created"
        else
            echo "  WARNING: could not create ${path} (drive may be unplugged or autofs-managed); continuing"
        fi
    done

    echo "  Mountpoint setup complete (optional drives may be unplugged)"
}

REBUILD_DOCKER_SCRIPT="${APP_DIR}/scripts/rebuild_docker.sh"

restart_docker_stacks() {
    section "Docker stacks (dashboard + canary)"

    if [[ ! -x "$REBUILD_DOCKER_SCRIPT" ]]; then
        echo "  ERROR: rebuild helper not found or not executable: ${REBUILD_DOCKER_SCRIPT}"
        exit 1
    fi

    if ! "$REBUILD_DOCKER_SCRIPT"; then
        echo "  ERROR: failed to rebuild Docker stacks"
        exit 1
    fi

    echo "  Dashboard: http://raven:8088"
}

show_runtime_status() {
    section "Production runtime status (systemd)"
    echo "  Expected units:"
    echo "    $VULTURE_BOT_SERVICE        — discord_bot.py (long-running)"
    echo "    $VULTURE_SCHEDULER_TIMER    — schedules hunt cycles"
    echo "    $VULTURE_SCHEDULER_SERVICE  — oneshot main.py cycle (inactive between runs is OK)"
    echo "    $FINCH_API_SERVICE            — Finch local API"
    echo "    $FINCH_TELEGRAM_SERVICE       — Finch Telegram bridge"
    echo ""
    echo "  systemctl is-active $BOT_UNIT:"
    systemctl is-active "$BOT_UNIT" 2>&1 || true
    echo ""
    echo "  systemctl is-active $SCHEDULER_TIMER_UNIT:"
    systemctl is-active "$SCHEDULER_TIMER_UNIT" 2>&1 || true
    echo ""
    echo "  systemctl is-active ${FINCH_API_SERVICE%.service}:"
    systemctl is-active "${FINCH_API_SERVICE%.service}" 2>&1 || true
    echo ""
    echo "  systemctl is-active ${FINCH_TELEGRAM_SERVICE%.service}:"
    systemctl is-active "${FINCH_TELEGRAM_SERVICE%.service}" 2>&1 || true
    echo ""
    echo "  systemctl list-timers --all | grep vulture:"
    systemctl list-timers --all 2>&1 | grep vulture || echo "  (no vulture timers listed)"
    echo ""
    systemctl status "$SCHEDULER_TIMER_UNIT" --no-pager -l 2>&1 || true
    echo ""
    systemctl status "$SCHEDULER_UNIT" --no-pager -l 2>&1 || true
    echo ""
    echo "  Note: $SCHEDULER_UNIT inactive/dead after status=0/SUCCESS is expected between timer runs."
}

# ---------------------------------------------------------------------------
# 1. Enter repo
# ---------------------------------------------------------------------------
section "CD into $APP_DIR"
cd "$APP_DIR"

# ---------------------------------------------------------------------------
# 1b. Pre-update backup (before any code changes)
# ---------------------------------------------------------------------------
if [[ "${SKIP_PREUPDATE_BACKUP}" == "1" ]]; then
    section "Skipping pre-update backup (SKIP_PREUPDATE_BACKUP=1)"
else
    run_raven_preupdate_backup "$APP_DIR"
fi

# ---------------------------------------------------------------------------
# 2. Git fetch (needed before branch picker can list branches)
# ---------------------------------------------------------------------------
section "Fetching origin"
git fetch origin

# ---------------------------------------------------------------------------
# 3. Branch picker (interactive when BRANCH was not set in the environment)
# ---------------------------------------------------------------------------
if [[ $BRANCH_PROVIDED -eq 0 ]]; then
    section "Branch selection"

    REMOTE_BRANCHES=()
    while IFS= read -r b; do
        [[ -z "$b" ]] && continue
        REMOTE_BRANCHES+=("$b")
    done < <(
        {
            git branch -r --format='%(refname:short)' | sed 's|^origin/||' | grep -v '^HEAD$' | grep -x 'main' || true
            git branch -r --format='%(refname:short)' | sed 's|^origin/||' | grep -v '^HEAD$' | grep -xv 'main' | sort || true
        }
    )

    echo ""
    echo "  Available branches on origin:"
    echo ""
    for i in "${!REMOTE_BRANCHES[@]}"; do
        printf "  %3d) %s\n" "$((i + 1))" "${REMOTE_BRANCHES[$i]}"
    done
    echo ""
    read -r -p "  Select branch to deploy [default: main]: " BRANCH_INPUT

    if [[ -z "$BRANCH_INPUT" ]]; then
        BRANCH="main"
    elif [[ "$BRANCH_INPUT" =~ ^[0-9]+$ ]]; then
        IDX=$(( BRANCH_INPUT - 1 ))
        if [[ $IDX -lt 0 || $IDX -ge ${#REMOTE_BRANCHES[@]} ]]; then
            echo "  ERROR: number out of range."
            exit 1
        fi
        BRANCH="${REMOTE_BRANCHES[$IDX]}"
    else
        BRANCH="$BRANCH_INPUT"
    fi

    echo "  Selected branch: $BRANCH"
fi

# ---------------------------------------------------------------------------
# 4. Checkout and pull selected branch
# ---------------------------------------------------------------------------
section "Checking out branch: $BRANCH"
git checkout "$BRANCH"

section "Fast-forward pull"
git pull --ff-only origin "$BRANCH"

# ---------------------------------------------------------------------------
# 5. Pre-flight checks
# ---------------------------------------------------------------------------
section "Pre-flight checks"

if [[ ! -f ".env" ]]; then
    echo "  ERROR: .env not found in $APP_DIR"
    echo "  Copy .env.example and fill in secrets before deploying."
    exit 1
fi
echo "  .env present"

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "  ERROR: Python executable not found or not executable: $PYTHON_BIN"
    echo "  Create the venv with:  python3 -m venv .venv && $PIP install -r requirements.txt"
    exit 1
fi
echo "  Python executable present: $PYTHON_BIN"

# ---------------------------------------------------------------------------
# 6. Install / sync dependencies
# ---------------------------------------------------------------------------
section "Installing requirements"
"$PIP" install -r requirements.txt

# ---------------------------------------------------------------------------
# 7. Compile Python source
# ---------------------------------------------------------------------------
section "Compiling Python source (syntax check)"
"$PYTHON_BIN" -m compileall -q adapters crow engine models main.py discord_bot.py
echo "  Compile OK"

# ---------------------------------------------------------------------------
# 8. Validate data layer
# ---------------------------------------------------------------------------
section "Running validate_step1.py"
"$PYTHON_BIN" scripts/validate_step1.py

# ---------------------------------------------------------------------------
# 9. Run one hunt cycle
# ---------------------------------------------------------------------------
section "Running one hunt cycle (main.py)"
"$PYTHON_BIN" main.py

# ---------------------------------------------------------------------------
# 10. Install and restart production systemd units (only after all checks pass)
# ---------------------------------------------------------------------------
if [[ "${SKIP_SYSTEMD_RESTART:-0}" == "1" ]]; then
    section "Skipping systemd install/restart (SKIP_SYSTEMD_RESTART=1)"
else
    install_systemd_units
    restart_systemd_services
    show_runtime_status
fi

if [[ "${SKIP_DASHBOARD_RESTART:-0}" == "1" ]]; then
    section "Skipping Docker stack rebuild (SKIP_DASHBOARD_RESTART=1)"
else
    restart_docker_stacks
fi

echo ""
echo "========================================"
echo "  Raven update complete"
echo "========================================"
echo ""
echo "  Verify on Raven:"
echo "    systemctl status $SCHEDULER_TIMER_UNIT --no-pager -l"
echo "    systemctl status $SCHEDULER_UNIT --no-pager -l"
echo "    systemctl list-timers --all | grep vulture"
echo "    journalctl -u $SCHEDULER_UNIT -n 80 --no-pager"
    echo "    systemctl status $BOT_UNIT --no-pager -l"
    echo "    journalctl -u $BOT_UNIT -n 100 --no-pager"
    echo "    systemctl status ${FINCH_API_SERVICE%.service} --no-pager -l"
    echo "    systemctl status ${FINCH_TELEGRAM_SERVICE%.service} --no-pager -l"
echo "    docker ps"
echo "    curl -I http://localhost:8088"
echo ""
echo "  Docker recovery (if needed):"
echo "    ./scripts/rebuild_docker.sh"
echo ""
echo "  tmux is deprecated for normal production runtime."
echo "  Use it only for optional manual debugging if needed."
