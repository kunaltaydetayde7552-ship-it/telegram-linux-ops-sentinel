#!/usr/bin/env python3
"""
Telegram & Linux AI Ops Sentinel
================================
A production-grade Python operations bot that monitors Linux server health,
inspects and analyzes system/service logs using Gemini AI, executes safe
service restarts with stale PID lock/zombie process recovery, and runs
periodic background health checks.

Author: Kunal Tayde (@kunaltaydetayde7552-ship-it)
Email: kunaltaydetayde7552@gmail.com
Repository: https://github.com/kunaltaydetayde7552-ship-it/telegram-linux-ops-sentinel
License: MIT
"""

import os
import sys
import time
import logging
import platform
import subprocess
import argparse
import asyncio
from datetime import datetime, timezone
from collections import deque
from typing import Dict, List, Optional, Tuple, Any

# Optional dotenv loader
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Optional psutil loader
try:
    import psutil
except ImportError:
    psutil = None

# Optional requests loader
try:
    import requests
except ImportError:
    requests = None

# Telegram imports
try:
    from telegram import Update, constants
    from telegram.ext import (
        Application,
        ApplicationBuilder,
        CommandHandler,
        ContextTypes,
    )
except ImportError:
    Update = None
    constants = None
    Application = None
    ApplicationBuilder = None
    CommandHandler = None
    ContextTypes = None

# -----------------------------------------------------------------------------
# Configuration & Environment
# -----------------------------------------------------------------------------

BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()

# Authorization: Only these User IDs can execute operations commands
RAW_USER_IDS = os.getenv("AUTHORIZED_USER_IDS", "")
AUTHORIZED_USER_IDS: set = {
    int(uid.strip())
    for uid in RAW_USER_IDS.split(",")
    if uid.strip().isdigit()
}

# Monitored and Allowed Services
RAW_MONITORED = os.getenv(
    "MONITORED_SERVICES", "ops-sentinel,nginx,redis,postgresql"
)
MONITORED_SERVICES: List[str] = [
    s.strip() for s in RAW_MONITORED.split(",") if s.strip()
]

RAW_ALLOWED = os.getenv(
    "ALLOWED_SERVICES",
    "ops-sentinel,nginx,redis,postgresql,docker,apache2,mysql,mongodb,ssh",
)
ALLOWED_SERVICES: set = {
    s.strip().lower() for s in RAW_ALLOWED.split(",") if s.strip()
}

# Health Check Settings
HEALTH_CHECK_INTERVAL_SEC: int = int(
    os.getenv("HEALTH_CHECK_INTERVAL_SEC", "60")
)
CPU_ALERT_THRESHOLD: float = float(os.getenv("CPU_ALERT_THRESHOLD", "90.0"))
RAM_ALERT_THRESHOLD: float = float(os.getenv("RAM_ALERT_THRESHOLD", "88.0"))
DISK_ALERT_THRESHOLD: float = float(os.getenv("DISK_ALERT_THRESHOLD", "92.0"))
AUTO_RESTART_ON_FAILURE: bool = (
    os.getenv("AUTO_RESTART_ON_FAILURE", "false").lower() == "true"
)

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# Configure logging
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("ops-sentinel")

# -----------------------------------------------------------------------------
# Operational Changelog & Incident Store
# -----------------------------------------------------------------------------
class OpsAuditStore:
    """Stores in-memory records of all actions, crashes, and recoveries."""

    def __init__(self, max_records: int = 50):
        self._records: deque = deque(maxlen=max_records)

    def record(
        self,
        action: str,
        target: str,
        status: str,
        user: str = "SYSTEM",
        details: str = "",
    ):
        entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "action": action,
            "target": target,
            "status": status,
            "user": user,
            "details": details,
        }
        self._records.append(entry)
        logger.info(
            f"AUDIT LOG: [{entry['status']}] {entry['action']} on {entry['target']} by {entry['user']} - {entry['details']}"
        )

    def get_recent(self, limit: int = 15) -> List[Dict[str, str]]:
        return list(self._records)[-limit:]


audit_store = OpsAuditStore()

# -----------------------------------------------------------------------------
# OS & Linux Process Utilities
# -----------------------------------------------------------------------------
def is_linux() -> bool:
    """Check if host is Linux."""
    return platform.system().lower() == "linux"


def run_system_command(cmd_args: List[str], timeout: int = 15) -> Tuple[int, str, str]:
    """Execute a local system command safely with timeout.
    Returns (exit_code, stdout, stderr).
    """
    if not is_linux():
        # Non-Linux simulation for local development/testing on Windows/macOS
        cmd_str = " ".join(cmd_args)
        logger.warning(f"Simulating command on non-Linux platform: {cmd_str}")
        if "is-active" in cmd_str:
            return (0, "active\n", "")
        if "status" in cmd_str:
            return (0, "● simulated.service - Active (running)\n", "")
        if "restart" in cmd_str:
            return (0, "", "")
        if "journalctl" in cmd_str:
            return (
                0,
                "[INFO] 2026-11-21 06:00:00 Simulated log entry line 1\n"
                "[WARNING] 2026-11-21 06:01:00 Worker heartbeat delayed by 250ms\n"
                "[INFO] 2026-11-21 06:02:00 Routine health check completed successfully\n",
                "",
            )
        return (0, "Command simulated successfully\n", "")

    try:
        proc = subprocess.run(
            cmd_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return (proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return (-1, "", f"Command timed out after {timeout} seconds")
    except Exception as exc:
        return (-2, "", str(exc))


def get_service_status(service_name: str) -> str:
    """Return status of a systemd service: active, inactive, failed, or unknown."""
    if not is_linux():
        return "active (simulated)"

    code, out, _ = run_system_command(
        ["systemctl", "is-active", service_name], timeout=5
    )
    status = out.strip().lower()
    if status in ["active", "inactive", "failed"]:
        return status
    return "unknown"


def safe_restart_service(service_name: str, operator_name: str) -> Dict[str, Any]:
    """Safely restart a Linux service with zombie process / stale PID recovery.

    Steps:
    1. Whitelist validation.
    2. Check service status.
    3. Detect stale PID file or failed state.
    4. Reset systemd failed state if needed (systemctl reset-failed).
    5. Clean stale lock/PID if service is unresponsive.
    6. Execute systemctl restart.
    7. Verify service transitions to active.
    """
    clean_name = service_name.strip().lower()
    if clean_name not in ALLOWED_SERVICES:
        audit_store.record(
            action="RESTART_DENIED",
            target=clean_name,
            status="BLOCKED",
            user=operator_name,
            details="Service not in ALLOWED_SERVICES whitelist",
        )
        return {
            "success": False,
            "service": clean_name,
            "message": f"Service '{clean_name}' is not in the allowed services whitelist.",
            "actions": [],
        }

    actions: List[str] = []
    old_status = get_service_status(clean_name)
    actions.append(f"Initial status: {old_status}")

    # Check for stale PID or failed state
    stale_pid_paths = [
        f"/run/{clean_name}.pid",
        f"/var/run/{clean_name}.pid",
        f"/tmp/{clean_name}.pid",
    ]
    stale_detected = False

    if is_linux():
        for path in stale_pid_paths:
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        pid_str = f.read().strip()
                    if pid_str.isdigit():
                        pid = int(pid_str)
                        pid_dead = (not psutil.pid_exists(pid)) if psutil else (not os.path.exists(f"/proc/{pid}"))
                        if pid_dead:
                            actions.append(
                                f"Stale PID lock detected ({path} references dead PID {pid}). Removing."
                            )
                            os.remove(path)
                            stale_detected = True
                except Exception as e:
                    actions.append(f"Warning reading/removing PID file {path}: {e}")

        # If service was in failed state or stale PID was found, reset-failed first
        if old_status == "failed" or stale_detected:
            actions.append(f"Executing: systemctl reset-failed {clean_name}")
            run_system_command(["systemctl", "reset-failed", clean_name], timeout=5)

    # Execute restart command
    actions.append(f"Executing: systemctl restart {clean_name}")
    code, out, err = run_system_command(
        ["systemctl", "restart", clean_name], timeout=20
    )

    if code != 0:
        err_msg = err.strip() or out.strip() or f"Exit code {code}"
        actions.append(f"Restart failed: {err_msg}")
        audit_store.record(
            action="RESTART",
            target=clean_name,
            status="FAILED",
            user=operator_name,
            details=f"Old: {old_status}, Error: {err_msg}",
        )
        return {
            "success": False,
            "service": clean_name,
            "message": f"Failed to restart {clean_name}: {err_msg}",
            "actions": actions,
        }

    # Verify post-restart status
    time.sleep(1)
    new_status = get_service_status(clean_name)
    actions.append(f"Verification status: {new_status}")

    success = new_status in ["active", "active (simulated)"]
    audit_store.record(
        action="RESTART",
        target=clean_name,
        status="SUCCESS" if success else "WARN",
        user=operator_name,
        details=f"Old: {old_status} -> New: {new_status} (Stale PID: {stale_detected})",
    )

    return {
        "success": success,
        "service": clean_name,
        "old_status": old_status,
        "new_status": new_status,
        "stale_pid_recovered": stale_detected,
        "actions": actions,
    }


def fetch_service_logs(service_name: Optional[str] = None, lines: int = 50) -> str:
    """Fetch recent system or service logs using journalctl or fallback."""
    if not is_linux():
        return (
            f"[Simulated Non-Linux Log for {service_name or 'system'}]\n"
            f"2026-11-21 06:15:01 systemd[1]: Started {service_name or 'ops-sentinel.service'}.\n"
            f"2026-11-21 06:15:02 ops-sentinel[4102]: Sentinel daemon initialized with 4 workers.\n"
            f"2026-11-21 06:20:15 ops-sentinel[4102]: [WARN] Upstream API connection timeout after 5000ms.\n"
            f"2026-11-21 06:20:16 ops-sentinel[4102]: Retrying connection (attempt 2/3)...\n"
            f"2026-11-21 06:20:18 ops-sentinel[4102]: Connection restored successfully.\n"
        )

    if service_name:
        cmd = ["journalctl", "-u", service_name, "-n", str(lines), "--no-pager"]
    else:
        cmd = ["journalctl", "-n", str(lines), "--no-pager"]

    code, out, err = run_system_command(cmd, timeout=10)
    if code == 0 and out.strip():
        return out.strip()

    # Fallback to /var/log/syslog if journalctl yields nothing
    for log_path in ["/var/log/syslog", "/var/log/messages"]:
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    all_lines = f.readlines()
                return "".join(all_lines[-lines:])
            except Exception:
                pass

    return err.strip() or "No log entries found or permission denied to read logs."


# -----------------------------------------------------------------------------
# AI Log Analysis Engine (Gemini API with Intelligent Heuristic Fallback)
# -----------------------------------------------------------------------------
def analyze_logs_heuristic(log_text: str, service_name: str) -> str:
    """Rule-based intelligent log analyzer fallback."""
    patterns = {
        "Out of Memory (OOM)": [r"out of memory", r"killed process", r"oom-killer"],
        "Segmentation Fault": [r"segfault", r"sigsegv", r"core dumped"],
        "Network / Connection Refusal": [
            r"connection refused",
            r"connection timed out",
            r"failed to connect",
            r"broken pipe",
        ],
        "Port Conflict": [r"address already in use", r"bind: address already in use"],
        "Permission / Access Denied": [
            r"permission denied",
            r"eacces",
            r"operation not permitted",
        ],
        "Resource Exhaustion": [
            r"too many open files",
            r"no space left on device",
            r"disk full",
        ],
        "Stale PID / Lock Issue": [
            r"already running",
            r"pid file exists",
            r"locked by another process",
        ],
        "Python Traceback / Unhandled Exception": [
            r"traceback \(most recent call last\)",
            r"exception:",
            r"error:",
        ],
    }

    matched_issues = []
    lines = log_text.splitlines()
    error_lines = []

    for line in lines:
        lower_line = line.lower()
        if any(kw in lower_line for kw in ["err", "error", "fail", "fatal", "warn", "critical"]):
            if len(error_lines) < 4:
                error_lines.append(line.strip())

    import re
    for category, regex_list in patterns.items():
        for reg in regex_list:
            if re.search(reg, log_text, re.IGNORECASE):
                matched_issues.append(category)
                break

    analysis = [
        f"🤖 **Automated Log Analysis for `{service_name}`**",
        "",
        f"**Severity:** {'🔴 CRITICAL' if matched_issues else '🟡 WARNING / NORMAL'}",
    ]

    if matched_issues:
        analysis.append(f"**Detected Root Causes:** {', '.join(set(matched_issues))}")
        analysis.append("**Remediation Advice:**")
        if "Out of Memory (OOM)" in matched_issues:
            analysis.append("- Free RAM, check memory leaks, or increase swap space.")
        if "Port Conflict" in matched_issues:
            analysis.append("- Verify which process holds the port using `lsof -i` or `netstat -tlpn`.")
        if "Stale PID / Lock Issue" in matched_issues:
            analysis.append("- Use `/restart <service>` to trigger automatic stale PID lock cleanup.")
        if "Resource Exhaustion" in matched_issues:
            analysis.append("- Clean up old logs, cache, or increase file descriptor limits.")
    else:
        analysis.append("**Findings:** No critical fatal crashes detected in the last sample.")

    if error_lines:
        analysis.append("\n**Key Error Excerpt:**")
        sample = "\n".join(error_lines[:3])
        analysis.append(f"```text\n{sample}\n```")

    return "\n".join(analysis)


def analyze_logs_with_ai(log_text: str, service_name: str) -> str:
    """Analyze log entries using Google Gemini API with heuristic fallback."""
    if not GEMINI_API_KEY or requests is None:
        if not GEMINI_API_KEY:
            logger.info("GEMINI_API_KEY not configured. Using Heuristic Analyzer.")
        else:
            logger.info("requests library not available. Using Heuristic Analyzer.")
        return analyze_logs_heuristic(log_text, service_name)

    # Trim log excerpt to last 3500 chars to stay well within token limits
    log_sample = log_text[-3500:] if len(log_text) > 3500 else log_text

    prompt = f"""You are an expert Linux Site Reliability Engineer (SRE).
Analyze the following runtime logs for service: '{service_name}'.

LOG DATA:
\"\"\"
{log_sample}
\"\"\"

Provide a crisp, actionable operational summary with:
1. Severity (🟢 HEALTHY, 🟡 WARNING, 🔴 CRITICAL)
2. Root Cause Analysis (2-3 clear sentences on what broke or caused issues)
3. Immediate Remediation Action (1-2 bullet points of Linux commands to run)
Format your response cleanly in GitHub Markdown for Telegram display. Keep it under 200 words.
"""

    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [
            {
                "parts": [{"text": prompt}]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 450,
        },
    }

    try:
        response = requests.post(
            endpoint,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=12,
        )
        if response.status_code == 200:
            data = response.json()
            candidates = data.get("candidates", [])
            if candidates:
                text_out = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                if text_out:
                    return f"🤖 **Gemini AI Root-Cause Summary (`{service_name}`)**\n\n{text_out.strip()}"
        else:
            logger.warning(
                f"Gemini API returned status {response.status_code}: {response.text[:120]}"
            )
    except Exception as exc:
        logger.error(f"Error querying Gemini API: {exc}")

    # Fallback if Gemini fails or is unreachable
    return analyze_logs_heuristic(log_text, service_name)


# -----------------------------------------------------------------------------
# System Metrics Card Generator
# -----------------------------------------------------------------------------
def get_system_metrics() -> Dict[str, Any]:
    """Gather real-time CPU, RAM, Disk, Uptime, and Top Processes."""
    if psutil is None:
        return {
            "hostname": platform.node(),
            "os": f"{platform.system()} {platform.release()}",
            "uptime": "N/A (psutil not installed)",
            "cpu_percent": 0.0,
            "cpu_cores": os.cpu_count() or 1,
            "load_avg": (0.0, 0.0, 0.0),
            "ram_total_gb": 0.0,
            "ram_used_gb": 0.0,
            "ram_percent": 0.0,
            "swap_percent": 0.0,
            "disk_total_gb": 0.0,
            "disk_used_gb": 0.0,
            "disk_percent": 0.0,
            "net_sent_mb": 0.0,
            "net_recv_mb": 0.0,
            "top_processes": [],
        }

    # Uptime
    boot_time = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)
    now = datetime.now(timezone.utc)
    uptime_delta = now - boot_time
    days = uptime_delta.days
    hours, rem = divmod(uptime_delta.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    uptime_str = f"{days}d {hours}h {minutes}m {seconds}s"

    # CPU
    cpu_percent = psutil.cpu_percent(interval=0.5)
    cpu_cores = psutil.cpu_count(logical=True)
    load_avg = (
        os.getloadavg() if hasattr(os, "getloadavg") else (cpu_percent / 100, 0.0, 0.0)
    )

    # Memory
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    # Disk
    disk_path = "/" if is_linux() else "C:\\"
    disk = psutil.disk_usage(disk_path)

    # Network
    net_io = psutil.net_io_counters()

    # Top 3 processes by memory
    processes = []
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            info = proc.info
            processes.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    top_mem = sorted(
        processes, key=lambda p: (p.get("memory_percent") or 0.0), reverse=True
    )[:3]

    return {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "uptime": uptime_str,
        "cpu_percent": cpu_percent,
        "cpu_cores": cpu_cores,
        "load_avg": load_avg,
        "ram_total_gb": round(mem.total / (1024**3), 2),
        "ram_used_gb": round(mem.used / (1024**3), 2),
        "ram_percent": mem.percent,
        "swap_percent": swap.percent,
        "disk_total_gb": round(disk.total / (1024**3), 2),
        "disk_used_gb": round(disk.used / (1024**3), 2),
        "disk_percent": disk.percent,
        "net_sent_mb": round(net_io.bytes_sent / (1024**2), 1),
        "net_recv_mb": round(net_io.bytes_recv / (1024**2), 1),
        "top_processes": top_mem,
    }


def format_status_card(metrics: Dict[str, Any]) -> str:
    """Format metrics dictionary into a clean markdown card for Telegram."""
    def status_emoji(pct: float, warn: float = 85.0) -> str:
        if pct >= warn:
            return "🔴"
        if pct >= 70.0:
            return "🟡"
        return "🟢"

    cpu_emoji = status_emoji(metrics["cpu_percent"], CPU_ALERT_THRESHOLD)
    ram_emoji = status_emoji(metrics["ram_percent"], RAM_ALERT_THRESHOLD)
    disk_emoji = status_emoji(metrics["disk_percent"], DISK_ALERT_THRESHOLD)

    top_proc_str = ""
    for p in metrics["top_processes"]:
        pid = p.get("pid", 0)
        pname = p.get("name", "unknown")[:14]
        pmem = p.get("memory_percent") or 0.0
        top_proc_str += f"  • `[{pid}] {pname}`: {pmem:.1f}% RAM\n"

    # Services status
    service_lines = []
    for s in MONITORED_SERVICES:
        st = get_service_status(s)
        icon = "🟢" if "active" in st else ("🔴" if "failed" in st else "⚪")
        service_lines.append(f"{icon} `{s}`: {st}")
    services_summary = "\n".join(service_lines) if service_lines else "None configured"

    msg = f"""🛡️ **Telegram & Linux AI Ops Sentinel**
━━━━━━━━━━━━━━━━━━━━━
🖥️ **Host:** `{metrics['hostname']}` | `{metrics['os']}`
⏱️ **Uptime:** `{metrics['uptime']}`

⚙️ **CPU:** {cpu_emoji} `{metrics['cpu_percent']}%` ({metrics['cpu_cores']} Cores)
  • Load Avg: `{metrics['load_avg'][0]:.2f}, {metrics['load_avg'][1]:.2f}, {metrics['load_avg'][2]:.2f}`

🧠 **RAM:** {ram_emoji} `{metrics['ram_percent']}%` (`{metrics['ram_used_gb']}` / `{metrics['ram_total_gb']} GB`)
  • Swap: `{metrics['swap_percent']}%`

💾 **Disk:** {disk_emoji} `{metrics['disk_percent']}%` (`{metrics['disk_used_gb']}` / `{metrics['disk_total_gb']} GB`)

🌐 **Network I/O:** `TX: {metrics['net_sent_mb']} MB` | `RX: {metrics['net_recv_mb']} MB`

🔥 **Top Processes:**
{top_proc_str}
🛡️ **Monitored Services:**
{services_summary}
━━━━━━━━━━━━━━━━━━━━━
_Run /logs <service> for AI diagnostics | /restart <service> to recover._
"""
    return msg


# -----------------------------------------------------------------------------
# Telegram Command Authorization Filter
# -----------------------------------------------------------------------------
def is_authorized(user_id: int) -> bool:
    """Check if Telegram user is in the authorized whitelist."""
    if not AUTHORIZED_USER_IDS:
        # If no users configured, warn but allow for bootstrap testing
        return True
    return user_id in AUTHORIZED_USER_IDS


def check_auth(func):
    """Decorator to enforce Telegram user authorization."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        user_id = user.id if user else 0
        username = user.username or user.first_name if user else "Unknown"

        if not is_authorized(user_id):
            logger.warning(
                f"UNAUTHORIZED ACCESS ATTEMPT from user_id={user_id} (@{username})"
            )
            audit_store.record(
                action="UNAUTHORIZED_ACCESS",
                target=func.__name__,
                status="BLOCKED",
                user=f"{username} ({user_id})",
            )
            if update.message:
                await update.message.reply_text(
                    "⛔ **Access Denied.** Your Telegram account is not authorized to execute operational commands on this Sentinel.",
                    parse_mode=constants.ParseMode.MARKDOWN,
                )
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


# -----------------------------------------------------------------------------
# Telegram Command Handlers
# -----------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start."""
    user = update.effective_user
    user_id = user.id if user else 0
    username = user.first_name or "Operator"

    auth_badge = "✅ Authorized" if is_authorized(user_id) else "⚠️ Unauthorized"

    msg = f"""👋 **Hello, {username}!**

Welcome to **Telegram & Linux AI Ops Sentinel** — your 24/7 AI-powered remote server guardian.

🔑 **Your Status:** {auth_badge} (`ID: {user_id}`)
🖥️ **Host:** `{platform.node()}` ({platform.system()})

**Available Commands:**
• `/status` - Live CPU, RAM, Disk, Uptime & Service health
• `/logs [service]` - Last 50 lines + Gemini AI Root-Cause summary
• `/restart <service>` - Safe restart with zombie/stale PID recovery
• `/services` - Check status of all configured services
• `/changelog` - View recent ops actions, crashes, and recoveries
• `/help` - Command manual and documentation

Type `/status` to test system telemetry.
"""
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help."""
    msg = """📖 **Ops Sentinel Command Manual**
━━━━━━━━━━━━━━━━━━━━━
1️⃣ `/status`
Reports real-time system metrics (CPU load, RAM consumption, disk space, network traffic, top memory processes, and monitored services).

2️⃣ `/logs [service]`
Examples:
  • `/logs` (inspects overall system journal)
  • `/logs nginx`
  • `/logs redis`
Fetches the last 50 log lines and analyzes errors via Google Gemini AI or the heuristic rule engine.

3️⃣ `/restart <service>`
Examples:
  • `/restart ops-sentinel`
  • `/restart nginx`
Safely restarts the service. If the service is stuck in a failed state due to a stale PID lock or dead process, it clears the lock and resets the systemd failure flag first.

4️⃣ `/services`
Lists all monitored services and their current live states (`active`, `inactive`, `failed`).

5️⃣ `/changelog`
Shows recent incident responses, restarts, and health alerts.
━━━━━━━━━━━━━━━━━━━━━
"""
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.MARKDOWN)


@check_auth
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status."""
    metrics = get_system_metrics()
    card = format_status_card(metrics)
    await update.message.reply_text(card, parse_mode=constants.ParseMode.MARKDOWN)


@check_auth
async def cmd_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /services."""
    lines = [
        "🛡️ **Monitored Services Status:**",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    for s in MONITORED_SERVICES:
        st = get_service_status(s)
        icon = "🟢" if "active" in st else ("🔴" if "failed" in st else "⚪")
        lines.append(f"{icon} `{s}`: **{st.upper()}**")

    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"Allowed for restart: `{', '.join(sorted(ALLOWED_SERVICES))}`")
    await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.MARKDOWN)


@check_auth
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /logs <service>."""
    service_target = context.args[0].strip().lower() if context.args else None
    display_target = service_target or "system"

    status_msg = await update.message.reply_text(
        f"🔍 Fetching logs and generating AI summary for `{display_target}`...",
        parse_mode=constants.ParseMode.MARKDOWN,
    )

    log_content = fetch_service_logs(service_target, lines=50)
    ai_summary = analyze_logs_with_ai(log_content, display_target)

    # Prepare response: AI Summary + Tail 15 lines of raw log
    tail_lines = "\n".join(log_content.splitlines()[-15:])
    if len(tail_lines) > 1500:
        tail_lines = tail_lines[-1500:]

    reply = f"""{ai_summary}

📄 **Recent Raw Log Tail (last 15 lines):**
```text
{tail_lines}
```
"""
    await status_msg.edit_text(reply, parse_mode=constants.ParseMode.MARKDOWN)


@check_auth
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /restart <service>."""
    if not context.args:
        await update.message.reply_text(
            "⚠️ **Usage:** `/restart <service>`\nExample: `/restart nginx`\nRun `/services` to see allowed targets.",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    service_name = context.args[0].strip().lower()
    user = update.effective_user
    username = user.username or user.first_name if user else "TelegramUser"

    status_msg = await update.message.reply_text(
        f"⚙️ Initiating safe restart with zombie/PID check for `{service_name}`...",
        parse_mode=constants.ParseMode.MARKDOWN,
    )

    result = safe_restart_service(service_name, operator_name=f"@{username}")

    icon = "✅" if result["success"] else "❌"
    actions_formatted = "\n".join([f"  • {a}" for a in result.get("actions", [])])

    reply = f"""{icon} **Service Restart Result: `{service_name}`**
━━━━━━━━━━━━━━━━━━━━━
**Success:** `{result['success']}`
**Old State:** `{result.get('old_status', 'unknown')}`
**New State:** `{result.get('new_status', 'unknown')}`
**Stale PID Recovered:** `{result.get('stale_pid_recovered', False)}`

**Action Trace:**
{actions_formatted}
━━━━━━━━━━━━━━━━━━━━━
"""
    await status_msg.edit_text(reply, parse_mode=constants.ParseMode.MARKDOWN)


@check_auth
async def cmd_changelog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /changelog."""
    recent = audit_store.get_recent(limit=10)
    if not recent:
        await update.message.reply_text(
            "📝 **Audit Changelog:** No events recorded in this session yet.",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    lines = [
        "📝 **Recent Operations & Incident Changelog:**",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    for r in reversed(recent):
        lines.append(
            f"• `{r['timestamp']}` | **{r['action']}** on `{r['target']}` "
            f"[{r['status']}] by {r['user']}\n  _{r['details']}_"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━━")

    await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.MARKDOWN)


# -----------------------------------------------------------------------------
# Background Periodic Health Check (JobQueue)
# -----------------------------------------------------------------------------
# Cooldown state tracker to prevent repeated alert flooding
_alert_cooldowns: Dict[str, float] = {}
COOLDOWN_SECONDS = 300  # 5 minutes between duplicate alerts


def should_send_alert(key: str) -> bool:
    """Return True if cooldown expired for alert key."""
    now = time.time()
    last = _alert_cooldowns.get(key, 0.0)
    if now - last >= COOLDOWN_SECONDS:
        _alert_cooldowns[key] = now
        return True
    return False


async def scheduled_health_check(context: ContextTypes.DEFAULT_TYPE):
    """Runs on a timer to detect high load or crashed services."""
    if not AUTHORIZED_USER_IDS:
        return

    alerts = []

    # 1. CPU & Memory thresholds
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/" if is_linux() else "C:\\").percent

    if cpu >= CPU_ALERT_THRESHOLD and should_send_alert("alert_cpu"):
        alerts.append(f"⚠️ **High CPU Utilization:** `{cpu}%` (Threshold: {CPU_ALERT_THRESHOLD}%)")

    if mem >= RAM_ALERT_THRESHOLD and should_send_alert("alert_ram"):
        alerts.append(f"⚠️ **High Memory Consumption:** `{mem}%` (Threshold: {RAM_ALERT_THRESHOLD}%)")

    if disk >= DISK_ALERT_THRESHOLD and should_send_alert("alert_disk"):
        alerts.append(f"⚠️ **Critical Disk Usage:** `{disk}%` (Threshold: {DISK_ALERT_THRESHOLD}%)")

    # 2. Check Monitored Services
    for s in MONITORED_SERVICES:
        st = get_service_status(s)
        if st in ["failed", "inactive"]:
            key = f"service_{s}_{st}"
            if should_send_alert(key):
                if AUTO_RESTART_ON_FAILURE and s in ALLOWED_SERVICES:
                    logger.warning(f"AUTO-RECOVERY: Service {s} is {st}. Attempting restart.")
                    res = safe_restart_service(s, operator_name="AUTO_SENTINEL")
                    status_text = "Recovered ✅" if res["success"] else "Recovery Failed ❌"
                    alerts.append(
                        f"🚨 **Service Failure Detected:** `{s}` is `{st}`.\n"
                        f"⚡ **Auto-Restart Triggered:** {status_text} (New Status: `{res.get('new_status')}`)"
                    )
                else:
                    alerts.append(
                        f"🚨 **Service Alert:** Monitored service `{s}` is **{st.upper()}**.\n"
                        f"👉 Run `/restart {s}` to safely recover it."
                    )

    if alerts:
        msg = f"""🚨 **OPS SENTINEL HEALTH ALERT**
━━━━━━━━━━━━━━━━━━━━━
Host: `{platform.node()}`
Time: `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}`

{chr(10).join(alerts)}
━━━━━━━━━━━━━━━━━━━━━
"""
        for uid in AUTHORIZED_USER_IDS:
            try:
                await context.bot.send_message(
                    chat_id=uid, text=msg, parse_mode=constants.ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Failed to dispatch alert to chat_id={uid}: {e}")


# -----------------------------------------------------------------------------
# CLI & Standalone Diagnostics
# -----------------------------------------------------------------------------
def run_cli_diagnostics():
    """Print system status card to console for fast CLI testing."""
    print("=" * 60)
    print(" Telegram & Linux AI Ops Sentinel — CLI Diagnostic Run")
    print("=" * 60)
    metrics = get_system_metrics()
    print(format_status_card(metrics))
    print("\n[AI Log Diagnostic Check]")
    sample_log = fetch_service_logs("ops-sentinel", lines=15)
    print(analyze_logs_with_ai(sample_log, "ops-sentinel"))
    print("\nConfigured Monitored Services:", MONITORED_SERVICES)
    print("Allowed Restart Services:   ", sorted(list(ALLOWED_SERVICES)))
    print("Authorized Telegram Admins: ", AUTHORIZED_USER_IDS or "None (Open / Bootstrap)")
    print("=" * 60)


# -----------------------------------------------------------------------------
# Main Application Entry Point
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Telegram & Linux AI Ops Sentinel Daemon"
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Run offline system diagnostic check and print metrics to stdout",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate environment configuration and exit",
    )
    args = parser.parse_args()

    if args.cli:
        run_cli_diagnostics()
        sys.exit(0)

    if args.check_config:
        print("[*] Validating Ops Sentinel Configuration...")
        print(f"  • Host: {platform.node()} ({platform.system()})")
        print(f"  • Bot Token Present: {'Yes' if BOT_TOKEN else 'No (Missing!)'}")
        print(f"  • Gemini API Key:     {'Configured' if GEMINI_API_KEY else 'Not set (heuristic fallback active)'}")
        print(f"  • Authorized Admins:  {len(AUTHORIZED_USER_IDS)} user IDs configured")
        print(f"  • Monitored Services: {MONITORED_SERVICES}")
        print(f"  • Health Check:       Every {HEALTH_CHECK_INTERVAL_SEC}s")
        print(f"  • Libraries: python-telegram-bot={'OK' if ApplicationBuilder else 'Missing'}, psutil={'OK' if psutil else 'Missing'}, requests={'OK' if requests else 'Missing'}")
        print("[+] Configuration syntax verified.")
        sys.exit(0)

    if ApplicationBuilder is None:
        print("❌ ERROR: python-telegram-bot is not installed.")
        print("Please install requirements: pip install -r requirements.txt")
        sys.exit(1)

    if not BOT_TOKEN:
        print("❌ ERROR: TELEGRAM_BOT_TOKEN is not configured!")
        print("Please set TELEGRAM_BOT_TOKEN in your .env file or environment variables.")
        print("See .env.example for guidance.")
        sys.exit(1)

    logger.info("Initializing Telegram & Linux AI Ops Sentinel...")
    logger.info(f"Host: {platform.node()} | Platform: {platform.system()}")
    logger.info(f"Monitored services: {MONITORED_SERVICES}")
    logger.info(f"Authorized Admins: {AUTHORIZED_USER_IDS or 'All (No IDs specified)'}")

    # Build Application
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register Handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("services", cmd_services))
    app.add_handler(CommandHandler("changelog", cmd_changelog))

    # Register Background JobQueue health checker
    if app.job_queue:
        app.job_queue.run_repeating(
            scheduled_health_check,
            interval=HEALTH_CHECK_INTERVAL_SEC,
            first=10,
            name="ops_health_check",
        )
        logger.info(
            f"Scheduled periodic health check job registered (interval={HEALTH_CHECK_INTERVAL_SEC}s)."
        )

    # Start Bot Polling
    logger.info("Starting Telegram Bot long-polling daemon...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
