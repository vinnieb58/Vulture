#!/usr/bin/env bash
# update_raven_quick.sh
#
# Fast Raven deploy: pull code, sync deps, refresh systemd units, restart bot/timer,
# and rebuild Docker compose stacks — without running a full hunt cycle.
#
# For Docker-only rebuilds (no git/systemd), use:
#   scripts/rebuild_docker.sh
#
# For full validation (validate_step1.py + one immediate main.py cycle), use:
#   scripts/update_raven.sh
#
# Usage:
#   cd ~/projects/vulture
#   ./scripts/update_raven_quick.sh
#
#   ./scripts/update_raven_quick.sh --run-once    # quick deploy + one scheduler cycle
#   ./scripts/update_raven_quick.sh --no-docker        # skip Docker stack rebuild/restart
#   ./scripts/update_raven_quick.sh --no-services      # skip systemd restarts
#   ./scripts/update_raven_quick.sh --rebuild-dashboard # force dashboard image rebuild
#   ./scripts/update_raven_quick.sh --help
#
# Environment overrides (optional):
#   APP_DIR                    — repo root (default: $HOME/projects/vulture)
#   PYTHON                     — venv python path relative to APP_DIR (default: .venv/bin/python)
#   VULTURE_BOT_SERVICE        — default: vulture-bot.service
#   VULTURE_SCHEDULER_SERVICE  — default: vulture-scheduler.service
#   VULTURE_SCHEDULER_TIMER    — default: vulture-scheduler.timer

set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/projects/vulture}"
PYTHON="${PYTHON:-.venv/bin/python}"
VULTURE_BOT_SERVICE="${VULTURE_BOT_SERVICE:-vulture-bot.service}"
VULTURE_SCHEDULER_SERVICE="${VULTURE_SCHEDULER_SERVICE:-vulture-scheduler.service}"
VULTURE_SCHEDULER_TIMER="${VULTURE_SCHEDULER_TIMER:-vulture-scheduler.timer}"

SKIP_DOCKER=0
SKIP_SERVICES=0
RUN_ONCE=0
FORCE_REBUILD_DASHBOARD=0
PRE_UPDATE_HEAD=""

PIP="${APP_DIR}/.venv/bin/pip"
PYTHON_BIN="${APP_DIR}/${PYTHON}"
REBUILD_DOCKER_SCRIPT="${APP_DIR}/scripts/rebuild_docker.sh"
DASHBOARD_COMPOSE_FILE="${APP_DIR}/docker-compose.dashboard.yml"
CANARY_COMPOSE_FILE="${APP_DIR}/docker-compose.canary.yml"

DASHBOARD_CHANGE_PATHS=(
    dashboard/
    docker-compose.dashboard.yml
    dashboard/Dockerfile
    dashboard/requirements.txt
)

BOT_UNIT="${VULTURE_BOT_SERVICE%.service}"
SCHEDULER_UNIT="${VULTURE_SCHEDULER_SERVICE%.service}"
SCHEDULER_TIMER_UNIT="${VULTURE_SCHEDULER_TIMER%.timer}"

usage() {
    cat <<'EOF'
Usage: update_raven_quick.sh [OPTIONS]

Fast Raven deploy without an immediate full hunt cycle.

Options:
  --no-docker          Skip Docker stack rebuild/restart
  --no-services        Skip systemd unit install and service restarts
  --rebuild-dashboard  Force dashboard image rebuild/recreate
  --run-once           After deploy, run one scheduler cycle via:
                       systemctl start vulture-scheduler.service
  --help               Show this help and exit

Examples:
  ./scripts/update_raven_quick.sh
  ./scripts/update_raven_quick.sh --run-once
  ./scripts/update_raven_quick.sh --no-docker --no-services

Full deploy (validation + immediate hunt cycle):
  ./scripts/update_raven.sh
EOF
}

section() {
    echo ""
    echo "========================================"
    echo "  $*"
    echo "========================================"
}

print_git_state() {
    local label="$1"
    echo "  ${label}:"
    echo "    branch: $(git branch --show-current 2>/dev/null || echo '(detached)')"
    echo "    commit: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
}

unit_source_exists() {
    local unit="$1"
    [[ -f "${APP_DIR}/deploy/systemd/${unit}" || -f "/etc/systemd/system/${unit}" ]]
}

print_unit_state() {
    local unit="$1"
    local note="${2:-}"

    local enabled="not-found"
    local active="not-found"

    if systemctl is-enabled "$unit" &>/dev/null; then
        enabled="$(systemctl is-enabled "$unit" 2>/dev/null || echo unknown)"
    fi

    if systemctl is-active "$unit" &>/dev/null; then
        active="$(systemctl is-active "$unit" 2>/dev/null || echo unknown)"
    fi

    echo "  ${unit}: enabled=${enabled} active=${active}${note}"
}

install_systemd_units() {
    local unit_dir="${APP_DIR}/deploy/systemd"

    section "Installing systemd units"

    if [[ ! -d "$unit_dir" ]]; then
        echo "  WARNING: ${unit_dir} not found; skipping unit install"
        return 0
    fi

    local copied=0
    shopt -s nullglob
    for unit_file in "$unit_dir"/*.service "$unit_dir"/*.timer; do
        local unit_name
        unit_name="$(basename "$unit_file")"
        echo "  Installing ${unit_name}"
        sudo cp "$unit_file" "/etc/systemd/system/${unit_name}"
        copied=1
    done
    shopt -u nullglob

    if [[ $copied -eq 0 ]]; then
        echo "  WARNING: no .service or .timer files found in ${unit_dir}"
        return 0
    fi

    sudo systemctl daemon-reload
    echo "  systemd daemon-reload complete"
}

restart_systemd_services() {
    section "Restarting systemd services"

    if unit_source_exists "$VULTURE_BOT_SERVICE"; then
        if ! sudo systemctl enable "$VULTURE_BOT_SERVICE"; then
            echo "  ERROR: failed to enable ${VULTURE_BOT_SERVICE}"
            exit 1
        fi
        if ! sudo systemctl restart "$VULTURE_BOT_SERVICE"; then
            echo "  ERROR: failed to restart ${VULTURE_BOT_SERVICE}"
            systemctl status "$BOT_UNIT" --no-pager -l 2>&1 || true
            exit 1
        fi
        echo "  Restarted: ${VULTURE_BOT_SERVICE}"
    else
        echo "  Skipped (not present): ${VULTURE_BOT_SERVICE}"
    fi

    if unit_source_exists "$VULTURE_SCHEDULER_TIMER"; then
        if ! sudo systemctl enable "$VULTURE_SCHEDULER_TIMER"; then
            echo "  ERROR: failed to enable ${VULTURE_SCHEDULER_TIMER}"
            exit 1
        fi
        if ! sudo systemctl restart "$VULTURE_SCHEDULER_TIMER"; then
            echo "  ERROR: failed to restart ${VULTURE_SCHEDULER_TIMER}"
            systemctl status "$SCHEDULER_TIMER_UNIT" --no-pager -l 2>&1 || true
            exit 1
        fi
        echo "  Restarted: ${VULTURE_SCHEDULER_TIMER}"
    else
        echo "  Skipped (not present): ${VULTURE_SCHEDULER_TIMER}"
        if unit_source_exists "$VULTURE_SCHEDULER_SERVICE"; then
            echo "  NOTE: ${VULTURE_SCHEDULER_SERVICE} exists but no timer unit was found."
            echo "        Scheduler service is not restarted automatically (use --run-once or full deploy)."
        fi
    fi
}

run_scheduler_once() {
    section "Running one scheduler cycle (--run-once)"

    if ! unit_source_exists "$VULTURE_SCHEDULER_SERVICE"; then
        echo "  ERROR: ${VULTURE_SCHEDULER_SERVICE} not found; cannot run one cycle"
        exit 1
    fi

    if ! sudo systemctl start "$VULTURE_SCHEDULER_SERVICE"; then
        echo "  ERROR: failed to start ${VULTURE_SCHEDULER_SERVICE}"
        systemctl status "$SCHEDULER_UNIT" --no-pager -l 2>&1 || true
        exit 1
    fi
    echo "  Started: ${VULTURE_SCHEDULER_SERVICE}"
}

dashboard_files_changed() {
    local path
    local changed_files

    if [[ -z "$PRE_UPDATE_HEAD" ]]; then
        return 1
    fi

    changed_files="$(git diff --name-only "$PRE_UPDATE_HEAD" HEAD 2>/dev/null || true)"
    if [[ -z "$changed_files" ]]; then
        return 1
    fi

    for path in "${DASHBOARD_CHANGE_PATHS[@]}"; do
        if printf '%s\n' "$changed_files" | grep -q "^${path}"; then
            return 0
        fi
    done

    return 1
}

rebuild_docker_stacks() {
    section "Rebuilding Docker stacks"

    if ! command -v docker &>/dev/null; then
        echo "  WARNING: docker not found; skipping stack rebuild"
        return 0
    fi

    if [[ ! -x "$REBUILD_DOCKER_SCRIPT" ]]; then
        echo "  ERROR: rebuild helper not found or not executable: ${REBUILD_DOCKER_SCRIPT}"
        exit 1
    fi

    local rebuild_dashboard=0
    if [[ $FORCE_REBUILD_DASHBOARD -eq 1 ]]; then
        rebuild_dashboard=1
        echo "  Dashboard rebuild: forced (--rebuild-dashboard)"
    elif dashboard_files_changed; then
        rebuild_dashboard=1
        echo "  Dashboard rebuild: performed (dashboard files changed since pre-update HEAD)"
    else
        echo "  Dashboard rebuild: skipped (no dashboard file changes detected)"
    fi

    if [[ $rebuild_dashboard -eq 1 ]]; then
        "$REBUILD_DOCKER_SCRIPT" --dashboard
    fi

    if [[ -f "$CANARY_COMPOSE_FILE" ]]; then
        echo "  Rebuilding Canary stack"
        "$REBUILD_DOCKER_SCRIPT" --file docker-compose.canary.yml
    else
        echo "  Canary stack: skipped (compose file not found)"
    fi
}

show_final_status() {
    section "Final status"

    print_git_state "git"

    echo ""
    echo "  systemd units:"
    print_unit_state "$VULTURE_BOT_SERVICE"
    print_unit_state "$VULTURE_SCHEDULER_TIMER"
    print_unit_state "$VULTURE_SCHEDULER_SERVICE" " (inactive/dead between timer runs is OK)"

    echo ""
    echo "  scheduler timers:"
    systemctl list-timers --all 2>/dev/null | grep -E 'vulture|NEXT|UNIT' || echo "  (no vulture timers listed)"

    echo ""
    echo "  docker stacks:"
    if command -v docker &>/dev/null && [[ -f "$DASHBOARD_COMPOSE_FILE" ]]; then
        docker compose -f "$DASHBOARD_COMPOSE_FILE" ps 2>&1 || echo "  (docker compose ps failed)"
    elif command -v docker &>/dev/null; then
        docker ps 2>&1 || echo "  (docker ps failed)"
    else
        echo "  Skipped (docker unavailable)"
    fi

    echo ""
    echo "  dashboard HTTP:"
    if command -v curl &>/dev/null; then
        if curl -fsS --max-time 10 http://localhost:8088/health 2>/dev/null; then
            echo ""
        else
            echo "  Dashboard health check failed at http://localhost:8088/health"
        fi
    else
        echo "  Skipped (curl not available)"
    fi
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --no-docker)
                SKIP_DOCKER=1
                ;;
            --no-services)
                SKIP_SERVICES=1
                ;;
            --run-once)
                RUN_ONCE=1
                ;;
            --rebuild-dashboard)
                FORCE_REBUILD_DASHBOARD=1
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            *)
                echo "ERROR: unknown option: $1" >&2
                echo "Run with --help for usage." >&2
                exit 2
                ;;
        esac
        shift
    done
}

main() {
    parse_args "$@"

    section "CD into ${APP_DIR}"
    cd "$APP_DIR"

    section "Git state (before update)"
    print_git_state "current"
    PRE_UPDATE_HEAD="$(git rev-parse HEAD 2>/dev/null || true)"

    section "Fetching origin"
    git fetch origin

    section "Updating current branch"
    CURRENT_BRANCH="$(git branch --show-current || true)"
    if [[ -z "$CURRENT_BRANCH" ]]; then
        echo "  WARNING: detached HEAD at $(git rev-parse --short HEAD); skipping pull"
    else
        echo "  Pulling origin/${CURRENT_BRANCH} (fast-forward only)"
        git pull --ff-only origin "$CURRENT_BRANCH"
    fi

    section "Git state (after update)"
    print_git_state "current"

    section "Pre-flight checks"
    if [[ ! -f ".env" ]]; then
        echo "  ERROR: .env not found in ${APP_DIR}"
        echo "  Copy .env.example and fill in secrets before deploying."
        exit 1
    fi
    echo "  .env present"

    if [[ ! -x "$PYTHON_BIN" ]]; then
        echo "  ERROR: Python executable not found or not executable: ${PYTHON_BIN}"
        echo "  Create the venv with: python3 -m venv .venv && ${PIP} install -r requirements.txt"
        exit 1
    fi
    echo "  Python executable present: ${PYTHON_BIN}"

    if [[ -f "requirements.txt" ]]; then
        section "Installing requirements"
        "$PIP" install -r requirements.txt
    else
        section "Installing requirements"
        echo "  Skipped: requirements.txt not found"
    fi

    section "Compiling Python source (syntax check)"
    "$PYTHON_BIN" -m compileall -q adapters crow engine models main.py discord_bot.py
    echo "  Compile OK"

    if [[ $SKIP_SERVICES -eq 0 ]]; then
        install_systemd_units
        restart_systemd_services
    else
        section "Skipping systemd install/restart (--no-services)"
    fi

    if [[ $RUN_ONCE -eq 1 ]]; then
        run_scheduler_once
    fi

    if [[ $SKIP_DOCKER -eq 0 ]]; then
        rebuild_docker_stacks
    else
        section "Skipping Docker rebuild (--no-docker)"
    fi

    show_final_status

    echo ""
    echo "========================================"
    echo "  Raven quick update complete"
    echo "========================================"
    echo ""
    echo "  Full deploy (validation + immediate hunt):"
    echo "    ./scripts/update_raven.sh"
}

main "$@"
