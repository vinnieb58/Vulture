#!/usr/bin/env bash
# raven_healthcheck.sh — Raven host health report (read-only, no secrets).
#
# Production Vulture runtime is verified via systemd (vulture-bot, vulture-scheduler),
# not tmux. tmux output below is optional debug-only and is never a failure signal.
#
# Install on Raven (from repo root):
#   cp scripts/raven_healthcheck.sh ~/raven_healthcheck.sh
#   chmod +x ~/raven_healthcheck.sh
#
# Run:
#   ~/raven_healthcheck.sh              # full health report
#   ~/raven_healthcheck.sh --post-reboot   # focused post-reboot checklist
#
# Repo copy:
#   bash scripts/raven_healthcheck.sh
#   bash scripts/raven_post_reboot_check.sh

set -uo pipefail

MODE="full"
if [[ "${1:-}" == "--post-reboot" ]]; then
    MODE="post-reboot"
elif [[ -n "${1:-}" ]]; then
    echo "Usage: $0 [--post-reboot]" >&2
    exit 2
fi

OK_ITEMS=()
WARN_ITEMS=()
FAIL_ITEMS=()

section() {
    echo ""
    echo "========================================"
    echo "  $*"
    echo "========================================"
}

subsection() {
    echo ""
    echo "--- $* ---"
}

record_ok() {
    OK_ITEMS+=("$1")
}

record_warn() {
    WARN_ITEMS+=("$1")
}

record_fail() {
    FAIL_ITEMS+=("$1")
}

run_cmd() {
    local label="$1"
    shift
    subsection "$label"
    "$@" 2>&1 || echo "(command failed or unavailable: $label)"
}

# Return 0 when unit is active, 1 otherwise.
service_is_active() {
    local unit="$1"
    systemctl is-active --quiet "$unit" 2>/dev/null
}

# Print enabled + active lines; sets summary via record_* when check_label provided.
print_service_state() {
    local unit="$1"
    local check_label="${2:-}"

    local enabled="unknown"
    local active="unknown"

    if systemctl is-enabled "$unit" &>/dev/null; then
        enabled="$(systemctl is-enabled "$unit" 2>/dev/null || echo unknown)"
    else
        enabled="not-found"
    fi

    if systemctl is-active "$unit" &>/dev/null; then
        active="$(systemctl is-active "$unit" 2>/dev/null || echo unknown)"
    else
        active="not-found"
    fi

    echo "  $unit: enabled=$enabled active=$active"

    if [[ -n "$check_label" ]]; then
        if [[ "$active" == "active" ]]; then
            record_ok "$check_label active"
        elif [[ "$enabled" == "not-found" || "$active" == "not-found" ]]; then
            record_warn "$check_label not installed"
        elif [[ "$active" == "failed" || "$active" == "inactive" ]]; then
            if [[ "$unit" == vulture-bot || "$unit" == vulture-scheduler ]]; then
                record_fail "$check_label not active ($active)"
            else
                record_warn "$check_label not active ($active)"
            fi
        else
            record_warn "$check_label state=$active"
        fi
    fi
}

resolve_ssh_unit() {
    for candidate in ssh sshd; do
        if systemctl list-unit-files "${candidate}.service" &>/dev/null; then
            if systemctl is-enabled "${candidate}.service" &>/dev/null || \
               systemctl is-active "${candidate}.service" &>/dev/null; then
                echo "${candidate}.service"
                return 0
            fi
        fi
    done
    echo "ssh.service"
}

section_identity() {
    section "Identity / host / boot time"
    run_cmd "hostname" hostname
    run_cmd "date" date
    run_cmd "uptime" uptime
    run_cmd "last boot (who -b)" who -b
}

section_failed_units() {
    section "Failed systemd units"
    local failed_output
    failed_output="$(systemctl --failed --no-pager 2>/dev/null || true)"
    if [[ -z "$failed_output" ]]; then
        echo "  (systemctl --failed unavailable)"
        record_warn "failed unit scan unavailable"
        return
    fi
    echo "$failed_output"
    if echo "$failed_output" | grep -qE '^[[:space:]]*0 loaded units listed'; then
        record_ok "no failed systemd units"
    elif echo "$failed_output" | grep -qE 'loaded units listed'; then
        record_fail "failed systemd units present"
    else
        record_ok "no failed systemd units"
    fi
}

section_key_services() {
    section "Key services (enabled + active)"
    local ssh_unit
    ssh_unit="$(resolve_ssh_unit)"
    print_service_state "$ssh_unit" "ssh"
    print_service_state tailscaled "tailscale"
    print_service_state smbd "samba"
    print_service_state docker "docker"
    print_service_state vulture-bot "vulture-bot"
    print_service_state vulture-scheduler "vulture-scheduler"
}

section_network() {
    section "Network interfaces and routes"
    run_cmd "ip -br addr" ip -br addr
    run_cmd "ip route" ip route
}

section_internet_ping() {
    section "Internet connectivity (ping)"
    run_cmd "ping 1.1.1.1" ping -c 3 -W 3 1.1.1.1
    if ping -c 1 -W 3 1.1.1.1 &>/dev/null; then
        record_ok "ping 1.1.1.1"
    else
        record_warn "ping 1.1.1.1 failed"
    fi

    run_cmd "ping google.com" ping -c 3 -W 5 google.com
    if ping -c 1 -W 5 google.com &>/dev/null; then
        record_ok "ping google.com"
    else
        record_warn "ping google.com failed"
    fi
}

section_tailscale() {
    section "Tailscale"
    run_cmd "tailscale status" tailscale status
    run_cmd "tailscale ip -4" tailscale ip -4
    if command -v tailscale &>/dev/null && tailscale ip -4 &>/dev/null; then
        record_ok "tailscale ip -4"
    else
        record_warn "tailscale ip -4 unavailable"
    fi
}

section_disk_storage() {
    section "Disk usage and mounted storage"
    run_cmd "df -h" df -h
    if df -h / 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$5); if ($5+0 >= 90) exit 1; exit 0}'; then
        record_ok "root disk usage below 90%"
    else
        record_warn "root disk usage at or above 90%"
    fi
}

# Expected Roost / external storage paths. Optional absent drives are warnings only.
EXPECTED_STORAGE_PATHS=(
    "/mnt/storage/microsd"
    "/mnt/storage/toshiba_ext"
    "/mnt/storage/pelican_backup"
    "/mnt/storage/raven_nvme"
    "/mnt/storage/roost_spinning_0"
)

OPTIONAL_STORAGE_PATHS=(
    "/mnt/storage/raven_nvme"
    "/mnt/storage/roost_spinning_0"
)

section_expected_storage_mounts() {
    section "Expected storage mountpoints (optional drives may be unplugged)"
    local path mounted is_optional
    for path in "${EXPECTED_STORAGE_PATHS[@]}"; do
        is_optional=0
        for optional in "${OPTIONAL_STORAGE_PATHS[@]}"; do
            if [[ "$path" == "$optional" ]]; then
                is_optional=1
                break
            fi
        done
        if [[ ! -e "$path" ]]; then
            if [[ "$is_optional" -eq 1 ]]; then
                echo "  $path — OPTIONAL_MISSING (path does not exist)"
                record_warn "$path optional missing"
            else
                echo "  $path — MISSING (path does not exist)"
                record_warn "$path missing"
            fi
            continue
        fi
        # Trigger automount before checking mount state.
        ls -1 "$path" >/dev/null 2>&1 || true
        if mountpoint -q "$path" 2>/dev/null; then
            mounted="$(findmnt --mountpoint "$path" -n -o SOURCE,FSTYPE 2>/dev/null || echo mounted)"
            echo "  $path — OK ($mounted)"
            record_ok "$path mounted"
        else
            if [[ "$is_optional" -eq 1 ]]; then
                echo "  $path — OPTIONAL_MISSING (path exists, drive unplugged or automount idle)"
                record_warn "$path optional not mounted"
            else
                echo "  $path — NOT_MOUNTED (path exists, drive may be unplugged)"
                record_warn "$path not mounted"
            fi
        fi
    done
}

section_lsblk() {
    section "Block devices (lsblk -f)"
    run_cmd "lsblk -f" lsblk -f
}

section_fstab() {
    section "/etc/fstab"
    if [[ -r /etc/fstab ]]; then
        cat /etc/fstab
    else
        echo "  /etc/fstab not readable"
        record_warn "/etc/fstab not readable"
    fi
}

section_usb() {
    section "USB devices (lsusb)"
    run_cmd "lsusb" lsusb
}

section_samba() {
    section "Samba"
    run_cmd "systemctl status smbd --no-pager -l" systemctl status smbd --no-pager -l
    run_cmd "testparm -s" testparm -s
}

section_docker() {
    section "Docker"
    run_cmd "systemctl status docker --no-pager -l" systemctl status docker --no-pager -l
    run_cmd "docker ps" docker ps
    run_cmd "docker ps -a" docker ps -a
    run_cmd "docker system df" docker system df
}

section_vulture_systemd() {
    section "Vulture systemd services (production runtime)"
    echo "  Production checks use systemd, not tmux."
    run_cmd "systemctl status vulture-bot --no-pager -l" \
        systemctl status vulture-bot --no-pager -l
    run_cmd "systemctl status vulture-scheduler --no-pager -l" \
        systemctl status vulture-scheduler --no-pager -l

    # Summary for vulture units is recorded in section_key_services to avoid duplicates.
}

section_vulture_journal() {
    section "Vulture journal logs (recent)"
    run_cmd "journalctl -u vulture-bot -n 100 --no-pager" \
        journalctl -u vulture-bot -n 100 --no-pager
    run_cmd "journalctl -u vulture-scheduler -n 100 --no-pager" \
        journalctl -u vulture-scheduler -n 100 --no-pager
}

section_process_fallback() {
    section "Process fallback checks (pgrep)"
    local patterns=("discord_bot.py" "main.py" "scheduler" "hunt" "vulture")
    local pattern
    for pattern in "${patterns[@]}"; do
        subsection "pgrep -af $pattern"
        if pgrep -af "$pattern" 2>/dev/null; then
            if [[ "$pattern" == "discord_bot.py" ]]; then
                record_ok "discord_bot.py process detected"
            elif [[ "$pattern" == "main.py" ]]; then
                record_ok "main.py process detected"
            fi
        else
            echo "  (no matches)"
            if [[ "$pattern" == "discord_bot.py" || "$pattern" == "main.py" ]]; then
                if ! service_is_active vulture-bot && [[ "$pattern" == "discord_bot.py" ]]; then
                    record_warn "discord_bot.py process not detected"
                elif ! service_is_active vulture-scheduler && [[ "$pattern" == "main.py" ]]; then
                    record_warn "main.py process not detected"
                fi
            fi
        fi
    done
}

section_tmux_debug() {
    section "tmux sessions (DEBUG ONLY — not production runtime)"
    echo "  tmux is optional manual debugging only. Missing sessions are NOT failures."
    if command -v tmux &>/dev/null; then
        run_cmd "tmux list-sessions" tmux list-sessions
    else
        echo "  tmux not installed (OK for production)"
    fi
}

section_listening_ports() {
    section "Listening ports (ss -tulpn)"
    if ss -tulpn &>/dev/null; then
        run_cmd "ss -tulpn" ss -tulpn
    elif sudo -n ss -tulpn &>/dev/null; then
        run_cmd "sudo ss -tulpn" sudo -n ss -tulpn
    elif sudo ss -tulpn &>/dev/null; then
        run_cmd "sudo ss -tulpn" sudo ss -tulpn
    else
        echo "  ss -tulpn unavailable (sudo may be required)"
        record_warn "listening port scan unavailable"
    fi
}

section_boot_warnings() {
    section "Recent boot warnings (journalctl -b -p warning..alert)"
    run_cmd "journalctl -b -p warning..alert --no-pager" \
        journalctl -b -p warning..alert --no-pager
}

section_summary() {
    section "Summary"
    echo "  Mode: $MODE"
    echo ""
    echo "  OK (${#OK_ITEMS[@]}):"
    if [[ ${#OK_ITEMS[@]} -eq 0 ]]; then
        echo "    (none recorded)"
    else
        local item
        for item in "${OK_ITEMS[@]}"; do
            echo "    [OK]   $item"
        done
    fi
    echo ""
    echo "  WARN (${#WARN_ITEMS[@]}):"
    if [[ ${#WARN_ITEMS[@]} -eq 0 ]]; then
        echo "    (none)"
    else
        for item in "${WARN_ITEMS[@]}"; do
            echo "    [WARN] $item"
        done
    fi
    echo ""
    echo "  FAIL (${#FAIL_ITEMS[@]}):"
    if [[ ${#FAIL_ITEMS[@]} -eq 0 ]]; then
        echo "    (none)"
    else
        for item in "${FAIL_ITEMS[@]}"; do
            echo "    [FAIL] $item"
        done
    fi
    echo ""
    if [[ ${#FAIL_ITEMS[@]} -gt 0 ]]; then
        echo "  Overall: FAIL — review failed items above."
    elif [[ ${#WARN_ITEMS[@]} -gt 0 ]]; then
        echo "  Overall: WARN — production may be OK; review warnings."
    else
        echo "  Overall: OK"
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo "Raven health check"
echo "Started: $(date -Is 2>/dev/null || date)"
echo "Mode: $MODE"

if [[ "$MODE" == "post-reboot" ]]; then
    section_identity
    section_failed_units
    section_key_services
    section_network
    section_internet_ping
    section_tailscale
    section_disk_storage
    section_expected_storage_mounts
    section_lsblk
    section_fstab
    section_usb
    section_docker
    section_vulture_systemd
    section_vulture_journal
    section_process_fallback
    section_tmux_debug
    section_listening_ports
    section_boot_warnings
    run_cmd "testparm -s (Samba config syntax)" testparm -s
else
    section_identity
    section_failed_units
    section_key_services
    section_network
    section_internet_ping
    section_tailscale
    section_disk_storage
    section_expected_storage_mounts
    section_lsblk
    section_fstab
    section_usb
    section_samba
    section_docker
    section_vulture_systemd
    section_vulture_journal
    section_process_fallback
    section_tmux_debug
    section_listening_ports
    section_boot_warnings
fi

section_summary

# Do not exit non-zero: report is informational; caller inspects summary.
exit 0
