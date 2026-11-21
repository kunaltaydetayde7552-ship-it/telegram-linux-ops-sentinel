# Linux AI Ops Sentinel — Infrastructure & Production Runbook

> **Service:** Telegram & Linux AI Ops Sentinel  
> **Target Platform:** Cloud Linux VM (Ubuntu 22.04 / 24.04 LTS, Debian 12, Rocky/RHEL 9)  
> **Role:** Junior AI & Bot Developer / Linux DevOps Operations  
> **Architecture:** 24/7 Python Telegram Bot + Systemd Daemon + Auto-Recovery Watchdog + Cron Automations  

---

## 1. Architecture & Filesystem Blueprint

The **Telegram & Linux AI Ops Sentinel** is deployed as an isolated system daemon running continuously in the background. It monitors system health, listens to Telegram commands, inspects logs using AI, and self-heals immediately upon crash.

```
                    +------------------------------------------+
                    |          Telegram / Discord User         |
                    +--------------------+---------------------+
                                         |
                                         v  (HTTPS Webhook / Long Polling)
+---------------------------------------------------------------------------------------+
| Cloud Linux VM (Ubuntu / Debian)                                                      |
|                                                                                       |
|   +-------------------------------------------------------------------------------+   |
|   | systemd Init System (/etc/systemd/system/ops-sentinel.service)                |   |
|   |   - Restart=always | RestartSec=5 | StartLimitBurst=5                         |   |
|   |   - Sandboxed Execution (ProtectSystem=full, NoNewPrivileges=true)            |   |
|   +-----------------------------------+-------------------------------------------+   |
|                                       | manages                                       |
|                                       v                                               |
|   +-------------------------------------------------------------------------------+   |
|   | Main Process: ops_sentinel.py (Python Virtualenv: /opt/telegram-ops-sentinel)  |   |
|   |   - CPU / RAM / Disk Metrics Collector                                        |   |
|   |   - AI-Powered Journal Log Analyzer (Gemini / OpenAI API)                     |   |
|   |   - Systemctl Service Controller (/restart <service>)                         |   |
|   +-----------------------------------+-------------------------------------------+   |
|                                       | emits structured logs                         |
|                                       v                                               |
|   +-------------------------------------------------------------------------------+   |
|   | Linux Subsystems:                                                             |   |
|   |   - systemd-journald (/var/log/journal/ & journalctl)                         |   |
|   |   - Cron Watchdogs (/etc/cron.d/ or crontab: healthcheck & log cleanup)       |   |
|   |   - Chaos Simulator (simulate_crash.sh for self-healing verification)         |   |
|   +-------------------------------------------------------------------------------+   |
+---------------------------------------------------------------------------------------+
```

### Directory Structure on Target Linux Host
```
/opt/telegram-ops-sentinel/
├── .env                       # API tokens, Telegram Bot Token, Webhooks (chmod 600)
├── ops_sentinel.py            # Primary Bot & Sentinel application
├── simulate_crash.sh          # Chaos engineering & self-healing test script
├── crontab_jobs.sh            # Scheduled health pings & log cleanup manager
├── venv/                      # Isolated Python 3 virtual environment
│   └── bin/python
├── logs/                      # Application local log repository
│   ├── heartbeat.log          # Automated health check ping records
│   ├── cleanup.log            # Log rotation and purge audit trail
│   ├── metrics.csv            # Hourly resource utilization snapshot
│   └── archive/               # Gzipped archives (>7 days old)
└── data/                      # Persistent state, cache, and lock files
```

---

## 2. SSH Terminal Administration & Remote Server Access

Operating remote cloud machines requires secure, multiplexed SSH configurations to ensure uninterrupted access even during network drops.

### 2.1 Generating Modern SSH Keypairs
Do not use legacy RSA keys. Generate an **Ed25519** elliptic-curve keypair:
```bash
# Generate high-security key
ssh-keygen -t ed25519 -a 100 -C "admin@ops-sentinel-vm" -f ~/.ssh/id_sentinel_ed25519

# Secure local key permissions
chmod 700 ~/.ssh
chmod 600 ~/.ssh/id_sentinel_ed25519
chmod 644 ~/.ssh/id_sentinel_ed25519.pub
```

### 2.2 Installing Public Key to Remote Host
```bash
# Automated key transfer
ssh-copy-id -i ~/.ssh/id_sentinel_ed25519.pub ubuntu@<REMOTE_VM_IP>

# Alternatively, manual injection into authorized_keys:
cat ~/.ssh/id_sentinel_ed25519.pub | ssh ubuntu@<REMOTE_VM_IP> "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

### 2.3 SSH Client Profile (`~/.ssh/config`)
Add this configuration to your local workstation to enable one-word connection, connection multiplexing, and automatic keep-alives:
```ssh-config
Host ops-vm
    HostName <REMOTE_VM_IP>
    User ubuntu
    IdentityFile ~/.ssh/id_sentinel_ed25519
    Port 22
    ServerAliveInterval 30
    ServerAliveCountMax 5
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h:%p
    ControlPersist 4h
```
*Connect with a single command:* `ssh ops-vm`

### 2.4 Server-Side SSH Hardening (`/etc/ssh/sshd_config`)
On the remote VM, harden SSH against brute-force attacks:
```ini
# /etc/ssh/sshd_config.d/99-hardened.conf
Port 22
PermitRootLogin no
PasswordAuthentication no
ChallengeResponseAuthentication no
PubkeyAuthentication yes
MaxAuthTries 3
ClientAliveInterval 300
ClientAliveCountMax 2
X11Forwarding no
```
*Reload SSH daemon:* `sudo systemctl restart sshd`

### 2.5 Persistent Sessions (`tmux`)
Always run long-running tasks inside a `tmux` session so disconnections do not kill your terminal:
```bash
# Start new session named 'sentinel'
tmux new -s sentinel

# Detach from session: Press [Ctrl + B], then release and press [D]
# Re-attach to running session
tmux attach -t sentinel

# List active tmux sessions
tmux ls
```

---

## 3. Systemd Service Management (`systemctl`)

`systemd` is the standard Linux init system and service manager. It guarantees 24/7 uptime through aggressive self-healing policies.

### 3.1 The Production Unit File (`/etc/systemd/system/ops-sentinel.service`)
```ini
[Unit]
Description=Telegram & Linux AI Ops Sentinel - 24/7 Cloud Autonomous Monitor
Documentation=https://github.com/kunaltaydetayde7552-ship-it/telegram-linux-ops-sentinel
After=network.target network-online.target systemd-journald.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/telegram-ops-sentinel
Environment=PYTHONUNBUFFERED=1
Environment=ENVIRONMENT=production
EnvironmentFile=-/opt/telegram-ops-sentinel/.env
ExecStart=/opt/telegram-ops-sentinel/venv/bin/python /opt/telegram-ops-sentinel/ops_sentinel.py
ExecReload=/bin/kill -HUP $MAINPID

# 24/7 Self-Healing & Crash Recovery
Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5

# Logging via systemd journal
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ops-sentinel

# Security Sandboxing & Privileges
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=/opt/telegram-ops-sentinel/logs /opt/telegram-ops-sentinel/data /var/log/ops-sentinel

[Install]
WantedBy=multi-user.target
```

### 3.2 Key Systemd Directives Explained
* **`Restart=always`**: Guarantees that whether the bot exits with code 0 (clean exit), code 1 (Python exception), or is killed with `kill -9` (SIGKILL), systemd will immediately reschedule its launch.
* **`RestartSec=5`**: Enforces a 5-second backoff delay prior to restart, allowing transient network or socket states to clear.
* **`StartLimitIntervalSec=60` & `StartLimitBurst=5`**: Crash loop circuit breaker. If the application crashes more than 5 times within 60 seconds, systemd prevents endless CPU spin, enters a failed state, and notifies operators.
* **`NoNewPrivileges=true`**: Prevents the process and any child processes from gaining elevated root privileges via `setuid` binaries.
* **`ProtectSystem=full`**: Mounts `/usr`, `/boot`, and `/etc` as read-only for the bot process.

### 3.3 Essential `systemctl` Operations Cheat Sheet
| Task | Command |
|---|---|
| Register/Update Unit File | `sudo systemctl daemon-reload` |
| Enable Automatic Boot Start | `sudo systemctl enable ops-sentinel.service` |
| Start Service Immediately | `sudo systemctl start ops-sentinel.service` |
| Stop Service | `sudo systemctl stop ops-sentinel.service` |
| Restart Service | `sudo systemctl restart ops-sentinel.service` |
| Graceful Reload (Configuration) | `sudo systemctl reload ops-sentinel.service` |
| Detailed Status & PID Inspection | `systemctl status ops-sentinel.service` |
| Boolean Health Check (Script-friendly) | `systemctl is-active --quiet ops-sentinel && echo "ONLINE"` |
| Inspect Specific Properties | `systemctl show ops-sentinel -p MainPID,ActiveState,SubState` |
| View Systemd Failed Units | `systemctl --failed` |

---

## 4. Log Inspection & Real-Time Debugging

Modern Linux daemons output logs directly to `stdout`/`stderr`, which are ingested by `systemd-journald`.

### 4.1 `journalctl` Command Arsenal

#### Live Stream Logs (Equivalent to `tail -f`)
```bash
sudo journalctl -u ops-sentinel -f
```

#### Filter by Log Severity Level
Levels: `emerg` (0), `alert` (1), `crit` (2), `err` (3), `warning` (4), `notice` (5), `info` (6), `debug` (7).
```bash
# Show only errors, criticals, and panics
sudo journalctl -u ops-sentinel -p err..emerg

# Show warnings and above
sudo journalctl -u ops-sentinel -p warning..emerg -n 50 --no-pager
```

#### Time-Window Log Slicing
```bash
# Logs from the last 15 minutes
sudo journalctl -u ops-sentinel --since "15 min ago"

# Logs from a specific time window
sudo journalctl -u ops-sentinel --since "2026-11-21 00:00:00" --until "2026-11-21 02:00:00"

# Logs generated since the current system boot
sudo journalctl -u ops-sentinel -b
```

#### JSON Output for Automated AI Triage
```bash
sudo journalctl -u ops-sentinel -n 25 -o json-pretty
```

### 4.2 Linux Text Pipelines (`grep`, `awk`, `sed`)

#### 1. Extracting Python Tracebacks with Context
```bash
# Search for Traceback and capture 15 lines of context following it
sudo journalctl -u ops-sentinel --no-pager | grep -A 15 -E "Traceback \(most recent call last\):"
```

#### 2. Real-time Live Error Watcher
```bash
# Continuously scan for fatal exceptions and highlight in terminal
sudo journalctl -u ops-sentinel -f | grep --line-buffered -E "CRITICAL|ERROR|Exception|Failed"
```

#### 3. Analyzing Command Execution Frequency with `awk`
```bash
# Count how many times each Telegram command (/status, /logs, /restart) was triggered
sudo journalctl -u ops-sentinel --since "24 hours ago" \
  | grep "Command executed:" \
  | awk '{print $NF}' \
  | sort \
  | uniq -c \
  | sort -nr
```

#### 4. Disk Usage of Journal Logs & Vacuuming
```bash
# Check current disk space occupied by journald logs
journalctl --disk-usage

# Retain only the last 500MB of logs
sudo journalctl --vacuum-size=500M

# Retain only the last 7 days of logs
sudo journalctl --vacuum-time=7d
```

---

## 5. System Resource Monitoring (`htop`, `top`, `vmstat`, `free`, `df`)

Linux operations engineers must quickly identify CPU spikes, memory leaks, zombie processes, and disk exhaustion.

### 5.1 Interactive Process Auditing with `htop`
Launch `htop` in the terminal:
```bash
htop
```

#### Essential `htop` Navigation Shortcuts:
* **`F3` / `/`**: Search for process by name (e.g., type `python` or `ops-sentinel`).
* **`F4` / `\`**: Filter process list to show only matching processes.
* **`F5` / `t`**: Tree View — reveals parent-child hierarchy and thread spawning.
* **`F6` / `>`**: Sort by Metric (select `PERCENT_CPU`, `PERCENT_MEM`, or `TIME+`).
* **`F9` / `k`**: Send Signal to selected process:
  * Select `15 (SIGTERM)` for graceful exit.
  * Select `9 (SIGKILL)` for immediate hard termination.
* **`q`**: Quit `htop`.

### 5.2 Command-Line Diagnostic Tools

#### Memory Utilization (`free -h`)
```bash
free -h
# Look at "available" rather than "free" (Linux caches disk pages in RAM).
# If swap "used" is high and constantly increasing, investigate memory leaks.
```

#### Disk Space & Inodes (`df -h`, `df -i`)
```bash
# Check filesystem free space
df -h /

# Check inode consumption (frequent cause of "No space left on device" despite free GBs)
df -i /
```

#### CPU & I/O Wait Bottlenecks (`vmstat 1 5`)
```bash
vmstat 1 5
# Columns to monitor:
#   r: Running processes awaiting CPU
#   b: Blocked processes waiting on I/O (disk/network)
#   si / so: Memory swapped in/out per second (should be 0)
#   wa: CPU time spent waiting on I/O (high wa indicates slow disk)
```

#### Active Network Sockets (`ss -tulnp`)
```bash
# List all TCP listening ports and the processes bound to them
sudo ss -tulnp
```

---

## 6. Scheduled Automation (`crontab_jobs.sh`)

Recurring tasks are managed via `crontab_jobs.sh`.

### 6.1 Configured Schedule
| Schedule | Cadence | Script / Action | Purpose |
|---|---|---|---|
| `*/15 * * * *` | Every 15 min | `crontab_jobs.sh run-healthcheck` | Pings service health, captures PID/RAM/CPU, auto-recovers if dead |
| `0 2 * * *` | Daily at 02:00 AM | `crontab_jobs.sh run-cleanup` | Gzips logs >2d, archives >7d, purges >30d |
| `*/30 * * * *` | Every 30 min | `find ... -name "*.lock" -delete` | Purges abandoned lock files |
| `0 * * * *` | Hourly | `metrics.csv` append | Writes timestamp, CPU%, RAM(MB), Disk% to CSV |

### 6.2 Managing Cron via the Script
```bash
# Install scheduled jobs cleanly
./crontab_jobs.sh install

# View active crontab
./crontab_jobs.sh list

# Test healthcheck immediately
./crontab_jobs.sh run-healthcheck

# Test log cleanup routine immediately
./crontab_jobs.sh run-cleanup

# Remove sentinel jobs cleanly
./crontab_jobs.sh remove
```

---

## 7. Chaos Testing & Incident Simulation (`simulate_crash.sh`)

To prove enterprise reliability, the infrastructure must be tested against sudden ungraceful failure.

### 7.1 Executing Chaos Scenarios
```bash
# Make script executable
chmod +x simulate_crash.sh

# 1. Simulate ungraceful crash (SIGKILL / kill -9)
sudo ./simulate_crash.sh --sigkill

# 2. Simulate graceful termination (SIGTERM / kill -15)
sudo ./simulate_crash.sh --sigterm

# 3. Read-only health check & configuration validation
./simulate_crash.sh --verify
```

### 7.2 What Happens During the Chaos Run:
1. The script identifies the active PID of `ops-sentinel.service`.
2. It sends `SIGKILL` directly to the PID.
3. It starts a sub-second timer while the kernel terminates the process.
4. `systemd` detects the non-zero / uncatchable termination.
5. In accordance with `Restart=always` and `RestartSec=5`, `systemd` initiates a fresh process after 5 seconds.
6. `simulate_crash.sh` detects the new PID and validates system health.
7. Total downtime, old PID, new PID, and exit code are logged to `/var/log/ops-sentinel/crash_simulation.log`.

---

## 8. Incident Response & Troubleshooting Playbook

### Scenario A: Service enters `failed` state with Crash Loop
* **Symptom:** `systemctl status ops-sentinel` displays `Active: failed (Result: exit-code)`.
* **Root Cause:** Application failed more than 5 times within 60 seconds (rate limiting tripped).
* **Remediation:**
  1. Inspect the fatal crash reason:
     ```bash
     sudo journalctl -u ops-sentinel -e --no-pager -n 30
     ```
  2. If missing environment variable or bad token in `.env`, correct `/opt/telegram-ops-sentinel/.env`.
  3. Reset the systemd rate limit counter:
     ```bash
     sudo systemctl reset-failed ops-sentinel.service
     ```
  4. Restart the service:
     ```bash
     sudo systemctl restart ops-sentinel.service
     ```

### Scenario B: Stale Lock File or Zombie PID
* **Symptom:** Bot fails to start with `Address already in use` or `LockError: /tmp/sentinel.lock exists`.
* **Remediation:**
  1. Check if a rogue Python process is running outside systemd:
     ```bash
     ps aux | grep ops_sentinel.py
     ```
  2. Terminate the rogue PID:
     ```bash
     sudo kill -9 <ROGUE_PID>
     ```
  3. Remove stale lock files:
     ```bash
     sudo rm -f /tmp/sentinel.lock /opt/telegram-ops-sentinel/data/*.lock
     ```
  4. Start systemd unit:
     ```bash
     sudo systemctl start ops-sentinel.service
     ```

### Scenario C: Out of Disk Space (`ENOSPC`)
* **Symptom:** Service crashes silently; logs cannot be written.
* **Remediation:**
  1. Check disk utilization: `df -h /`
  2. Vacuum systemd journald logs: `sudo journalctl --vacuum-size=200M`
  3. Force-run log cleanup: `./crontab_jobs.sh run-cleanup`
  4. Clear apt cache: `sudo apt-get clean`

---

## 9. Zero-to-Production Deployment Checklist

- [ ] **1. VM Provisioning:** Launch Ubuntu 22.04 LTS instance with SSH key authentication.
- [ ] **2. System Updates:** `sudo apt update && sudo apt upgrade -y`
- [ ] **3. Tooling:** `sudo apt install -y python3-venv python3-pip git htop curl jq gzip`
- [ ] **4. Repository Setup:** Clone repository to `/opt/telegram-ops-sentinel` and set ownership: `sudo chown -R ubuntu:ubuntu /opt/telegram-ops-sentinel`
- [ ] **5. Virtual Environment:** `python3 -m venv /opt/telegram-ops-sentinel/venv && source /opt/telegram-ops-sentinel/venv/bin/activate && pip install -r requirements.txt`
- [ ] **6. Configuration:** Copy and populate `.env` with API credentials: `chmod 600 /opt/telegram-ops-sentinel/.env`
- [ ] **7. Systemd Installation:**
  ```bash
  sudo cp ops-sentinel.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable ops-sentinel.service
  sudo systemctl start ops-sentinel.service
  ```
- [ ] **8. Automated Cron Setup:** Run `./crontab_jobs.sh install`
- [ ] **9. Chaos Verification:** Execute `sudo ./simulate_crash.sh --sigkill` to confirm auto-healing in 5s.
- [ ] **10. Live Verification:** Issue `/status` command in Telegram to receive server health confirmation.
