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
# Usage (non-interactive — all prompts skipped):
#   APP_DIR="$PWD" BRANCH=main SCHEDULER_INTERVAL_SECONDS=900 bash scripts/update_raven.sh
#
# Usage (interactive — branch and interval are prompted):
#   bash scripts/update_raven.sh
#   BRANCH=main bash scripts/update_raven.sh          # branch fixed, interval prompted
#   SCHEDULER_INTERVAL_SECONDS=600 bash scripts/update_raven.sh  # interval fixed, branch prompted
#
# Environment overrides (all optional):
#   APP_DIR                    — absolute path to the Vulture repo root
#                                (default: $HOME/projects/vulture)
#   BRANCH                     — git branch to deploy; skips branch picker when set
#   PYTHON                     — Python executable inside venv (default: .venv/bin/python)
#   SCHEDULER_INTERVAL_SECONDS — seconds between hunt cycles;  skips interval prompt when set

set -euo pipefail

# ---------------------------------------------------------------------------
# Config — detect which values were explicitly provided before applying defaults
# ---------------------------------------------------------------------------
APP_DIR="${APP_DIR:-$HOME/projects/vulture}"
PYTHON="${PYTHON:-.venv/bin/python}"

# Track whether the caller supplied BRANCH / SCHEDULER_INTERVAL_SECONDS so we
# know whether to show interactive prompts later.
BRANCH_PROVIDED=0
if [[ -n "${BRANCH:-}" ]]; then BRANCH_PROVIDED=1; fi

SCHEDULER_INTERVAL_PROVIDED=0
if [[ -n "${SCHEDULER_INTERVAL_SECONDS:-}" ]]; then SCHEDULER_INTERVAL_PROVIDED=1; fi

# Apply defaults — these values may be overwritten by interactive prompts below.
BRANCH="${BRANCH:-main}"
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
# 2. Safety check: detect unexpected tmux sessions before touching anything
# ---------------------------------------------------------------------------
section "Checking tmux sessions"

KNOWN_SESSIONS="bot scheduler"
EXTRA_SESSIONS=()

# Collect session names that are not in KNOWN_SESSIONS.
while IFS= read -r s; do
    [[ -z "$s" ]] && continue
    found=0
    for k in $KNOWN_SESSIONS; do [[ "$s" == "$k" ]] && found=1 && break; done
    [[ $found -eq 0 ]] && EXTRA_SESSIONS+=("$s")
done < <(tmux ls 2>/dev/null | awk -F: '{print $1}')

if [[ ${#EXTRA_SESSIONS[@]} -gt 0 ]]; then
    echo ""
    echo "  WARNING: Found tmux session(s) that are NOT 'bot' or 'scheduler':"
    echo ""
    for S in "${EXTRA_SESSIONS[@]}"; do
        echo "  ---- Session: $S ----"
        echo "  Inspect panes:"
        echo "    tmux list-panes -t \"$S\" -F '#{pane_index}: #{pane_current_command} #{pane_current_path}'"

        # Show a live tail of the session output if possible.
        echo "  Recent output:"
        tmux capture-pane -t "$S" -p 2>/dev/null | tail -n 10 | sed 's/^/    /' || true

        # Strong warning if any pane looks like it's running Python/bot code.
        if tmux list-panes -t "$S" -F '#{pane_current_command}' 2>/dev/null \
               | grep -qiE 'python|main\.py|discord_bot'; then
            echo ""
            echo "  *** STRONG WARNING: session '$S' appears to be running a Python"
            echo "  *** process (possibly discord_bot.py or main.py).  Starting a new"
            echo "  *** 'bot' session while this one is alive may create duplicate"
            echo "  *** bot runtimes."
        fi
        echo ""
    done

    echo "  Unknown tmux sessions were found.  What do you want to do?"
    echo "  [c] continue without touching them"
    echo "  [k] kill all unknown sessions listed above"
    echo "  [a] abort  (default)"
    echo ""
    read -r -p "  Choice [c/k/a]: " TMUX_CHOICE

    case "${TMUX_CHOICE,,}" in
        c)
            echo "  Continuing — unknown sessions left as-is."
            ;;
        k)
            echo "  Killing unknown sessions..."
            for S in "${EXTRA_SESSIONS[@]}"; do
                echo "    Killing: $S"
                tmux kill-session -t "$S" 2>/dev/null || echo "    (already gone: $S)"
            done
            echo "  Done."
            ;;
        *)
            echo "  Aborted."
            exit 1
            ;;
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
# 4. Git fetch (needed before branch picker can list branches)
# ---------------------------------------------------------------------------
section "Fetching origin"
git fetch origin

# ---------------------------------------------------------------------------
# 5. Branch picker (interactive when BRANCH was not set in the environment)
# ---------------------------------------------------------------------------
if [[ $BRANCH_PROVIDED -eq 0 ]]; then
    section "Branch selection"

    # Build sorted branch list: main first, then everything else alphabetically.
    REMOTE_BRANCHES=()
    while IFS= read -r b; do
        [[ -z "$b" ]] && continue
        # Strip "origin/" prefix.
        b="${b#origin/}"
        # Skip HEAD pointer.
        [[ "$b" == "HEAD" ]] && continue
        REMOTE_BRANCHES+=("$b")
    done < <(
        git branch -r --format='%(refname:short)' \
            | sed 's|^origin/||' \
            | grep -v '^HEAD' \
            | sort \
            | { grep -x 'main' || true; grep -xv 'main' || true; }
    )

    # Re-read properly with main-first ordering.
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
# 6. Scheduler interval picker (interactive when not set in the environment)
# ---------------------------------------------------------------------------
if [[ $SCHEDULER_INTERVAL_PROVIDED -eq 0 ]]; then
    section "Scheduler interval"
    echo ""
    read -r -p "  Scheduler interval in seconds [default: 900]: " INTERVAL_INPUT

    if [[ -z "$INTERVAL_INPUT" ]]; then
        SCHEDULER_INTERVAL_SECONDS=900
    elif [[ "$INTERVAL_INPUT" =~ ^[0-9]+$ ]] && [[ "$INTERVAL_INPUT" -gt 0 ]]; then
        SCHEDULER_INTERVAL_SECONDS="$INTERVAL_INPUT"
    else
        echo "  ERROR: interval must be a positive integer."
        exit 1
    fi

    echo "  Scheduler interval: ${SCHEDULER_INTERVAL_SECONDS}s"
fi

# ---------------------------------------------------------------------------
# 7. Checkout and pull selected branch
# ---------------------------------------------------------------------------
section "Checking out branch: $BRANCH"
git checkout "$BRANCH"

section "Fast-forward pull"
git pull --ff-only origin "$BRANCH"

# ---------------------------------------------------------------------------
# 8. Pre-flight checks
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
# 9. Install / sync dependencies
# ---------------------------------------------------------------------------
section "Installing requirements"
"$PIP" install -r requirements.txt

# ---------------------------------------------------------------------------
# 10. Compile Python source
# ---------------------------------------------------------------------------
section "Compiling Python source (syntax check)"
"$PYTHON_BIN" -m compileall -q adapters crow engine models main.py discord_bot.py
echo "  Compile OK"

# ---------------------------------------------------------------------------
# 11. Validate data layer
# ---------------------------------------------------------------------------
section "Running validate_step1.py"
"$PYTHON_BIN" scripts/validate_step1.py

# ---------------------------------------------------------------------------
# 12. Smoke-run one hunt cycle
# ---------------------------------------------------------------------------
section "Running one hunt cycle (main.py)"
"$PYTHON_BIN" main.py

# ---------------------------------------------------------------------------
# 13. Start discord bot
# ---------------------------------------------------------------------------
section "Starting discord_bot.py in tmux session: bot"
tmux new-session -d -s bot -c "$APP_DIR" "$PYTHON_BIN discord_bot.py"
echo "  Session 'bot' started"

# ---------------------------------------------------------------------------
# 14. Start scheduler
# ---------------------------------------------------------------------------
section "Starting scheduler in tmux session: scheduler"
tmux new-session -d -s scheduler -c "$APP_DIR" \
    "while true; do $PYTHON_BIN main.py; sleep $SCHEDULER_INTERVAL_SECONDS; done"
echo "  Session 'scheduler' started  (interval: ${SCHEDULER_INTERVAL_SECONDS}s)"

# ---------------------------------------------------------------------------
# 15. Show running sessions
# ---------------------------------------------------------------------------
section "Active tmux sessions"
echo "  Expected: bot (Discord control), scheduler (hunt cycles every ${SCHEDULER_INTERVAL_SECONDS}s)"
echo ""
tmux ls || echo "  (no sessions listed)"

echo ""
echo "========================================"
echo "  Raven update complete"
echo "========================================"
