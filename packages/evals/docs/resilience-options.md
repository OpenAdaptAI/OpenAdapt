# VM/Tunnel Resilience and Recording Session Recovery

## Problem Statement

Recording demos on the Azure VM (waa-pool-00) requires:
- SSH tunnels forwarding local 5001->VM 5000 (WAA/QEMU), local 5050->VM 5051 (evaluate via socat), local 8006->VM 8006 (VNC)
- The user interacting with the VM desktop via VNC in a browser
- The `record_waa_demos.py` script capturing screenshots at each step

When the VM becomes unreachable (SSH timeout, Azure platform hiccup, network flap), or the
SSH tunnel drops silently:
1. The VNC session freezes -- the user cannot continue performing actions
2. The recording script's HTTP calls to `/screenshot` and `/execute` fail
3. All in-memory state (completed steps, step metadata, plan revisions) is lost
4. The user must restart the entire task recording from scratch after recovery

The VM recently became unreachable despite showing "Running" in Azure, requiring a full
restart. This cost an entire recording session.

---

## Option 1: Replace SSH tunnels with `autossh`

### Description

`autossh` wraps SSH and monitors the connection, automatically restarting it when it detects
the tunnel has died. It uses a monitoring port to send periodic test data through the
connection, detecting dead tunnels faster than SSH's built-in `ServerAliveInterval`.

### Current State

`SSHTunnelManager` (in `infrastructure/ssh_tunnel.py`) already sets `ServerAliveInterval=60`
and `ServerAliveCountMax=10`, giving a 10-minute tolerance before SSH declares the connection
dead. `run_dc_eval.py` has a separate tunnel setup with `ServerAliveInterval=15` and
`ServerAliveCountMax=3` (45-second detection). Neither auto-reconnects when the tunnel dies
mid-session.

### Implementation

```bash
# Install: brew install autossh (macOS) or apt-get install autossh (Linux)

# Replace the manual ssh tunnel command with:
autossh -M 20000 -f -N \
  -o "ServerAliveInterval=15" \
  -o "ServerAliveCountMax=3" \
  -o "ExitOnForwardFailure=yes" \
  -o "StrictHostKeyChecking=no" \
  -L 5001:localhost:5000 \
  -L 5050:localhost:5051 \
  -L 8006:localhost:8006 \
  azureuser@172.173.66.131
```

In Python (`SSHTunnelManager._start_tunnel`), replace the `ssh` subprocess with `autossh`,
adding the `-M <port>` flag for the monitoring channel.

### Effort: **Low** (1-2 hours)

### Impact: **Medium-High**

Automatically recovers from tunnel drops without any manual intervention, as long as the VM
is still reachable via SSH. Detects dead tunnels in seconds rather than minutes.

### Tradeoffs

- Does NOT help when the VM itself is unreachable (the recent incident -- SSH timeout was
  because the VM stopped responding, not because the tunnel dropped)
- Requires `autossh` to be installed on the local machine
- The monitoring port (-M) must not conflict with other services
- VNC session in the browser will still briefly freeze during reconnection (~5-10 seconds)
- Does not preserve the state of in-flight HTTP requests that were interrupted

### Verdict: **Implement first** -- low effort, immediate value for the common case

---

## Option 2: Recording session state persistence (checkpoint/resume)

### Description

Save recording session state to disk after each step, so that if the connection drops, the
script can resume from where it left off rather than starting over.

### Current State

The recording script (`scripts/record_waa_demos.py`, `cmd_record_waa`) saves screenshots
to `{task_dir}/step_XX_before.png` and `step_XX_after.png` as it goes, but the critical
in-memory state is lost on crash:

- `completed_steps: list[str]` -- steps the user has already performed
- `remaining_steps: list[str]` -- steps still to do
- `steps_meta: list[dict]` -- metadata about each completed step
- `step_plans: list[dict]` -- history of plan revisions
- `refined_indices: set[int]` -- which steps were refined
- `step_idx: int` -- current step counter
- `before_png: bytes` -- last screenshot

The final `meta.json` is only written AFTER all steps are complete.

### Implementation

Add incremental checkpointing to the recording loop:

```python
CHECKPOINT_FILE = "checkpoint.json"

def _save_checkpoint(task_dir, completed_steps, remaining_steps,
                     steps_meta, step_plans, refined_indices, step_idx):
    checkpoint = {
        "completed_steps": completed_steps,
        "remaining_steps": remaining_steps,
        "steps_meta": steps_meta,
        "step_plans": step_plans,
        "refined_indices": list(refined_indices),
        "step_idx": step_idx,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    (task_dir / CHECKPOINT_FILE).write_text(
        json.dumps(checkpoint, indent=2), encoding="utf-8"
    )

def _load_checkpoint(task_dir):
    cp_file = task_dir / CHECKPOINT_FILE
    if cp_file.exists():
        return json.loads(cp_file.read_text(encoding="utf-8"))
    return None
```

In the recording loop, call `_save_checkpoint()` after every step completion (when the user
presses Enter or types "d"). On startup, check for a checkpoint file and offer to resume:

```python
checkpoint = _load_checkpoint(task_dir)
if checkpoint:
    answer = input(f"  Found checkpoint at step {checkpoint['step_idx']}. Resume? [Y/n] ")
    if answer.lower() in ("", "y", "yes"):
        completed_steps = checkpoint["completed_steps"]
        remaining_steps = checkpoint["remaining_steps"]
        # ... restore all state ...
```

The before/after screenshots are already saved to disk per-step, so only the metadata
needs checkpointing.

### Effort: **Low** (2-3 hours)

### Impact: **High**

This is the highest-impact change because it directly addresses the "lost work" problem.
Even if autossh cannot reconnect (VM unreachable), the completed steps are preserved.
After the VM is restarted and tunnels re-established, the user can resume from the exact
step where they left off.

### Tradeoffs

- The VM desktop state may have changed after restart (QEMU reset clears the Windows
  session), so the user may need to re-run task setup. The checkpoint would need to
  detect this and offer to re-run `_soft_reset_task_env()` or `_hard_reset_task_env()`
  before resuming.
- Screenshots from before the restart may not match the current screen, but this is
  acceptable since the recording captures before/after pairs per step.
- Steps performed by the user but not yet confirmed (Enter not pressed) are still lost.
  This is a small window -- typically a few seconds.

### Verdict: **Implement second** -- directly solves the data loss problem

---

## Option 3: Run the recording script inside `tmux` on the VM

### Description

Instead of running `record_waa_demos.py` locally (where it depends on SSH tunnels for
every HTTP call), run it inside a `tmux` session on the Azure VM itself. The script
communicates with WAA over `localhost:5000` directly, eliminating the tunnel dependency.
The user connects via SSH to interact with the tmux session, and uses VNC for the desktop.

### Implementation

```bash
# On the VM:
ssh azureuser@172.173.66.131

# Start tmux session
tmux new-session -s recording

# Run the recording script (pointing at localhost inside the VM)
python record_waa_demos.py record-waa \
  --server http://localhost:5000 \
  --evaluate-url http://localhost:5051 \
  --tasks 04d9aeaf,0a0faba3
```

If SSH drops, the tmux session continues running on the VM. Reconnect with:

```bash
ssh azureuser@172.173.66.131
tmux attach -t recording
```

### Effort: **Low-Medium** (2-4 hours)

Requires:
- Installing Python + dependencies on the VM (or running inside the Docker container)
- The script needs access to the `.env` file with API keys (for VLM calls during annotation)
- Syncing the script and its dependencies to the VM

### Impact: **High** for script survival, **None** for VNC

The recording script itself becomes tunnel-independent. However, the user still needs
VNC (via SSH tunnel) to perform the actual desktop actions. If the tunnel drops, the
user cannot interact with the desktop, and the script will be waiting at an `input()`
prompt.

### Tradeoffs

- The script survives tunnel drops, but the user interaction (VNC) still requires
  a tunnel, so this only helps with the script state, not the user experience.
- Adds operational complexity (syncing code to VM, managing dependencies).
- VLM API calls from the VM go through Azure's egress, not the local network --
  this is fine but may have different latency characteristics.
- The script's interactive prompts (step confirmation) are rendered in the SSH
  terminal, not locally, so the UX may feel slightly different.

### Verdict: **Consider after Options 1+2** -- significant benefit only when combined
with checkpoint/resume

---

## Option 4: VM health monitoring with auto-recovery

### Description

Deploy a lightweight monitoring agent (cron job or systemd timer) on the VM that:
1. Periodically verifies the Docker container and QEMU/Windows are healthy
2. Writes health status to a file or cloud endpoint
3. Optionally auto-restarts the container if it becomes unresponsive

On the local side, add a background health check thread to the recording script that:
1. Pings the VM every 15 seconds
2. Warns the user immediately when connectivity is lost
3. Attempts to reconnect tunnels automatically

### Current State

`VMMonitor` (in `infrastructure/vm_monitor.py`) already has `check_status()` that probes
SSH, VNC, WAA, container status, and disk usage. `run_dc_eval.py` has `ensure_waa_ready()`
with a 3-step recovery sequence (probe -> reconnect tunnel -> restart container).

### Implementation

**VM-side watchdog** (`/etc/systemd/system/waa-watchdog.service`):
```ini
[Unit]
Description=WAA Container Health Watchdog
After=docker.service

[Service]
Type=simple
ExecStart=/usr/local/bin/waa-watchdog.sh
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
#!/bin/bash
# /usr/local/bin/waa-watchdog.sh
while true; do
    # Check if WAA Flask server responds
    if ! curl -sf --connect-timeout 5 http://localhost:5000/probe > /dev/null 2>&1; then
        echo "$(date): WAA probe failed, checking container..."
        if docker ps -q -f name=winarena | grep -q .; then
            echo "$(date): Container running but WAA unresponsive"
            # Log to persistent file for post-mortem
            echo "$(date) WAA_UNRESPONSIVE" >> /home/azureuser/waa-health.log
        else
            echo "$(date): Container not running, restarting..."
            docker start winarena
            echo "$(date) CONTAINER_RESTARTED" >> /home/azureuser/waa-health.log
        fi
    fi
    sleep 30
done
```

**Client-side health thread** (added to recording script):
```python
import threading

def _health_monitor(server, interval=15):
    """Background thread that monitors connection health."""
    while True:
        try:
            resp = requests.get(f"{server}/probe", timeout=5)
            if not resp.ok:
                print("\n  WARNING: WAA server returned non-OK status!")
        except Exception:
            print("\n  WARNING: Lost connection to WAA server!")
            print("  Recording paused. Waiting for reconnection...")
        time.sleep(interval)

# Start before recording loop:
monitor = threading.Thread(target=_health_monitor, args=(server,), daemon=True)
monitor.start()
```

### Effort: **Medium** (4-6 hours)

### Impact: **Medium**

The VM-side watchdog prevents the container from silently dying and provides a health
log for post-mortem analysis. The client-side monitor gives early warning so the user
knows immediately when something is wrong (rather than discovering it when the next
step fails).

### Tradeoffs

- The watchdog can restart the Docker container but cannot fix VM-level issues
  (Azure platform problems, kernel panics, network stack failures).
- Auto-restarting the container triggers a QEMU/Windows reboot, which takes 2-5
  minutes and resets the desktop state. This may be worse than leaving the container
  alone if the issue is transient.
- The client-side health monitor adds threading complexity to the recording script.
- Does not prevent data loss by itself -- must be combined with checkpoint/resume.

### Verdict: **Implement the VM-side watchdog during pool provisioning** (bake it into
the Docker setup script in `pool.py`)

---

## Option 5: Cloud logging for VM state

### Description

Send structured logs from the VM to a cloud-hosted log aggregator so that:
1. When the VM becomes unreachable, we can see the last logs before it went dark
2. We can correlate Azure platform events with WAA container events
3. Post-mortem analysis is possible without SSH access to the VM

### Options

| Service | Free Tier | Setup | Notes |
|---------|-----------|-------|-------|
| Azure Monitor / Log Analytics | 5 GB/month | Medium | Native Azure integration, `az monitor` CLI |
| Datadog | 5 hosts free | Medium | Full-featured, overkill for 1 VM |
| Grafana Cloud | 50 GB/month | Low-Medium | Good Loki for logs, free tier generous |
| Syslog to Blob Storage | Pay-per-GB (~$0.02/GB) | Low | Simple, just `rsyslog` + Azure Blob |
| **WandB** (already integrated) | Free for OSS | **Low** | Already have `WandbLogger` in codebase |

### Implementation (lightweight: journald + Azure Blob)

```bash
# On VM: Configure journald to capture Docker logs
cat >> /etc/systemd/journald.conf << 'EOF'
Storage=persistent
SystemMaxUse=500M
EOF

# Ship logs to Azure Blob every 5 minutes
cat > /usr/local/bin/ship-logs.sh << 'SCRIPT'
#!/bin/bash
LOG_FILE="/tmp/waa-logs-$(date +%Y%m%d_%H%M).jsonl"
journalctl --since "5 minutes ago" -o json > "$LOG_FILE"
az storage blob upload \
  --account-name openadaptstorage \
  --container-name vm-logs \
  --name "$(hostname)/$(basename $LOG_FILE)" \
  --file "$LOG_FILE" \
  --auth-mode login 2>/dev/null
rm "$LOG_FILE"
SCRIPT
chmod +x /usr/local/bin/ship-logs.sh
echo "*/5 * * * * /usr/local/bin/ship-logs.sh" | crontab -
```

### Implementation (WandB -- leverage existing integration)

The codebase already has `WandbLogger` in `openadapt_evals/integrations/wandb_logger.py`.
We could add a lightweight VM health logger that posts periodic status to a WandB run:

```python
# On the recording client side:
wandb.init(project="openadapt-evals", name="recording-session-{task_id}")
wandb.log({"step": step_idx, "status": "completed", "task_id": task_id})
```

### Effort: **Medium** (3-5 hours for blob approach, 1-2 hours for WandB)

### Impact: **Low-Medium**

Helps with diagnosis but does not prevent or recover from outages. Most useful for
understanding WHY the VM became unreachable (Azure platform issue vs. container OOM
vs. network flap).

### Tradeoffs

- Adds operational overhead (storage costs, log rotation, credential management)
- Does not directly prevent or recover from failures
- The WandB approach is easiest but requires network connectivity from the VM to WandB
  servers, which fails during the exact scenario we're trying to diagnose
- Azure Monitor is the most robust option but requires additional Azure setup

### Verdict: **Nice to have** -- implement after the higher-impact options, or if
debugging recurring unexplained outages

---

## Option 6: Azure VM keep-alive / serial console access

### Description

Address the root cause: the VM becoming unreachable despite showing "Running" in Azure.

**Azure Serial Console** provides out-of-band access to the VM even when SSH is broken.
It connects through the Azure fabric, not the network, so it works even when the VM's
networking stack is down.

**Azure VM Auto-Restart** can be configured via Azure Monitor alerts to automatically
restart a VM when it stops responding to health probes.

### Implementation

**Enable Serial Console** (one-time setup):
```bash
# Enable boot diagnostics (required for serial console)
az vm boot-diagnostics enable \
  --resource-group openadapt-agents \
  --name waa-pool-00

# Access serial console
az serial-console connect \
  --resource-group openadapt-agents \
  --name waa-pool-00
```

**Azure Monitor auto-restart rule**:
```bash
# Create action group for VM restart
az monitor action-group create \
  --resource-group openadapt-agents \
  --name vm-restart-action \
  --short-name vmrestart \
  --action azurefunction restart-vm ...

# Create alert rule on heartbeat metric
az monitor metrics alert create \
  --resource-group openadapt-agents \
  --name waa-vm-heartbeat \
  --scopes /subscriptions/.../virtualMachines/waa-pool-00 \
  --condition "avg Percentage CPU < 1 for 5 minutes" \
  --action vm-restart-action
```

**Local keep-alive ping from recording script**:
```python
# Periodically SSH a no-op command to keep the connection alive
# and detect failures early
def _keepalive_ping(vm_ip, ssh_user="azureuser", interval=30):
    while True:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             f"{ssh_user}@{vm_ip}", "echo alive"],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            print(f"\n  ALERT: VM not responding to SSH!")
        time.sleep(interval)
```

### Effort: **Medium** (3-4 hours)

### Impact: **Medium-High**

Serial Console provides a backup access path when SSH is down. The auto-restart alert
reduces the time to recovery from "user notices and manually restarts" to "Azure detects
and auto-restarts within 5-10 minutes".

### Tradeoffs

- Serial Console requires boot diagnostics to be enabled, which uses a storage account
  and has a small cost
- Auto-restart rules can cause false positives (low CPU does not always mean the VM is
  dead)
- Auto-restarting the VM loses all in-progress work unless checkpoint/resume is
  implemented (Option 2)
- These are Azure-specific solutions that would not transfer to other cloud providers

### Verdict: **Enable Serial Console immediately** (one command, zero ongoing cost);
defer auto-restart alert until checkpoint/resume is working

---

## Option 7: Use `mosh` instead of SSH for the interactive terminal

### Description

`mosh` (Mobile Shell) is designed for unreliable connections. It uses UDP instead of
TCP, handles roaming (IP changes), and provides instant local echo. Unlike SSH tunnels,
mosh maintains the session across network interruptions.

### Why it does NOT help here

Mosh only replaces the interactive terminal session. It does NOT support port forwarding
(tunnels). Since the recording workflow depends on SSH tunnels for VNC (8006), WAA (5001),
and evaluate (5050), mosh cannot replace SSH in this architecture.

Mosh would only help if the recording script ran on the VM (Option 3) and the user
connected via mosh for the interactive terminal. But VNC still requires an SSH tunnel.

### Effort: **N/A**
### Impact: **None** for this use case
### Verdict: **Do not implement** -- does not support port forwarding

---

## Recommended Implementation Order

| Priority | Option | Effort | Impact | Rationale |
|----------|--------|--------|--------|-----------|
| **1** | **autossh tunnels** | Low (1-2h) | Med-High | Immediate win: auto-reconnects tunnels on network flaps |
| **2** | **Checkpoint/resume** | Low (2-3h) | High | Prevents data loss even when VM is fully unreachable |
| **3** | **Serial Console** | Low (15 min) | Medium | One-time `az` command, provides emergency access |
| **4** | **VM-side watchdog** | Medium (4-6h) | Medium | Bake into pool provisioning; catches silent container deaths |
| **5** | **Client health monitor** | Low (1-2h) | Low-Med | Early warning for the user during recording |
| **6** | **Cloud logging (WandB)** | Low (1-2h) | Low-Med | Post-mortem diagnosis; leverage existing integration |
| **7** | **tmux on VM** | Med (2-4h) | Medium | Makes script tunnel-independent; pair with Option 2 |
| **8** | **Azure auto-restart** | Medium (3-4h) | Medium | Automated recovery; only useful with checkpoint/resume |

### Phase 1: Unblock recording NOW (3-4 hours total)

1. **Install `autossh`** and update tunnel commands in `run_dc_eval.py` and
   `SSHTunnelManager`. This handles the common case of transient tunnel drops.

2. **Add checkpoint/resume** to `record_waa_demos.py`. Save state to
   `{task_dir}/checkpoint.json` after every step. On restart, detect the checkpoint
   and offer to resume. This handles the catastrophic case (VM restart required).

3. **Enable Azure Serial Console** on waa-pool-00 (single `az` command). Provides
   emergency access when SSH is completely broken.

### Phase 2: Harden for reliability (1-2 days, lower priority)

4. Add the VM-side watchdog to the pool provisioning script (`pool.py`).
5. Add a background health check thread to the recording script.
6. Add lightweight WandB logging of recording session events.

### Phase 3: Long-term (when justified by recurring issues)

7. Move the recording script to run inside tmux on the VM.
8. Set up Azure Monitor auto-restart alerts.

---

## Appendix: What happened in the recent incident

The VM (waa-pool-00, 172.173.66.131) became unreachable via SSH despite Azure reporting
it as "Running". This is a known Azure behavior that can be caused by:

1. **Azure platform maintenance** -- the host is being migrated, and the VM is in a
   transient state where the guest OS is frozen but Azure has not yet marked it as
   failed.
2. **Guest OS hang** -- the Linux kernel or Docker daemon is in a deadlocked state
   (e.g., Docker out of disk space on the ephemeral drive, kernel OOM).
3. **Network Security Group (NSG) update** -- a stale NSG rule blocking SSH.
4. **Azure Accelerated Networking driver issue** -- known to cause intermittent
   connectivity loss on certain VM sizes.

The fix was a full VM restart (`az vm restart`), which takes 2-5 minutes. With
checkpoint/resume in place, this would lose at most the current in-progress step
(a few seconds of work) rather than the entire recording session.
