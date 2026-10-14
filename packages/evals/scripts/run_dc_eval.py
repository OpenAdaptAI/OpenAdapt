#!/usr/bin/env python3
"""Run demo-conditioned evaluation with auto-recovery.

Runs each task individually with its matching demo file. Includes:
- SSH tunnel health check + reconnect before each task
- WAA server health check with container restart if dead
- Retry on transient failures

Usage:
    # 3-task validation with ClaudeComputerUseAgent
    python scripts/run_dc_eval.py \
      --agent api-claude-cu \
      --tasks 0e763496,70745df8,fba2c100

    # All 12 tasks
    python scripts/run_dc_eval.py --agent api-claude-cu

    # Deterministic desktop parity + pinned image version metadata
    python scripts/run_dc_eval.py --agent api-claude-cu --clean-desktop \\
      --force-tray-icons --waa-image-version win11-24h2-2026-03-04
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import requests

from openadapt_evals.constants import HARDER_TASK_IDS


def short_id(task_id: str) -> str:
    return task_id[:8]


def _kill_tunnels():
    subprocess.run(
        "ps aux | grep 'ssh.*5001' | grep -v grep | awk '{print $2}' | xargs kill 2>/dev/null",
        shell=True, capture_output=True,
    )


def _start_tunnel(vm_user: str, vm_ip: str) -> bool:
    cmd = [
        "ssh", "-f", "-N",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "TCPKeepAlive=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-L", "5001:localhost:5000",
        "-L", "8006:localhost:8006",
        f"{vm_user}@{vm_ip}",
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0


def _dismiss_lock_screen(server: str) -> bool:
    """Detect and dismiss Windows lock screen if present.

    Checks for LogonUI.exe process (Windows lock screen). If found,
    types the password ("admin") and presses Enter to unlock.

    Returns True if lock screen was dismissed or wasn't present.
    """
    def lock_state() -> bool | None:
        """Return True when locked, False when unlocked, or None if unknown."""
        resp = requests.post(
            f"{server}/execute",
            json={"command": 'powershell -Command "(Get-Process LogonUI -ErrorAction SilentlyContinue) -ne $null"'},
            timeout=10,
        )
        if not resp.ok:
            print(f"  Lock screen check failed: HTTP {resp.status_code}")
            return None

        output = resp.json().get("output", "").strip()
        if output == "True":
            return True
        if output == "False":
            return False
        print(f"  Lock screen check returned an unreadable state: {output!r}")
        return None

    try:
        state = lock_state()
        if state is None:
            return False
        if state is False:
            return True

        print("  Lock screen detected (LogonUI.exe running), dismissing...")

        # Type password and press Enter via pyautogui
        resp = requests.post(
            f"{server}/execute",
            json={"command": 'python -c "import pyautogui, time; pyautogui.press(\'enter\'); time.sleep(1); pyautogui.typewrite(\'admin\', interval=0.05); time.sleep(0.5); pyautogui.press(\'enter\')"'},
            timeout=15,
        )
        if resp.ok:
            print("  Unlock input sent, waiting for desktop...")
            time.sleep(5)  # Wait for desktop to load
            unlocked = lock_state() is False
            if not unlocked:
                print("  Lock screen remained present or could not be rechecked")
            return unlocked
        else:
            print(f"  Failed to dismiss lock screen: {resp.text}")
            return False

    except Exception as e:
        print(f"  Lock screen check error: {e}")
        return False


def _probe(server: str, timeout: int = 10) -> bool:
    try:
        resp = requests.get(f"{server}/probe", timeout=timeout)
        return resp.ok
    except Exception:
        return False


def _restart_container(vm_user: str, vm_ip: str) -> bool:
    """Restart Windows via QEMU monitor reset, falling back to docker restart.

    The QEMU monitor approach (``system_reset`` on port 7100) is preferred
    because it performs a reliable hard reset without killing the container.
    Falls back to ``docker restart winarena`` if the QEMU monitor is
    unreachable (e.g. ``nc`` not installed in the container).
    """
    from openadapt_evals.infrastructure.qemu_reset import QEMUResetManager

    mgr = QEMUResetManager(vm_ip=vm_ip, ssh_user=vm_user, timeout_seconds=300)

    # Try QEMU monitor reset first (preferred)
    if mgr.is_qemu_monitor_reachable():
        print("  Resetting Windows via QEMU monitor (system_reset)...")
        if mgr.reset_windows():
            print("  QEMU reset sent.")
            return True
        print("  QEMU reset command failed, falling back to docker restart...")
    else:
        print("  QEMU monitor unreachable, falling back to docker restart...")

    # Fallback: docker restart
    print("  Restarting WAA container (docker restart winarena)...")
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", f"{vm_user}@{vm_ip}",
         "docker restart winarena"],
        capture_output=True, timeout=120,
    )
    if result.returncode != 0:
        print(f"  Container restart failed: {result.stderr.decode()}")
        return False
    print("  Container restarted.")
    return True


def ensure_waa_ready(
    server: str,
    vm_user: str,
    vm_ip: str,
    max_wait: int = 420,
    evaluate_url: str | None = None,
) -> bool:
    """Ensure WAA is reachable, recovering via tunnel reconnect or container restart.

    Recovery sequence:
    1. Probe → OK: return True
    2. Reconnect tunnel → Probe → OK: return True
    3. Restart container → Reconnect tunnel → Wait for probe: return True/False

    If evaluate_url is provided, both the WAA server and evaluate server must respond.
    """
    # Step 1: Quick probe
    if _probe(server) and (evaluate_url is None or _probe(evaluate_url)):
        return _dismiss_lock_screen(server)

    # Step 2: Reconnect tunnel
    print("  WAA unreachable, reconnecting tunnel...")
    _kill_tunnels()
    time.sleep(1)
    if _start_tunnel(vm_user, vm_ip):
        time.sleep(3)
        if _probe(server) and (evaluate_url is None or _probe(evaluate_url)):
            print("  Tunnel reconnected, WAA ready!")
            return _dismiss_lock_screen(server)

    # Step 3: Tunnel up but WAA not responding → container restart
    print("  Tunnel OK but WAA server dead, restarting container...")
    _kill_tunnels()
    if not _restart_container(vm_user, vm_ip):
        return False

    # Wait for Windows to boot + Flask server to start
    time.sleep(10)
    _kill_tunnels()
    time.sleep(1)
    if not _start_tunnel(vm_user, vm_ip):
        print("  Failed to reconnect tunnel after restart")
        return False

    deadline = time.time() + max_wait
    last_print = 0
    while time.time() < deadline:
        elapsed = int(time.time() - (deadline - max_wait))
        if elapsed - last_print >= 15:
            print(f"  [{elapsed}s] Waiting for WAA server...")
            last_print = elapsed
        if _probe(server, timeout=10) and (evaluate_url is None or _probe(evaluate_url, timeout=10)):
            print(f"  WAA ready after {elapsed}s!")
            return _dismiss_lock_screen(server)
        time.sleep(10)

    print(f"  TIMEOUT: WAA not ready after {max_wait}s")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Run DC eval with auto-recovery")
    parser.add_argument("--agent", default="api-claude-cu", help="Agent type")
    parser.add_argument("--demo-dir", default="annotated_demos", help="Demo directory")
    parser.add_argument("--server", default="http://localhost:5001")
    parser.add_argument("--evaluate-url", default=None,
                        help="Evaluate server URL (default: same as --server)")
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--output", default="benchmark_results")
    parser.add_argument(
        "--clean-desktop",
        action="store_true",
        help="Apply deterministic clean-desktop policy (OneDrive/toast suppression + tray parity)",
    )
    parser.add_argument(
        "--force-tray-icons",
        action="store_true",
        help="Force network/audio tray icons visible before each run",
    )
    parser.add_argument(
        "--waa-image-version",
        default=None,
        help="Pinned WAA image version label to record in run metadata",
    )
    parser.add_argument("--tasks", help="Comma-separated task IDs or prefixes (default: all 12)")
    parser.add_argument("--start-from", type=int, default=0, help="Task index to start from")
    parser.add_argument("--vm-ip", default=None, help="VM IP (auto-detected if omitted)")
    parser.add_argument("--vm-user", default="azureuser", help="VM SSH user")
    parser.add_argument("--zs-only", action="store_true", help="Run zero-shot only (no demo)")
    parser.add_argument("--dc-only", action="store_true", help="Run demo-conditioned only")
    parser.add_argument(
        "--controller",
        action="store_true",
        help=(
            "Use the DemoController state machine for DC runs. "
            "The controller drives the agent step-by-step through the plan "
            "with VLM verification at each step, retry on failure, and "
            "replanning when retries are exhausted."
        ),
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Max retries per step when using --controller (default: 2)",
    )
    parser.add_argument(
        "--max-replans",
        type=int,
        default=2,
        help="Max replans when using --controller (default: 2)",
    )
    parser.add_argument(
        "--done-gate",
        action="store_true",
        help="Verify task completion before accepting agent's 'done' signal",
    )
    parser.add_argument(
        "--done-gate-max-overrides",
        type=int,
        default=3,
        help="Max times to override premature 'done' (default: 3)",
    )
    parser.add_argument(
        "--done-gate-threshold",
        type=float,
        default=1.0,
        help="Minimum score to accept 'done' (default: 1.0)",
    )
    args = parser.parse_args()

    demo_dir = Path(args.demo_dir)
    output_dir = Path(args.output)

    # Resolve task IDs (support prefix matching)
    if args.tasks:
        raw_ids = [t.strip() for t in args.tasks.split(",")]
        task_ids = []
        for raw in raw_ids:
            matches = [tid for tid in HARDER_TASK_IDS if tid.startswith(raw)]
            if len(matches) == 1:
                task_ids.append(matches[0])
            elif len(matches) > 1:
                print(f"Ambiguous prefix '{raw}': {matches}")
                return 1
            else:
                task_ids.append(raw)  # Use as-is
    else:
        task_ids = HARDER_TASK_IDS

    # Build run conditions
    conditions = []  # (task_id, run_name, demo_path_or_None)
    for tid in task_ids:
        sid = short_id(tid)
        if not args.dc_only:
            conditions.append((tid, f"val_zs_{sid}", None))
        if not args.zs_only:
            # Prefer multilevel demo (Option D format) over plain .txt
            demo_path = demo_dir / f"{tid}_multilevel.txt"
            if not demo_path.exists():
                demo_path = demo_dir / f"{tid}.txt"
            if not demo_path.exists():
                demo_path = demo_dir / f"{tid}.json"
            if not demo_path.exists():
                print(f"WARNING: No demo for {tid}, skipping DC condition")
                continue
            conditions.append((tid, f"val_dc_{sid}", demo_path))

    from openadapt_evals.infrastructure.vm_ip import resolve_vm_ip

    vm_ip = resolve_vm_ip(args.vm_ip)

    print(f"Eval: {len(conditions)} runs ({len(task_ids)} tasks) with {args.agent}")
    if args.controller:
        print(f"Controller: ENABLED (max_retries={args.max_retries}, max_replans={args.max_replans})")
    print(f"VM: {vm_ip}")
    print()

    # Verify initial WAA health
    if not ensure_waa_ready(args.server, args.vm_user, vm_ip, evaluate_url=args.evaluate_url):
        print("ERROR: Cannot reach WAA server or evaluate server")
        return 1

    results = {}
    start_time = time.time()

    for i, (tid, run_name, demo_path) in enumerate(conditions):
        if i < args.start_from:
            print(f"[{i+1}/{len(conditions)}] Skipping {run_name}")
            continue

        # Health check before each run
        if not ensure_waa_ready(args.server, args.vm_user, vm_ip, evaluate_url=args.evaluate_url):
            print(f"  Skipping {run_name} - WAA unreachable after recovery")
            results[run_name] = {"status": "SKIP", "returncode": -1, "elapsed_s": 0}
            continue

        cond_label = "DC" if demo_path else "ZS"
        print(f"{'=' * 60}")
        print(f"[{i+1}/{len(conditions)}] {cond_label}: {tid}")
        if demo_path:
            print(f"  Demo: {demo_path.name} ({demo_path.stat().st_size} bytes)")
        print(f"  Run: {run_name}")
        print(f"{'=' * 60}")

        task_start = time.time()

        cmd = [
            sys.executable, "-m", "openadapt_evals.benchmarks.cli",
            "run",
            "--agent", args.agent,
            "--tasks", tid,
            "--server", args.server,
            "--max-steps", str(args.max_steps),
            "--output", str(output_dir),
            "--run-name", run_name,
        ]
        if args.evaluate_url:
            cmd.extend(["--evaluate-url", args.evaluate_url])
        if demo_path:
            cmd.extend(["--demo", str(demo_path.resolve())])
        if args.controller and demo_path:
            cmd.extend([
                "--controller",
                "--max-retries", str(args.max_retries),
                "--max-replans", str(args.max_replans),
            ])
        if args.clean_desktop:
            cmd.append("--clean-desktop")
        if args.force_tray_icons:
            cmd.append("--force-tray-icons")
        if args.waa_image_version:
            cmd.extend(["--waa-image-version", args.waa_image_version])
        if args.done_gate:
            cmd.extend([
                "--done-gate",
                "--done-gate-max-overrides", str(args.done_gate_max_overrides),
                "--done-gate-threshold", str(args.done_gate_threshold),
            ])

        result = subprocess.run(cmd)
        elapsed = time.time() - task_start

        status = "OK" if result.returncode == 0 else f"FAIL (rc={result.returncode})"
        results[run_name] = {
            "status": status,
            "returncode": result.returncode,
            "elapsed_s": elapsed,
            "task_id": tid,
            "condition": cond_label,
        }

        print(f"\n  -> {status} ({elapsed:.0f}s)\n")

    # Summary
    total_time = time.time() - start_time
    ok = sum(1 for r in results.values() if r["returncode"] == 0)

    print(f"\n{'=' * 60}")
    print(f"EVAL SUMMARY ({args.agent})")
    print(f"{'=' * 60}")
    print(f"  Runs: {ok}/{len(results)} completed")
    print(f"  Total time: {total_time:.0f}s ({total_time/60:.1f}min)")
    print()

    for name, r in results.items():
        cond = r.get("condition", "?")
        print(f"  {name:30s}  {cond:2s}  {r['status']:15s}  {r['elapsed_s']:.0f}s")

    print(f"\n  Results in: {output_dir}/val_*/")

    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
