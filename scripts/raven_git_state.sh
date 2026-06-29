#!/usr/bin/env bash
# Shared git state helpers for Raven deploy scripts.

print_raven_git_state() {
    local label="${1:-git}"

    echo "  ${label}:"
    local branch
    branch="$(git branch --show-current 2>/dev/null || true)"
    if [[ -z "$branch" ]]; then
        echo "    branch: (detached HEAD)"
    else
        echo "    branch: ${branch}"
    fi

    echo "    commit: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

    local upstream
    upstream="$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null || true)"
    if [[ -z "$upstream" ]]; then
        echo "    upstream: (none)"
        echo "    tracking: (no upstream configured)"
    else
        echo "    upstream: ${upstream}"
        local counts behind ahead
        counts="$(git rev-list --left-right --count HEAD...@{u} 2>/dev/null || true)"
        if [[ -n "$counts" ]]; then
            behind="${counts%% *}"
            ahead="${counts##* }"
            echo "    tracking: ahead ${ahead}, behind ${behind} vs ${upstream}"
        else
            echo "    tracking: (unable to compare with ${upstream})"
        fi
    fi

    local origin_main current_head
    origin_main="$(git rev-parse origin/main 2>/dev/null || true)"
    current_head="$(git rev-parse HEAD 2>/dev/null || true)"
    if [[ -z "$origin_main" ]]; then
        echo "    origin/main: (origin/main not available locally)"
    elif [[ "$current_head" == "$origin_main" ]]; then
        echo "    origin/main: current HEAD matches origin/main"
    else
        echo "    origin/main: current HEAD does NOT match origin/main"
        echo "    origin/main commit: $(git rev-parse --short origin/main 2>/dev/null || echo unknown)"
    fi
}

print_dashboard_logs_on_failure() {
    local compose_file="$1"
    local service="${2:-vulture-dashboard}"
    local tail_lines="${3:-80}"

    if ! command -v docker &>/dev/null; then
        echo "  Dashboard logs: skipped (docker unavailable)"
        return 0
    fi
    if [[ ! -f "$compose_file" ]]; then
        echo "  Dashboard logs: skipped (compose file not found: ${compose_file})"
        return 0
    fi

    echo ""
    echo "  Recent dashboard logs (last ${tail_lines} lines):"
    docker compose -f "$compose_file" logs --tail "$tail_lines" "$service" 2>&1 || true
}
