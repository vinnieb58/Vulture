#!/usr/bin/env bash
# raven_finch_services.sh
#
# Shared Finch systemd restart helpers for Raven deploy scripts.
# Expects APP_DIR to be set by the caller.

FINCH_API_SERVICE="${FINCH_API_SERVICE:-finch-api.service}"
FINCH_TELEGRAM_SERVICE="${FINCH_TELEGRAM_SERVICE:-finch-telegram.service}"

finch_unit_source_exists() {
    local unit="$1"
    [[ -f "${APP_DIR}/deploy/systemd/${unit}" || -f "/etc/systemd/system/${unit}" ]]
}

finch_service_is_active() {
    local unit="$1"
    systemctl is-active "$unit" &>/dev/null && [[ "$(systemctl is-active "$unit" 2>/dev/null)" == "active" ]]
}

restart_finch_services() {
    if finch_unit_source_exists "$FINCH_API_SERVICE"; then
        echo "  Restarting Finch API…"
        if ! sudo systemctl restart "$FINCH_API_SERVICE"; then
            echo "  ERROR: failed to restart ${FINCH_API_SERVICE}"
            systemctl status "${FINCH_API_SERVICE%.service}" --no-pager -l 2>&1 || true
            return 1
        fi
        if finch_service_is_active "$FINCH_API_SERVICE"; then
            echo "  Finch API active"
        else
            echo "  WARNING: ${FINCH_API_SERVICE} is not active after restart"
            systemctl status "${FINCH_API_SERVICE%.service}" --no-pager -l 2>&1 || true
        fi
    else
        echo "  WARNING: ${FINCH_API_SERVICE} not installed; skipping Finch API restart"
    fi

    if finch_unit_source_exists "$FINCH_TELEGRAM_SERVICE"; then
        echo "  Restarting Finch Telegram…"
        if ! sudo systemctl restart "$FINCH_TELEGRAM_SERVICE"; then
            echo "  ERROR: failed to restart ${FINCH_TELEGRAM_SERVICE}"
            systemctl status "${FINCH_TELEGRAM_SERVICE%.service}" --no-pager -l 2>&1 || true
            return 1
        fi
        if finch_service_is_active "$FINCH_TELEGRAM_SERVICE"; then
            echo "  Finch Telegram active"
        else
            echo "  WARNING: ${FINCH_TELEGRAM_SERVICE} is not active after restart"
            systemctl status "${FINCH_TELEGRAM_SERVICE%.service}" --no-pager -l 2>&1 || true
        fi
    else
        echo "  WARNING: ${FINCH_TELEGRAM_SERVICE} not installed; skipping Finch Telegram restart"
    fi

    return 0
}
