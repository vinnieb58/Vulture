#!/usr/bin/env bash
# update_raven.sh
#
# Deploy / update Vulture on Raven.
# Run from the repo root, or override APP_DIR to point elsewhere.
#
# Runtime model (production):
#   vulture-bot.service       — long-running Discord control (discord_bot.py)
#   vulture-scheduler.service — repeats main.py hunt cycles on a configurable interval
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
#   VULTURE_SCHEDULER_SERVICE  — systemd unit for the scheduler (default: vulture-scheduler.service)
#   SKIP_SYSTEMD_RESTART       — set to 1 to skip service restarts (tests / dry run)

set -euo pipefail

# ---------------------------------------------------------------------------
# Config — detect which values were explicitly provided before applying defaults
# ---------------------------------------------------------------------------
APP_DIR="${APP_DIR:-$HOME/projects/vulture}"
PYTHON="${PYTHON:-.venv/bin/python}"
VULTURE_BOT_SERVICE="${VULTURE_BOT_SERVICE:-vulture-bot.service}"
VULTURE_SCHEDULER_SERVICE="${VULTURE_SCHEDULER_SERVICE:-vulture-scheduler.service}"

# Track whether the caller supplied BRANCH so we know whether to show prompts.
BRANCH_PROVIDED=0
if [[ -n "${BRANCH:-}" ]]; then BRANCH_PROVIDED=1; fi

# Apply defaults — may be overwritten by interactive prompts below.
BRANCH="${BRANCH:-main}"

PIP="${APP_DIR}/.venv/bin/pip"
PYTHON_BIN="${APP_DIR}/${PYTHON}"

# systemd unit names without .service suffix (for status/journalctl commands)
BOT_UNIT="${VULTURE_BOT_SERVICE%.service}"
SCHEDULER_UNIT="${VULTURE_SCHEDULER_SERVICE%.service}"

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

restart_systemd_services() {
    section "Restarting systemd services"

    if ! sudo systemctl restart "$VULTURE_BOT_SERVICE"; then
        echo "  ERROR: failed to restart $VULTURE_BOT_SERVICE"
        print_service_diagnostics "$BOT_UNIT"
        exit 1
    fi
    echo "  Restarted: $VULTURE_BOT_SERVICE"

    if ! sudo systemctl restart "$VULTURE_SCHEDULER_SERVICE"; then
        echo "  ERROR: failed to restart $VULTURE_SCHEDULER_SERVICE"
        print_service_diagnostics "$SCHEDULER_UNIT"
        exit 1
    fi
    echo "  Restarted: $VULTURE_SCHEDULER_SERVICE"
}

show_runtime_status() {
    section "Production runtime status (systemd)"
    echo "  Expected units:"
    echo "    $VULTURE_BOT_SERVICE       — discord_bot.py"
    echo "    $VULTURE_SCHEDULER_SERVICE — main.py hunt cycle loop"
    echo ""
    echo "  systemctl is-active $BOT_UNIT:"
    systemctl is-active "$BOT_UNIT" 2>&1 || true
    echo ""
    echo "  systemctl is-active $SCHEDULER_UNIT:"
    systemctl is-active "$SCHEDULER_UNIT" 2>&1 || true
    echo ""
    systemctl status "$BOT_UNIT" --no-pager -l 2>&1 || true
    echo ""
    systemctl status "$SCHEDULER_UNIT" --no-pager -l 2>&1 || true
}

# ---------------------------------------------------------------------------
# 1. Enter repo
# ---------------------------------------------------------------------------
section "CD into $APP_DIR"
cd "$APP_DIR"

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
# 10. Restart production systemd services (only after all checks pass)
# ---------------------------------------------------------------------------
if [[ "${SKIP_SYSTEMD_RESTART:-0}" == "1" ]]; then
    section "Skipping systemd restart (SKIP_SYSTEMD_RESTART=1)"
else
    restart_systemd_services
    show_runtime_status
fi

echo ""
echo "========================================"
echo "  Raven update complete"
echo "========================================"
echo ""
echo "  Verify on Raven:"
echo "    systemctl status $BOT_UNIT --no-pager -l"
echo "    systemctl status $SCHEDULER_UNIT --no-pager -l"
echo "    journalctl -u $BOT_UNIT -n 100 --no-pager"
echo "    journalctl -u $SCHEDULER_UNIT -n 100 --no-pager"
echo ""
echo "  tmux is deprecated for normal production runtime."
echo "  Use it only for optional manual debugging if needed."
