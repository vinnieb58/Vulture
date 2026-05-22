#!/usr/bin/env bash
# update_raven.sh
#
# Deploy / update Vulture on Raven.
# Run from the repo root, or override APP_DIR to point elsewhere.
#
# Runtime model:
#   bot       — long-running Discord command/control process (discord_bot.py)
#   scheduler — repeats main.py hunt cycles on a configurable interval
#
# Usage:
#   bash scripts/update_raven.sh
#   APP_DIR="$PWD" BRANCH=main bash scripts/update_raven.sh
#   BRANCH=main bash scripts/update_raven.sh
#   BRANCH=cursor/my-feature bash scripts/update_raven.sh
#   SCHEDULER_INTERVAL_SECONDS=600 BRANCH=main bash scripts/update_raven.sh
#
# Environment overrides (all optional):
#   APP_DIR                    — absolute path to the Vulture repo root
#                                (default: $HOME/projects/vulture)
#   BRANCH                     — git branch to deploy          (default: main)
#   PYTHON                     — Python executable inside venv (default: .venv/bin/python)
#   SCHEDULER_INTERVAL_SECONDS — seconds between hunt cycles   (default: 900)

set -euo pipefail

# ---------------------------------------------------------------------------
# Config — all values are overridable via environment
# ---------------------------------------------------------------------------
APP_DIR="${APP_DIR:-$HOME/projects/vulture}"
BRANCH="${BRANCH:-main}"
PYTHON="${PYTHON:-.venv/bin/python}"
SCHEDULER_INTERVAL_SECONDS="${SCHEDULER_INTERVAL_SECONDS:-900}"

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
# 2. Safety check: warn about unexpected tmux sessions before touching anything
# ---------------------------------------------------------------------------
section "Checking tmux sessions"

KNOWN_SESSIONS="bot scheduler"
EXTRA_SESSIONS=()

if tmux ls 2>/dev/null | awk -F: '{print $1}' | while IFS= read -r s; do
       found=0
       for k in $KNOWN_SESSIONS; do [[ "$s" == "$k" ]] && found=1 && break; done
       [[ $found -eq 0 ]] && echo "$s"
   done | grep -q .; then

    # Collect the names into an array
    while IFS= read -r s; do
        EXTRA_SESSIONS+=("$s")
    done < <(
        tmux ls 2>/dev/null | awk -F: '{print $1}' | while IFS= read -r s; do
            found=0
            for k in $KNOWN_SESSIONS; do [[ "$s" == "$k" ]] && found=1 && break; done
            [[ $found -eq 0 ]] && echo "$s"
        done
    )
fi

if [[ ${#EXTRA_SESSIONS[@]} -gt 0 ]]; then
    echo ""
    echo "  WARNING: Found tmux session(s) that are NOT 'bot' or 'scheduler':"
    echo ""
    for S in "${EXTRA_SESSIONS[@]}"; do
        echo "  ---- Session: $S ----"
        echo "  Inspect panes:"
        echo "    tmux list-panes -t \"$S\" -F '#{pane_index}: #{pane_current_command} #{pane_current_path}'"
        echo "  Tail output:"
        echo "    tmux capture-pane -t \"$S\" -p | tail -n 40"

        # Check whether any pane in the session looks like it's running Python/bot code
        if tmux list-panes -t "$S" -F '#{pane_current_command}' 2>/dev/null \
               | grep -qiE 'python|main\.py|discord_bot'; then
            echo ""
            echo "  *** STRONG WARNING: session '$S' appears to be running a Python"
            echo "  *** process (possibly discord_bot.py or main.py).  Starting a new"
            echo "  *** 'bot' session while this one is alive may create duplicate"
            echo "  *** bot runtimes.  Inspect and kill it manually before re-running"
            echo "  *** this script, e.g.:"
            echo "  ***   tmux kill-session -t \"$S\""
        fi
        echo ""
    done

    echo "  This script will NOT kill unknown sessions automatically."
    echo "  Resolve the sessions above and re-run, or proceed at your own risk."
    echo ""
    read -r -p "  Continue anyway? [y/N] " REPLY
    case "$REPLY" in
        [yY][eE][sS]|[yY]) echo "  Continuing..." ;;
        *) echo "  Aborted."; exit 1 ;;
    esac
else
    echo "  No unexpected tmux sessions found."
fi

# ---------------------------------------------------------------------------
# 3. Stop managed tmux sessions (bot, scheduler)
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
# 4. Git update
# ---------------------------------------------------------------------------
section "Fetching origin"
git fetch origin

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
"$PYTHON_BIN" -m compileall -q adapters engine models main.py discord_bot.py
echo "  Compile OK"

# ---------------------------------------------------------------------------
# 8. Validate data layer
# ---------------------------------------------------------------------------
section "Running validate_step1.py"
"$PYTHON_BIN" scripts/validate_step1.py

# ---------------------------------------------------------------------------
# 9. Smoke-run one hunt cycle
# ---------------------------------------------------------------------------
section "Running one hunt cycle (main.py)"
"$PYTHON_BIN" main.py

# ---------------------------------------------------------------------------
# 10. Start discord bot
# ---------------------------------------------------------------------------
section "Starting discord_bot.py in tmux session: bot"
tmux new-session -d -s bot -c "$APP_DIR" "$PYTHON_BIN discord_bot.py"
echo "  Session 'bot' started"

# ---------------------------------------------------------------------------
# 11. Start scheduler
# ---------------------------------------------------------------------------
section "Starting scheduler in tmux session: scheduler"
tmux new-session -d -s scheduler -c "$APP_DIR" \
    "while true; do $PYTHON_BIN main.py; sleep $SCHEDULER_INTERVAL_SECONDS; done"
echo "  Session 'scheduler' started  (interval: ${SCHEDULER_INTERVAL_SECONDS}s)"

# ---------------------------------------------------------------------------
# 12. Show running sessions
# ---------------------------------------------------------------------------
section "Active tmux sessions"
echo "  Expected: bot (Discord control), scheduler (hunt cycles every ${SCHEDULER_INTERVAL_SECONDS}s)"
echo ""
tmux ls || echo "  (no sessions listed)"

echo ""
echo "========================================"
echo "  Raven update complete"
echo "========================================"
