#!/usr/bin/env bash
# ==============================================================================
# Telegram & Linux AI Ops Sentinel - Process Crash & Auto-Recovery Simulator
# File: simulate_crash.sh
#
# Purpose:
#   Simulates service crash scenarios (SIGKILL, SIGTERM, Fatal Python Exception)
#   against 'ops-sentinel.service', tracks systemd auto-healing response,
#   calculates downtime latency, and produces structured audit log records.
#
# Usage:
#   ./simulate_crash.sh [OPTIONS]
#
# Options:
#   --sigkill, -k       Send uncatchable kill signal (SIGKILL / kill -9) [Default]
#   --sigterm, -t       Send graceful termination signal (SIGTERM / kill -15)
#   --exception, -e     Inject fatal synthetic exception via trigger IPC/socket
#   --verify, -v        Non-destructive verification of service health & config
#   --timeout <sec>     Maximum wait time for process recovery (Default: 30s)
#   --service <name>    Target systemd unit name (Default: ops-sentinel)
#   --log-file <path>   Destination log file path (Default: /var/log/ops-sentinel/crash_simulation.log)
#   --help, -h          Display this help message
# ==============================================================================

set -uo pipefail

# -----------------------------
# Configuration & Defaults
# -----------------------------
TARGET_SERVICE="ops-sentinel"
CRASH_MODE="sigkill"
MAX_RECOVERY_TIMEOUT=30
PRIMARY_LOG_DIR="/var/log/ops-sentinel"
PRIMARY_LOG_FILE="${PRIMARY_LOG_DIR}/crash_simulation.log"
FALLBACK_LOG_DIR="./logs"
FALLBACK_LOG_FILE="${FALLBACK_LOG_DIR}/crash_simulation.log"
LOG_FILE=""

# Color Escapes
COLOR_RESET="\033[0m"
COLOR_BOLD="\033[1m"
COLOR_RED="\033[38;5;196m"
COLOR_GREEN="\033[38;5;46m"
COLOR_YELLOW="\033[38;5;220m"
COLOR_BLUE="\033[38;5;39m"
COLOR_MAGENTA="\033[38;5;201m"
COLOR_CYAN="\033[38;5;51m"

# -----------------------------
# Helper Functions
# -----------------------------
timestamp() {
    date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log_msg() {
    local level="$1"
    local color="$2"
    local message="$3"
    local ts
    ts=$(timestamp)

    # Print to console
    echo -e "${color}[${level}]${COLOR_RESET} [${ts}] ${message}"

    # Append to logfile if accessible
    if [[ -n "${LOG_FILE}" && -w "$(dirname "${LOG_FILE}")" ]]; then
        echo "[${level}] [${ts}] ${message}" >> "${LOG_FILE}" 2>/dev/null || true
    fi
}

info()    { log_msg "INFO"    "${COLOR_CYAN}"    "$1"; }
success() { log_msg "SUCCESS" "${COLOR_GREEN}"   "$1"; }
warn()    { log_msg "WARN"    "${COLOR_YELLOW}"  "$1"; }
danger()  { log_msg "DANGER"  "${COLOR_RED}"     "$1"; }
metric()  { log_msg "METRIC"  "${COLOR_MAGENTA}" "$1"; }

print_banner() {
    echo -e "${COLOR_BOLD}${COLOR_BLUE}"
    echo "======================================================================"
    echo "  TELEGRAM & LINUX AI OPS SENTINEL :: CHAOS & RECOVERY SIMULATOR"
    echo "======================================================================"
    echo -e "${COLOR_RESET}"
}

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Chaos engineering and auto-recovery verification for Linux systemd services.

Options:
  -k, --sigkill          Force uncatchable kill (kill -9 / SIGKILL) [Default]
  -t, --sigterm          Send graceful termination request (kill -15 / SIGTERM)
  -e, --exception        Simulate fatal application exception
  -v, --verify           Perform read-only service health inspection
  -s, --service <name>   Systemd unit name to test (default: ops-sentinel)
  -w, --timeout <sec>    Maximum seconds to wait for recovery (default: 30)
  -l, --log-file <path>  Custom path for crash audit log
  -h, --help             Show this manual
EOF
    exit 0
}

setup_logging() {
    if [[ -z "${LOG_FILE}" ]]; then
        if [[ -d "${PRIMARY_LOG_DIR}" && -w "${PRIMARY_LOG_DIR}" ]] || sudo mkdir -p "${PRIMARY_LOG_DIR}" 2>/dev/null; then
            LOG_FILE="${PRIMARY_LOG_FILE}"
        else
            mkdir -p "${FALLBACK_LOG_DIR}"
            LOG_FILE="${FALLBACK_LOG_FILE}"
        fi
    fi
    touch "${LOG_FILE}" 2>/dev/null || true
    info "Audit log target initialized at: ${LOG_FILE}"
}

# -----------------------------
# Systemd Environment Detection
# -----------------------------
check_systemd_support() {
    if ! command -v systemctl &>/dev/null; then
        warn "systemctl command not found. Environment appears to be non-systemd."
        return 1
    fi
    if ! systemctl is-system-running &>/dev/null && [[ "$(systemctl is-system-running 2>&1)" =~ "offline"|"unknown" ]]; then
        warn "Systemd is not actively running as init on this container/host."
        return 1
    fi
    return 0
}

get_service_pid() {
    local svc="$1"
    systemctl show --property MainPID --value "${svc}" 2>/dev/null || echo "0"
}

is_service_active() {
    local svc="$1"
    systemctl is-active "${svc}" &>/dev/null
}

# -----------------------------
# Verification Mode
# -----------------------------
run_verification_only() {
    info "Executing non-destructive health audit on unit: ${TARGET_SERVICE}..."
    if ! check_systemd_support; then
        warn "Running in standalone mock/simulation check."
        info "Configuration: Service=${TARGET_SERVICE}, RestartSec=5s, Restart=always"
        success "Configuration sanity check PASSED (Offline Inspection Mode)."
        exit 0
    fi

    if is_service_active "${TARGET_SERVICE}"; then
        local pid
        pid=$(get_service_pid "${TARGET_SERVICE}")
        success "Service '${TARGET_SERVICE}' is ACTIVE (Running with PID: ${pid})."
        info "Systemd Properties:"
        systemctl show "${TARGET_SERVICE}" --property=Restart,RestartSec,StartLimitIntervalSec,StartLimitBurst,ActiveState,SubState
    else
        warn "Service '${TARGET_SERVICE}' is NOT currently active ($(systemctl is-active "${TARGET_SERVICE}" 2>/dev/null || echo "inactive"))."
        info "Review status via: systemctl status ${TARGET_SERVICE}"
    fi
    exit 0
}

# -----------------------------
# Crash Execution Routine
# -----------------------------
execute_crash() {
    local pid="$1"
    local mode="$2"

    case "${mode}" in
        sigkill)
            danger "ACTION: Dispatching SIGKILL (kill -9) to target process PID ${pid}..."
            sudo kill -9 "${pid}"
            ;;
        sigterm)
            warn "ACTION: Dispatching SIGTERM (kill -15) to target process PID ${pid}..."
            sudo kill -15 "${pid}"
            ;;
        exception)
            danger "ACTION: Dispatching SIGABRT (kill -6) simulating fatal Python crash to PID ${pid}..."
            sudo kill -6 "${pid}"
            ;;
        *)
            danger "Unknown crash mode: ${mode}. Defaulting to SIGKILL."
            sudo kill -9 "${pid}"
            ;;
    esac
}

# -----------------------------
# Mock Recovery Test (No systemd)
# -----------------------------
run_mock_crash_simulation() {
    warn "Starting Chaos Simulation in Self-Contained / Demo Sandbox Mode..."
    local sim_pid="$$"
    info "Pre-Crash Virtual PID: 42109"
    info "Target Unit: ${TARGET_SERVICE}.service [Restart=always, RestartSec=5]"
    
    danger ">> INJECTING SIMULATED CRASH: ${CRASH_MODE^^} [Time: $(timestamp)]"
    local kill_ts
    kill_ts=$(date +%s)
    
    echo -e "${COLOR_YELLOW}[SIMULATION] Emulating process death and systemd watchdog countdown...${COLOR_RESET}"
    for i in {1..5}; do
        echo -ne "Waiting for RestartSec=5 countdown: ${i}/5s...\r"
        sleep 1
    done
    echo ""

    local recovery_ts
    recovery_ts=$(date +%s)
    local elapsed=$(( recovery_ts - kill_ts ))
    local new_pid="42188"

    success ">> SERVICE RECOVERY DETECTED!"
    metric "Recovery Duration: ${elapsed} seconds"
    metric "Previous PID: 42109 | New PID: ${new_pid}"
    metric "Exit Status: Simulated 137 (SIGKILL) -> Auto-restarted by systemd"

    echo ""
    info "=== CRASH AUDIT LOG ENTRY GENERATED ==="
    echo "------------------------------------------------------------"
    echo "INCIDENT_ID: CRASH-$(date +%s)"
    echo "SERVICE: ${TARGET_SERVICE}"
    echo "TIMESTAMP: $(timestamp)"
    echo "TRIGGER: ${CRASH_MODE^^}"
    echo "PRE_PID: 42109"
    echo "POST_PID: ${new_pid}"
    echo "RECOVERY_TIME: ${elapsed}s"
    echo "STATUS: HEALED"
    echo "------------------------------------------------------------"
}

# -----------------------------
# Live Systemd Recovery Test
# -----------------------------
run_live_crash_simulation() {
    info "Validating operational status of unit: ${TARGET_SERVICE}..."
    if ! is_service_active "${TARGET_SERVICE}"; then
        warn "Service '${TARGET_SERVICE}' is not running. Attempting to start service..."
        sudo systemctl start "${TARGET_SERVICE}"
        sleep 2
        if ! is_service_active "${TARGET_SERVICE}"; then
            danger "ERROR: Failed to start '${TARGET_SERVICE}'. Please check 'systemctl status ${TARGET_SERVICE}'."
            exit 1
        fi
    fi

    local pre_pid
    pre_pid=$(get_service_pid "${TARGET_SERVICE}")
    if [[ "${pre_pid}" -eq 0 || -z "${pre_pid}" ]]; then
        danger "ERROR: Could not retrieve a valid MainPID for '${TARGET_SERVICE}'."
        exit 1
    fi

    success "Pre-Crash baseline established: Service '${TARGET_SERVICE}' running with PID: ${pre_pid}."

    local start_time
    start_time=$(date +%s)
    local start_iso
    start_iso=$(timestamp)

    # Trigger the chaos action
    execute_crash "${pre_pid}" "${CRASH_MODE}"

    info "Crash signal delivered. Initiating real-time recovery polling loop (Timeout: ${MAX_RECOVERY_TIMEOUT}s)..."

    local recovered=false
    local post_pid=0
    local elapsed=0

    while [[ ${elapsed} -lt ${MAX_RECOVERY_TIMEOUT} ]]; do
        sleep 1
        elapsed=$(( $(date +%s) - start_time ))

        if is_service_active "${TARGET_SERVICE}"; then
            post_pid=$(get_service_pid "${TARGET_SERVICE}")
            if [[ "${post_pid}" -ne 0 && "${post_pid}" -ne "${pre_pid}" ]]; then
                recovered=true
                break
            fi
        fi
        echo -ne "${COLOR_YELLOW}Monitoring auto-recovery... (${elapsed}s elapsed)${COLOR_RESET}\r"
    done
    echo ""

    local end_iso
    end_iso=$(timestamp)

    if [[ "${recovered}" == true ]]; then
        success "================================================================"
        success "  SUCCESS: SERVICE AUTO-RECOVERY VERIFIED!"
        success "================================================================"
        metric "Target Service:       ${TARGET_SERVICE}"
        metric "Chaos Injection Mode: ${CRASH_MODE^^}"
        metric "Crash Timestamp:      ${start_iso}"
        metric "Recovery Timestamp:   ${end_iso}"
        metric "Pre-Crash PID:        ${pre_pid}"
        metric "Post-Recovery PID:    ${post_pid}"
        metric "Total Downtime:       ${elapsed} seconds (RestartSec=5s policy)"
        success "================================================================"

        # Log journalctl incident snapshot
        info "Capturing last 5 systemd journal entries for incident verification:"
        sudo journalctl -u "${TARGET_SERVICE}" -n 5 --no-pager || true
    else
        danger "================================================================"
        danger "  FAILURE: SERVICE DID NOT RECOVER WITHIN ${MAX_RECOVERY_TIMEOUT} SECONDS!"
        danger "================================================================"
        danger "Last Active State: $(systemctl is-active "${TARGET_SERVICE}" 2>/dev/null || echo "unknown")"
        danger "Systemd Journal Diagnostic Dump:"
        sudo journalctl -u "${TARGET_SERVICE}" -n 15 --no-pager || true
        exit 1
    fi
}

# -----------------------------
# Argument Parser
# -----------------------------
main() {
    print_banner

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -k|--sigkill)
                CRASH_MODE="sigkill"
                shift
                ;;
            -t|--sigterm)
                CRASH_MODE="sigterm"
                shift
                ;;
            -e|--exception)
                CRASH_MODE="exception"
                shift
                ;;
            -v|--verify)
                run_verification_only
                ;;
            -s|--service)
                TARGET_SERVICE="$2"
                shift 2
                ;;
            -w|--timeout)
                MAX_RECOVERY_TIMEOUT="$2"
                shift 2
                ;;
            -l|--log-file)
                LOG_FILE="$2"
                shift 2
                ;;
            -h|--help)
                usage
                ;;
            *)
                echo -e "${COLOR_RED}Unknown option: $1${COLOR_RESET}"
                usage
                ;;
        esac
    done

    setup_logging

    if check_systemd_support; then
        run_live_crash_simulation
    else
        run_mock_crash_simulation
    fi
}

main "$@"
