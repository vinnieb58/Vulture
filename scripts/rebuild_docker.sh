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
# Rebuilds every compose stack (dashboard is always built + force-recreated).
# Advanced: --file, --no-build, --no-cache, --dashboard (dashboard only)
#
# Environment overrides (optional):
#   APP_DIR — repo root (default: $HOME/projects/vulture)

set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/projects/vulture}"

DASHBOARD_COMPOSE_FILE="${APP_DIR}/docker-compose.dashboard.yml"
DASHBOARD_SERVICE="vulture-dashboard"
DASHBOARD_HEALTH_URL="http://localhost:8088/health"

NO_BUILD=0
NO_CACHE=0
DASHBOARD_ONLY=0
SELECTED_FILES=()

# Add HTTP health probes here when a new compose stack exposes a local port.
declare -A STACK_HEALTH_URLS=(
    ["docker-compose.dashboard.yml"]="${DASHBOARD_HEALTH_URL}"
)

usage() {
    cat <<'EOF'
Usage: rebuild_docker.sh [OPTIONS]

Rebuild/restart every Docker compose stack in this repo.

The dashboard (vulture-dashboard) is always explicitly built and
force-recreated so code changes reach the running container.

Options:
  --file FILE    Rebuild only this compose file (repeatable)
  --no-build     Restart without rebuilding images
  --no-cache     Build images without Docker layer cache
  --dashboard    Rebuild dashboard only (advanced; skips canary)
  --help         Show this help and exit

Examples:
  ./scripts/rebuild_docker.sh
  ./scripts/rebuild_docker.sh --file docker-compose.canary.yml

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

is_dashboard_compose_file() {
    local compose_file="$1"
    [[ "$(compose_basename "$compose_file")" == "docker-compose.dashboard.yml" ]]
}

needs_storage_mountpoints() {
    local compose_file="$1"
    is_dashboard_compose_file "$compose_file"
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

dashboard_container_id() {
    docker compose -f "$DASHBOARD_COMPOSE_FILE" ps -q "$DASHBOARD_SERVICE" 2>/dev/null | head -1 || true
}

dashboard_image_id() {
    docker compose -f "$DASHBOARD_COMPOSE_FILE" images -q "$DASHBOARD_SERVICE" 2>/dev/null | head -1 || true
}

dashboard_image_tag() {
    local image_id="$1"
    if [[ -z "$image_id" ]]; then
        echo "(not built yet)"
        return 0
    fi
    docker image inspect --format '{{if .RepoTags}}{{index .RepoTags 0}}{{else}}<untagged>{{end}}' "$image_id" 2>/dev/null || echo unknown
}

print_dashboard_deploy_state() {
    local label="$1"
    local compose_cmd="${2:-}"
    local container_id image_id image_tag created started status running_for

    echo ""
    echo "  ${label}:"
    echo "    compose file: ${DASHBOARD_COMPOSE_FILE}"
    echo "    service:      ${DASHBOARD_SERVICE}"
    if [[ -n "$compose_cmd" ]]; then
        echo "    compose cmd:  ${compose_cmd}"
    fi

    image_id="$(dashboard_image_id)"
    if [[ -n "$image_id" ]]; then
        image_tag="$(dashboard_image_tag "$image_id")"
        echo "    image id:     ${image_id}"
        echo "    image tag:    ${image_tag}"
    else
        echo "    image id:     (not built yet)"
        echo "    image tag:    (n/a)"
    fi

    container_id="$(dashboard_container_id)"
    if [[ -n "$container_id" ]]; then
        created="$(docker inspect --format '{{.Created}}' "$container_id" 2>/dev/null || echo unknown)"
        started="$(docker inspect --format '{{.State.StartedAt}}' "$container_id" 2>/dev/null || echo unknown)"
        status="$(docker inspect --format '{{.State.Status}}' "$container_id" 2>/dev/null || echo unknown)"
        running_for="$(docker ps --filter "id=${container_id}" --format '{{.RunningFor}}' 2>/dev/null | head -1 || echo unknown)"
        echo "    container id: ${container_id}"
        echo "    created:      ${created}"
        echo "    started:      ${started}"
        echo "    status:       ${status}"
        echo "    running for:  ${running_for}"
    else
        echo "    container id: (not running)"
        echo "    created:      (n/a)"
        echo "    started:      (n/a)"
        echo "    status:       (n/a)"
        echo "    running for:  (n/a)"
    fi
}

dashboard_build_args() {
    local git_commit build_timestamp
    git_commit="$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
    build_timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    printf 'BUILD_GIT_COMMIT=%s\nBUILD_TIMESTAMP=%s\n' "$git_commit" "$build_timestamp"
}

rebuild_dashboard_stack() {
    section "Rebuilding dashboard (docker-compose.dashboard.yml)"

    if [[ ! -f "$DASHBOARD_COMPOSE_FILE" ]]; then
        echo "  ERROR: compose file not found: ${DASHBOARD_COMPOSE_FILE}"
        exit 1
    fi

    ensure_storage_mountpoints
    print_dashboard_deploy_state "Before dashboard deploy"

    if [[ $NO_BUILD -eq 1 ]]; then
        local up_cmd=(
            docker compose -f "$DASHBOARD_COMPOSE_FILE"
            up -d --force-recreate "$DASHBOARD_SERVICE"
        )
        echo ""
        echo "  Up command: ${up_cmd[*]}"
        if ! "${up_cmd[@]}"; then
            echo "  ERROR: dashboard recreate failed" >&2
            docker compose -f "$DASHBOARD_COMPOSE_FILE" ps 2>&1 || true
            docker compose -f "$DASHBOARD_COMPOSE_FILE" logs --tail 50 "$DASHBOARD_SERVICE" 2>&1 || true
            exit 1
        fi
        echo "  Restarted dashboard (no build)"
        print_dashboard_deploy_state "After dashboard deploy" "${up_cmd[*]}"
    else
        local build_args_file
        build_args_file="$(mktemp)"
        dashboard_build_args >"$build_args_file"

        local build_cmd=(docker compose -f "$DASHBOARD_COMPOSE_FILE" --env-file "$build_args_file" build)
        if [[ $NO_CACHE -eq 1 ]]; then
            build_cmd+=(--no-cache)
        fi
        build_cmd+=("$DASHBOARD_SERVICE")

        echo ""
        echo "  Build command: ${build_cmd[*]}"
        if ! "${build_cmd[@]}"; then
            rm -f "$build_args_file"
            echo "  ERROR: dashboard image build failed" >&2
            exit 1
        fi

        local up_cmd=(
            docker compose -f "$DASHBOARD_COMPOSE_FILE"
            --env-file "$build_args_file"
            up -d --force-recreate "$DASHBOARD_SERVICE"
        )
        echo "  Up command: ${up_cmd[*]}"
        if ! "${up_cmd[@]}"; then
            rm -f "$build_args_file"
            echo "  ERROR: dashboard container recreate failed" >&2
            docker compose -f "$DASHBOARD_COMPOSE_FILE" ps 2>&1 || true
            docker compose -f "$DASHBOARD_COMPOSE_FILE" logs --tail 50 "$DASHBOARD_SERVICE" 2>&1 || true
            exit 1
        fi

        rm -f "$build_args_file"
        print_dashboard_deploy_state "After dashboard deploy" "${build_cmd[*]} && ${up_cmd[*]}"
    fi

    verify_dashboard_health
    echo ""
    echo "  Dashboard rebuilt and recreated successfully"
}

rebuild_generic_stack() {
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
        docker compose -f "$compose_file" up -d --force-recreate
        echo "  Restarted (no build): $(compose_basename "$compose_file")"
    else
        local build_cmd=(docker compose -f "$compose_file" build)
        if [[ $NO_CACHE -eq 1 ]]; then
            build_cmd+=(--no-cache)
        fi
        echo "  Build command: ${build_cmd[*]}"
        "${build_cmd[@]}"
        docker compose -f "$compose_file" up -d --build --force-recreate
        echo "  Rebuilt: $(compose_basename "$compose_file")"
    fi
}

rebuild_stack() {
    local compose_file="$1"

    if is_dashboard_compose_file "$compose_file"; then
        rebuild_dashboard_stack
        return 0
    fi

    rebuild_generic_stack "$compose_file"
}

verify_dashboard_health() {
    echo ""
    echo "  Dashboard verification:"
    docker compose -f "$DASHBOARD_COMPOSE_FILE" ps 2>&1 || echo "  (docker compose ps failed)"

    if ! command -v curl &>/dev/null; then
        echo "  Skipped HTTP health check (curl not available)"
        return 0
    fi

    echo ""
    echo "  HTTP check: ${DASHBOARD_HEALTH_URL}"
    if curl -fsS --max-time 10 "$DASHBOARD_HEALTH_URL"; then
        echo ""
    else
        echo "  ERROR: dashboard health check failed: ${DASHBOARD_HEALTH_URL}" >&2
        docker compose -f "$DASHBOARD_COMPOSE_FILE" logs --tail 50 "$DASHBOARD_SERVICE" 2>&1 || true
        exit 1
    fi
}

check_stack_health() {
    local compose_file="$1"
    local base_name
    local url

    base_name="$(compose_basename "$compose_file")"

    if is_dashboard_compose_file "$compose_file"; then
        return 0
    fi

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
            --dashboard)
                DASHBOARD_ONLY=1
                shift
                ;;
            --no-cache)
                NO_CACHE=1
                shift
                ;;
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

    if [[ $DASHBOARD_ONLY -eq 1 ]]; then
        section "Dashboard-only deploy"
        echo "  Canary and other stacks are not touched by --dashboard."
        echo "  Each compose file manages its own project; no shared down/up is required."
        rebuild_dashboard_stack

        echo ""
        echo "========================================"
        echo "  Dashboard rebuild complete"
        echo "========================================"
        return 0
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
