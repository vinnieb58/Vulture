#!/usr/bin/env bash
# update_raven.sh
#
# Deploy / update Vulture on Raven.
# Run from the repo root, or override APP_DIR to point elsewhere.
#
# Usage:
#   bash scripts/update_raven.sh
#   APP_DIR=/opt/vulture BRANCH=main bash scripts/update_raven.sh
#
# Environment overrides (all optional):
#   APP_DIR   — absolute path to the Vulture repo root  (default: $HOME/vulture)
#   BRANCH    — git branch to deploy                     (default: main)
#   PYTHON    — Python executable inside the venv        (default: .venv/bin/python)

set -euo pipefail

# ---------------------------------------------------------------------------
# Config — all values are overridable via environment
# ---------------------------------------------------------------------------
APP_DIR="${APP_DIR:-$HOME/vulture}"
BRANCH="${BRANCH:-main}"
PYTHON="${PYTHON:-.venv/bin/python}"

PIP="${APP_DIR}/.venv/bin/pip"
PYTHON_BIN="${APP_DIR}/${PYTHON}"

# ---------------------------------------------------------------------------
# Section helper
# ---------------------------------------------------------------------------
section() {
    echo ""
    echo "========================================"
    echo "  $*"
    echo "========================================"
}

# ---------------------------------------------------------------------------
# 1. Enter repo
# ---------------------------------------------------------------------------
section "CD into $APP_DIR"
cd "$APP_DIR"

# ---------------------------------------------------------------------------
# 2. Stop tmux sessions
# ---------------------------------------------------------------------------
section "Stopping tmux sessions: bot, scheduler"

for SESSION in bot scheduler; do
    if tmux has-session -t "=$SESSION" 2>/dev/null; then
        echo "  Killing session: $SESSION"
        tmux kill-session -t "=$SESSION"
    else
        echo "  Session not running (skipping): $SESSION"
    fi
done

# ---------------------------------------------------------------------------
# 3. Git update
# ---------------------------------------------------------------------------
section "Fetching origin"
git fetch origin

section "Checking out branch: $BRANCH"
git checkout "$BRANCH"

section "Fast-forward pull"
git pull --ff-only origin "$BRANCH"

# ---------------------------------------------------------------------------
# 4. Pre-flight checks
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
# 5. Install / sync dependencies
# ---------------------------------------------------------------------------
section "Installing requirements"
"$PIP" install -r requirements.txt

# ---------------------------------------------------------------------------
# 6. Compile Python source
# ---------------------------------------------------------------------------
section "Compiling Python source (syntax check)"
"$PYTHON_BIN" -m compileall -q adapters engine models main.py discord_bot.py
echo "  Compile OK"

# ---------------------------------------------------------------------------
# 7. Validate data layer
# ---------------------------------------------------------------------------
section "Running validate_step1.py"
"$PYTHON_BIN" scripts/validate_step1.py

# ---------------------------------------------------------------------------
# 8. Smoke-run one hunt cycle
# ---------------------------------------------------------------------------
section "Running one hunt cycle (main.py)"
"$PYTHON_BIN" main.py

# ---------------------------------------------------------------------------
# 9. Restart discord bot
# ---------------------------------------------------------------------------
section "Starting discord_bot.py in tmux session: bot"
tmux new-session -d -s bot "$PYTHON_BIN discord_bot.py"
echo "  Session 'bot' started"

# ---------------------------------------------------------------------------
# 10. Show running sessions
# ---------------------------------------------------------------------------
section "Active tmux sessions"
tmux ls || echo "  (no sessions listed)"

echo ""
echo "========================================"
echo "  Raven update complete"
echo "========================================"
