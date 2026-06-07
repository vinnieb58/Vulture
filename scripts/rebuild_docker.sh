#!/usr/bin/env bash
# rebuild_docker.sh
#
# Rebuild/restart Vulture Docker compose stacks on Raven.
# No git pull, no systemd restarts, no hunt cycles.
#
# Usage:
#   cd ~/projects/vulture
#   ./scripts/rebuild_docker.sh
#
#   ./scripts/rebuild_docker.sh --file docker-compose.dashboard.yml
#   ./scripts/rebuild_docker.sh --no-build
#   ./scripts/rebuild_docker.sh --help
#
# Environment overrides (optional):
#   APP_DIR — repo root (default: $HOME/projects/vulture)

set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/projects/vulture}"

NO_BUILD=0
SELECTED_FILES=()

# Add HTTP health probes here when a new compose stack exposes a local port.
declare -A STACK_HEALTH_URLS=(
    ["docker-compose.dashboard.yml"]="http://localhost:8088"
)

usage() {
    cat <<'EOF'
Usage: rebuild_docker.sh [OPTIONS]

Rebuild/restart Docker compose stacks defined in this repo.

By default, rebuilds every docker-compose*.yml file in the repo root.

Options:
  --file FILE    Rebuild only this compose file (repeatable)
  --no-build     Restart without rebuilding images
  --help         Show this help and exit

Examples:
  ./scripts/rebuild_docker.sh
  ./scripts/rebuild_docker.sh --file docker-compose.dashboard.yml
  ./scripts/rebuild_docker.sh --no-build --file docker-compose.dashboard.yml

Environment:
  APP_DIR        Repo root (default: $HOME/projects/vulture)
EOF
}

section() {
    echo ""
    echo "========================================"
    echo "  $*"
    echo "========================================"
}

resolve_compose_file() {
    local file="$1"

    if [[ -f "$file" ]]; then
        printf '%s\n' "$file"
        return 0
    fi

    if [[ -f "${APP_DIR}/${file}" ]]; then
        printf '%s\n' "${APP_DIR}/${file}"
        return 0
    fi

    echo "  ERROR: compose file not found: ${file}" >&2
    return 1
}

discover_compose_files() {
    local files=()
    shopt -s nullglob
    files=( "${APP_DIR}"/docker-compose*.yml )
    shopt -u nullglob

    if [[ ${#files[@]} -eq 0 ]]; then
        echo "  WARNING: no docker-compose*.yml files found in ${APP_DIR}"
        return 0
    fi

    local file
    for file in "${files[@]}"; do
        printf '%s\n' "$file"
    done
}

compose_basename() {
    basename "$1"
}

needs_storage_mountpoints() {
    local compose_file="$1"
    [[ "$(compose_basename "$compose_file")" == "docker-compose.dashboard.yml" ]]
}

ensure_storage_mountpoints() {
    section "Ensuring stable storage mountpoint directories"
    sudo mkdir -p \
        /mnt/storage \
        /mnt/storage/microsd \
        /mnt/storage/toshiba_ext \
        /mnt/storage/portable_beast \
        /mnt/storage/pelican_backup \
        /mnt/storage/raven_nvme \
        /mnt/storage/roost_spinning_0
    echo "  Mountpoint directories present (drives may be unplugged)"
}

rebuild_stack() {
    local compose_file="$1"

    section "Rebuilding $(compose_basename "$compose_file")"

    if [[ ! -f "$compose_file" ]]; then
        echo "  ERROR: compose file not found: ${compose_file}"
        exit 1
    fi

    if needs_storage_mountpoints "$compose_file"; then
        ensure_storage_mountpoints
    fi

    if [[ $NO_BUILD -eq 1 ]]; then
        docker compose -f "$compose_file" up -d
        echo "  Restarted (no build): $(compose_basename "$compose_file")"
    else
        docker compose -f "$compose_file" up -d --build
        echo "  Rebuilt: $(compose_basename "$compose_file")"
    fi
}

check_stack_health() {
    local compose_file="$1"
    local base_name
    local url

    base_name="$(compose_basename "$compose_file")"
    url="${STACK_HEALTH_URLS[$base_name]:-}"

    if [[ -z "$url" ]]; then
        return 0
    fi

    echo ""
    echo "  HTTP check (${base_name}): ${url}"
    if command -v curl &>/dev/null; then
        if curl -fsS -o /dev/null -I --max-time 5 "$url" 2>/dev/null; then
            curl -I --max-time 5 "$url" 2>&1 || true
        else
            echo "  Not reachable: ${url}"
        fi
    else
        echo "  Skipped (curl not available)"
    fi
}

show_stack_status() {
    local compose_file="$1"

    echo ""
    echo "  $(compose_basename "$compose_file"):"
    docker compose -f "$compose_file" ps 2>&1 || echo "  (docker compose ps failed)"
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --file|-f)
                if [[ $# -lt 2 ]]; then
                    echo "ERROR: --file requires a value" >&2
                    exit 2
                fi
                SELECTED_FILES+=("$2")
                shift 2
                ;;
            --no-build)
                NO_BUILD=1
                shift
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
    done
}

main() {
    parse_args "$@"

    section "CD into ${APP_DIR}"
    cd "$APP_DIR"

    if ! command -v docker &>/dev/null; then
        echo "  ERROR: docker not found"
        exit 1
    fi

    local compose_files=()
    local resolved
    local file

    if [[ ${#SELECTED_FILES[@]} -gt 0 ]]; then
        for file in "${SELECTED_FILES[@]}"; do
            resolved="$(resolve_compose_file "$file")"
            compose_files+=("$resolved")
        done
    else
        while IFS= read -r file; do
            [[ -z "$file" ]] && continue
            compose_files+=("$file")
        done < <(discover_compose_files)
    fi

    if [[ ${#compose_files[@]} -eq 0 ]]; then
        echo "  Nothing to rebuild."
        exit 0
    fi

    section "Docker stacks to rebuild"
    for file in "${compose_files[@]}"; do
        echo "  - $(compose_basename "$file")"
    done

    for file in "${compose_files[@]}"; do
        rebuild_stack "$file"
    done

    section "Final container status"
    for file in "${compose_files[@]}"; do
        show_stack_status "$file"
        check_stack_health "$file"
    done

    echo ""
    echo "========================================"
    echo "  Docker rebuild complete"
    echo "========================================"
}

main "$@"
