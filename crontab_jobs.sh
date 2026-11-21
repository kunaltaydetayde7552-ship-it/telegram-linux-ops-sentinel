#!/usr/bin/env bash
# ==============================================================================
# Telegram & Linux AI Ops Sentinel - Crontab Scheduled Jobs Manager
# File: crontab_jobs.sh
#
# Purpose:
#   Installs, manages, and executes automated cron jobs for:
#   1. Continuous Health Check Pings (every 15 minutes / 3 hours)
#   2. Daily Log Cleanup & Archival (runs daily at 02:00 AM)
#   3. Zombie & Stale Lock File Scanner (runs every 30 minutes)
#   4. Hourly System Resource Metric Snapshot
#
# Usage:
#   ./crontab_jobs.sh install          # Safely install jobs to current user's crontab
#   ./crontab_jobs.sh list             # Display active crontab entries
#   ./crontab_jobs.sh remove           # Remove Ops-Sentinel cron jobs cleanly
#   ./crontab_jobs.sh run-healthcheck  # Manually run the healthcheck ping routine
#   ./crontab_jobs.sh run-cleanup      # Manually run the log cleanup & rotation routine
#   ./crontab_jobs.sh --help           # Show help and raw crontab syntax
# ==============================================================================

set -uo pipefail

# -----------------------------
# Configuration Constants
# -----------------------------
CRON_TAG_BEGIN="# >>> BEGIN TELEGRAM OPS SENTINEL JOBS >>>"
CRON_TAG_END="# <<< END TELEGRAM OPS SENTINEL JOBS <<<"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
BASE_DIR="${OPS_BASE_DIR:-${SCRIPT_DIR:-/opt/telegram-linux-ops-sentinel}}"
LOG_DIR="${BASE_DIR}/logs"
ARCHIVE_DIR="${LOG_DIR}/archive"
VAR_LOG_DIR="/var/log/ops-sentinel"
HEARTBEAT_LOG="${LOG_DIR}/heartbeat.log"
CLEANUP_LOG="${LOG_DIR}/cleanup.log"
SYSTEM_METRICS_LOG="${LOG_DIR}/metrics.csv"
TELEGRAM_BOT_SCRIPT="${BASE_DIR}/ops_sentinel.py"
SYSTEMD_UNIT="ops-sentinel.service"

# Retention Policies
RETENTION_DAYS_LOGS=7
RETENTION_DAYS_ARCHIVE=30

# Colors for terminal output
C_RESET="\033[0m"
C_BOLD="\033[1m"
C_GREEN="\033[38;5;46m"
C_YELLOW="\033[38;5;220m"
C_RED="\033[38;5;196m"
C_BLUE="\033[38;5;39m"

# -----------------------------
# Utility Routines
# -----------------------------
timestamp() {
    date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log_info()    { echo -e "${C_BLUE}[INFO]${C_RESET} [$(timestamp)] $1"; }
log_success() { echo -e "${C_GREEN}[SUCCESS]${C_RESET} [$(timestamp)] $1"; }
log_warn()    { echo -e "${C_YELLOW}[WARN]${C_RESET} [$(timestamp)] $1"; }
log_danger()  { echo -e "${C_RED}[DANGER]${C_RESET} [$(timestamp)] $1"; }

ensure_directories() {
    mkdir -p "${LOG_DIR}" "${ARCHIVE_DIR}" 2>/dev/null || true
    if [[ -w "/var/log" ]]; then
        mkdir -p "${VAR_LOG_DIR}" 2>/dev/null || true
    fi
}

# -----------------------------
# Scheduled Routine 1: Healthcheck Ping
# -----------------------------
run_healthcheck() {
    ensure_directories
    local ts
    ts=$(timestamp)
    log_info "Initiating Healthcheck Ping for ${SYSTEMD_UNIT}..."

    local is_active="unknown"
    local pid="0"
    local cpu_usage="0.0%"
    local mem_usage="0.0MB"

    if command -v systemctl &>/dev/null; then
        if systemctl is-active --quiet "${SYSTEMD_UNIT}" 2>/dev/null; then
            is_active="ACTIVE"
            pid=$(systemctl show --property MainPID --value "${SYSTEMD_UNIT}" 2>/dev/null || echo "0")
            if [[ "${pid}" -ne 0 && -d "/proc/${pid}" ]]; then
                mem_usage=$(ps -p "${pid}" -o rss= 2>/dev/null | awk '{printf "%.2f MB", $1/1024}' || echo "N/A")
                cpu_usage=$(ps -p "${pid}" -o %cpu= 2>/dev/null | awk '{print $1 "%"}' || echo "N/A")
            fi
        else
            is_active="INACTIVE"
        fi
    else
        # Process check fallback
        if pgrep -f "ops_sentinel.py" &>/dev/null; then
            is_active="ACTIVE (Process Only)"
            pid=$(pgrep -f "ops_sentinel.py" | head -n1)
        else
            is_active="INACTIVE"
        fi
    fi

    local log_entry="[${ts}] STATUS=${is_active} | PID=${pid} | CPU=${cpu_usage} | MEM=${mem_usage}"
    echo "${log_entry}" >> "${HEARTBEAT_LOG}" 2>/dev/null || echo "${log_entry}"

    if [[ "${is_active}" == "ACTIVE"* ]]; then
        log_success "Healthcheck PASSED: ${log_entry}"
    else
        log_danger "Healthcheck ALERT: Service is DOWN! ${log_entry}"
        # Trigger systemctl restart attempt if running as privileged cron
        if command -v systemctl &>/dev/null; then
            log_warn "Dispatching auto-recovery command: systemctl restart ${SYSTEMD_UNIT}"
            systemctl restart "${SYSTEMD_UNIT}" 2>/dev/null || true
        fi
    fi
}

# -----------------------------
# Scheduled Routine 2: Log Cleanup & Archival
# -----------------------------
run_cleanup() {
    ensure_directories
    local ts
    ts=$(timestamp)
    log_info "Executing scheduled Log Cleanup & Rotation at ${ts}..."

    local target_dirs=("${LOG_DIR}" "${VAR_LOG_DIR}")
    local cleaned_count=0
    local archived_count=0

    for dir in "${target_dirs[@]}"; do
        if [[ -d "${dir}" ]]; then
            log_info "Scanning directory: ${dir}"

            # Step 1: Compress uncompressed log files older than 2 days
            while IFS= read -r -d '' logfile; do
                if [[ "${logfile}" != *.gz && "${logfile}" != *.csv ]]; then
                    gzip -f "${logfile}" 2>/dev/null || true
                    archived_count=$((archived_count + 1))
                fi
            done < <(find "${dir}" -maxdepth 1 -type f -name "*.log" -mtime +2 -print0 2>/dev/null)

            # Step 2: Move compressed logs older than RETENTION_DAYS_LOGS days to archive folder
            while IFS= read -r -d '' gzfile; do
                mv "${gzfile}" "${ARCHIVE_DIR}/" 2>/dev/null || true
            done < <(find "${dir}" -maxdepth 1 -type f -name "*.gz" -mtime +"${RETENTION_DAYS_LOGS}" -print0 2>/dev/null)

            # Step 3: Delete archived logs older than RETENTION_DAYS_ARCHIVE days
            while IFS= read -r -d '' oldfile; do
                rm -f "${oldfile}" 2>/dev/null || true
                cleaned_count=$((cleaned_count + 1))
            done < <(find "${ARCHIVE_DIR}" -type f -mtime +"${RETENTION_DAYS_ARCHIVE}" -print0 2>/dev/null)
        fi
    done

    # Step 4: Truncate current heartbeat if exceeding 10MB
    if [[ -f "${HEARTBEAT_LOG}" ]]; then
        local size_kb
        size_kb=$(du -k "${HEARTBEAT_LOG}" 2>/dev/null | cut -f1)
        if [[ ${size_kb:-0} -gt 10240 ]]; then
            tail -n 2000 "${HEARTBEAT_LOG}" > "${HEARTBEAT_LOG}.tmp" && mv "${HEARTBEAT_LOG}.tmp" "${HEARTBEAT_LOG}"
            log_info "Heartbeat log exceeded 10MB; truncated to last 2,000 entries."
        fi
    fi

    local summary="[${ts}] CLEANUP_COMPLETE: Compressed=${archived_count} files, Purged=${cleaned_count} expired archives."
    echo "${summary}" >> "${CLEANUP_LOG}" 2>/dev/null || echo "${summary}"
    log_success "${summary}"
}

# -----------------------------
# Crontab Text Block Definition
# -----------------------------
generate_cron_block() {
    cat <<EOF
${CRON_TAG_BEGIN}
# ------------------------------------------------------------------------------
# 1. Healthcheck Ping: Every 15 minutes, inspect service status & emit heartbeat
*/15 * * * * /bin/bash -c "${BASE_DIR}/crontab_jobs.sh run-healthcheck >> ${LOG_DIR}/heartbeat_cron.log 2>&1"

# 2. Daily Log Cleanup: Every morning at 02:00 AM, rotate & archive expired logs
0 2 * * * /bin/bash -c "${BASE_DIR}/crontab_jobs.sh run-cleanup >> ${LOG_DIR}/cleanup_cron.log 2>&1"

# 3. Zombie & Stale Lock Scanner: Every 30 minutes, clean abandoned lock files
*/30 * * * * find /tmp /opt/telegram-ops-sentinel/data -name "*.lock" -mmin +60 -delete 2>/dev/null

# 4. Hourly System Metric Snapshot: Log CPU, RAM & Disk usage to CSV
0 * * * * /bin/bash -c 'echo "\$(date -u +\\%Y-\\%m-\\%dT\\%H:\\%M:\\%SZ),\$(top -bn1 | grep "Cpu(s)" | awk "{print \\\$2 + \\\$4}"),\$(free -m | awk "/Mem:/ {print \\\$3}"),\$(df -h / | awk "NR==2 {print \\\$5}")" >> ${SYSTEM_METRICS_LOG}'
# ------------------------------------------------------------------------------
${CRON_TAG_END}
EOF
}

# -----------------------------
# Crontab Installation & Removal
# -----------------------------
install_crontab() {
    ensure_directories
    log_info "Configuring automated cron jobs in user crontab..."

    local existing_cron
    existing_cron=$(crontab -l 2>/dev/null || true)

    # Strip any previous Ops Sentinel entries cleanly
    local cleaned_cron
    cleaned_cron=$(echo "${existing_cron}" | sed "/${CRON_TAG_BEGIN}/,/${CRON_TAG_END}/d")

    # Combine existing crontab with new Ops Sentinel block
    local new_cron
    if [[ -z "${cleaned_cron// }" ]]; then
        new_cron="$(generate_cron_block)"
    else
        new_cron="${cleaned_cron}"$'\n'"$(generate_cron_block)"
    fi

    # Load into crontab
    echo "${new_cron}" | crontab -
    log_success "Crontab successfully updated with Ops Sentinel scheduled jobs!"
    echo ""
    list_crontab
}

remove_crontab() {
    log_info "Removing Ops Sentinel jobs from crontab..."
    local existing_cron
    existing_cron=$(crontab -l 2>/dev/null || true)

    if [[ -z "${existing_cron}" ]]; then
        log_warn "No crontab entries found."
        return 0
    fi

    local cleaned_cron
    cleaned_cron=$(echo "${existing_cron}" | sed "/${CRON_TAG_BEGIN}/,/${CRON_TAG_END}/d")

    if [[ -z "${cleaned_cron// }" ]]; then
        crontab -r 2>/dev/null || true
        log_success "Crontab was completely empty after removal and has been reset."
    else
        echo "${cleaned_cron}" | crontab -
        log_success "Ops Sentinel cron block successfully removed. Other user jobs preserved."
    fi
}

list_crontab() {
    echo -e "${C_BOLD}Active Crontab for user '$(whoami)':${C_RESET}"
    echo "------------------------------------------------------------"
    crontab -l 2>/dev/null || echo "(No crontab entries configured)"
    echo "------------------------------------------------------------"
}

show_help() {
    cat <<EOF
Usage: $0 [COMMAND]

Commands:
  install           Install the Ops Sentinel schedule into active crontab
  list              Display current user crontab entries
  remove            Remove only the Ops Sentinel block from crontab
  run-healthcheck   Manually execute the 15-minute healthcheck routine
  run-cleanup       Manually execute the daily log cleanup routine
  --help, -h        Display this help message and crontab reference

Raw Crontab Entries:
$(generate_cron_block)
EOF
}

# -----------------------------
# Main CLI Dispatcher
# -----------------------------
action="${1:-install}"

case "${action}" in
    install)
        install_crontab
        ;;
    remove|uninstall)
        remove_crontab
        ;;
    list|show)
        list_crontab
        ;;
    run-healthcheck|healthcheck)
        run_healthcheck
        ;;
    run-cleanup|cleanup)
        run_cleanup
        ;;
    -h|--help|help)
        show_help
        ;;
    *)
        echo -e "${C_RED}Error: Unknown command '${action}'${C_RESET}"
        show_help
        exit 1
        ;;
esac
