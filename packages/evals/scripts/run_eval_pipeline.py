#!/usr/bin/env python3
"""End-to-end eval pipeline: demo generation + VM lifecycle + ZS/DC evaluation.

Orchestrates the full evaluation flow:
  Phase 1 (parallel): generate VLM demos for new recordings + start VM if deallocated
  Phase 2 (sequential): establish SSH tunnels, socat proxy, wait for WAA readiness
  Phase 3: run ZS and DC evaluations via run_dc_eval logic
  Phase 4: print results summary

Usage:
    # Run everything for all recordings that have demos
    python scripts/run_eval_pipeline.py

    # Run for specific task(s)
    python scripts/run_eval_pipeline.py --tasks 04d9aeaf

    # Dry run (show what would happen)
    python scripts/run_eval_pipeline.py --tasks 04d9aeaf --dry-run

    # Override defaults
    python scripts/run_eval_pipeline.py \\
        --recordings waa_recordings \\
        --demo-dir demo_prompts_vlm \\
        --agent api-claude-cu \\
        --max-steps 15 \\
        --vm-name waa-pool-00 \\
        --zs-only

    # Use AWS instead of Azure
    python scripts/run_eval_pipeline.py --cloud aws --vm-name waa-pool-00

    # Train/eval UI parity mode (suppresses OneDrive/toast noise, pins image tag)
    python scripts/run_eval_pipeline.py --tasks 04d9aeaf --clean-desktop \\
        --force-tray-icons --waa-image-version win11-24h2-2026-03-04
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openadapt_evals.benchmarks.vm_cli import _create_vm_manager
from openadapt_evals.infrastructure.azure_vm import ssh_run, wait_for_ssh
from openadapt_evals.infrastructure.ssh_tunnel import SSHTunnelManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Repo root = parent of scripts/
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RECORDINGS = REPO_ROOT / "waa_recordings"
DEFAULT_DEMO_DIR = REPO_ROOT / "demo_prompts_vlm"
DEFAULT_OUTPUT = REPO_ROOT / "benchmark_results"
DEFAULT_VM_NAME = "waa-pool-00"
DEFAULT_RESOURCE_GROUP = "openadapt-agents"


# ── Phase 1a: Demo generation ─────────────────────────────────────────────


def _find_recordings_needing_demos(
    recordings_dir: Path,
    demo_dir: Path,
    task_filter: list[str] | None = None,
) -> list[tuple[Path, str]]:
    """Return (task_dir, task_id) pairs that have recordings but no demo file."""
    missing = []
    for task_dir in sorted(recordings_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        # Prefer meta_refined.json, fall back to meta.json
        meta_path = task_dir / "meta_refined.json"
        if not meta_path.exists():
            meta_path = task_dir / "meta.json"
        if not meta_path.exists():
            continue

        task_id = task_dir.name

        # Apply task filter (prefix matching)
        if task_filter:
            if not any(task_id.startswith(f) for f in task_filter):
                continue

        # Prefer multilevel demo (Option D format) over plain .txt
        demo_path = demo_dir / f"{task_id}_multilevel.txt"
        if not demo_path.exists():
            demo_path = demo_dir / f"{task_id}.txt"
        if not demo_path.exists():
            missing.append((task_dir, task_id))

    return missing


def _generate_demos(
    recordings_dir: Path,
    demo_dir: Path,
    task_filter: list[str] | None = None,
    provider: str = "openai",
    model: str | None = None,
) -> list[str]:
    """Generate VLM demos for recordings that don't have one yet.

    Returns list of task IDs that were generated.
    """
    missing = _find_recordings_needing_demos(recordings_dir, demo_dir, task_filter)
    if not missing:
        print("[demos] All recordings already have demo files")
        return []

    demo_dir.mkdir(parents=True, exist_ok=True)
    print(f"[demos] Generating VLM demos for {len(missing)} recording(s)...")

    generated = []
    for task_dir, task_id in missing:
        print(f"[demos] {task_id[:12]}...")

        cmd = [
            sys.executable, str(REPO_ROOT / "scripts" / "convert_recording_to_demo.py"),
            "--recordings", str(recordings_dir),
            "--output", str(demo_dir),
            "--mode", "vlm",
            "--provider", provider,
            "--task", task_id[:8],
        ]
        if model:
            cmd.extend(["--model", model])

        result = subprocess.run(cmd, timeout=600, capture_output=True, text=True)
        if result.returncode == 0:
            print("[demos]   -> done")
            generated.append(task_id)
        else:
            print(f"[demos]   ERROR: exit code {result.returncode}")
            if result.stderr:
                for line in result.stderr.strip().splitlines()[-3:]:
                    print(f"[demos]   ! {line}")

    return generated


# ── Phase 1b: VM lifecycle (uses VMProvider protocol) ─────────────────────


def _ensure_vm_running(
    vm_name: str,
    cloud: str = "azure",
    resource_group: str | None = None,
) -> str | None:
    """Ensure VM is running. Returns VM IP or None on failure.

    Uses the VMProvider protocol to support both Azure and AWS.
    Starts the VM if deallocated, then resolves its IP.
    """
    manager = _create_vm_manager(cloud=cloud, resource_group=resource_group)
    state = manager.get_vm_state(vm_name) or ""
    print(f"[vm] {vm_name}: {state or '(unknown)'}")

    if "running" in state.lower():
        ip = manager.get_vm_ip(vm_name)
        if ip:
            print(f"[vm] IP: {ip}")
        else:
            print(f"[vm] Could not resolve IP for {vm_name}")
        return ip

    if "deallocated" in state.lower() or "stopped" in state.lower():
        print(f"[vm] Starting {vm_name}...")
        if not manager.start_vm(vm_name):
            print("[vm] Start failed")
            return None
        print(f"[vm] {vm_name} started")
        ip = manager.get_vm_ip(vm_name)
        if ip:
            print(f"[vm] IP: {ip}")
        else:
            print(f"[vm] Could not resolve IP for {vm_name}")
        return ip

    # Unknown state
    print(f"[vm] Unexpected state: {state}")
    return None


def _ensure_container_running(vm_user: str, vm_ip: str) -> bool:
    """Ensure the WAA Docker container is running on the VM.

    After VM deallocate/start, the container may be in 'Exited' state.
    Uses ssh_run from infrastructure.azure_vm for SSH operations.
    """
    result = ssh_run(
        vm_ip,
        "docker inspect -f '{{.State.Running}}' winarena 2>/dev/null || echo missing",
        username=vm_user,
    )
    state = result.stdout.strip()

    if state == "true":
        print("[vm] WAA container already running")
        return True

    if state == "missing":
        print("[vm] WARNING: No winarena container found")
        return False

    # Container exists but not running — restart it
    print("[vm] WAA container not running, starting...")
    result = ssh_run(vm_ip, "docker start winarena", username=vm_user)
    if result.returncode == 0:
        print("[vm] WAA container started")
        return True

    print(f"[vm] Failed to start container: {result.stderr.strip()}")
    return False


def _deallocate_vm(
    vm_name: str,
    cloud: str = "azure",
    resource_group: str | None = None,
) -> bool:
    """Deallocate VM to stop billing.

    Uses the VMProvider protocol to support both Azure and AWS.
    """
    print(f"\n[vm] Deallocating {vm_name} (stops billing)...")
    manager = _create_vm_manager(cloud=cloud, resource_group=resource_group)
    if not manager.deallocate_vm(vm_name):
        print("[vm] Deallocate failed")
        return False
    print("[vm] Deallocate initiated. Billing will stop shortly.")
    return True


# ── Phase 2: Connectivity (uses SSHTunnelManager) ────────────────────────


def _setup_eval_proxy(vm_user: str, vm_ip: str) -> bool:
    """(Re-)establish socat proxy for the evaluate server on the VM.

    Uses ssh_run from infrastructure.azure_vm for the SSH command.
    """
    script = (
        "if systemctl list-unit-files socat-waa-evaluate.service "
        "| grep -q socat-waa-evaluate; then "
        "  sudo systemctl restart socat-waa-evaluate.service; "
        "else "
        "  killall socat 2>/dev/null || true; sleep 1; "
        "  which socat >/dev/null 2>&1 "
        "  || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq socat; "
        "  nohup socat TCP-LISTEN:5051,fork,reuseaddr "
        "  'EXEC:docker exec -i winarena socat - TCP\\:127.0.0.1\\:5050' "
        "  </dev/null >/dev/null 2>&1 & "
        "fi"
    )
    result = ssh_run(vm_ip, script, username=vm_user)
    if result.returncode != 0:
        print(f"  socat proxy setup failed: {result.stderr}")
        return False
    return True


def _probe(server: str, timeout: int = 10) -> bool:
    """HTTP probe to check if a server is alive."""
    import requests

    try:
        resp = requests.get(f"{server}/probe", timeout=timeout)
        return resp.ok
    except Exception:
        return False


def _ensure_waa_ready(
    server: str,
    vm_user: str,
    vm_ip: str,
    tunnel_manager: SSHTunnelManager,
    max_wait: int = 420,
    evaluate_url: str | None = None,
) -> bool:
    """Ensure WAA is reachable, recovering via tunnel reconnect if needed.

    Recovery sequence:
    1. Probe -> OK: return True
    2. Reconnect tunnel -> Probe -> OK: return True (skipped if vm_ip is empty)
    3. Wait for probe with timeout
    """
    if _probe(server) and (evaluate_url is None or _probe(evaluate_url)):
        return True

    if not vm_ip:
        print("  WAA unreachable and no VM IP available for tunnel reconnect")
        return False

    print("  WAA unreachable, reconnecting tunnel...")
    tunnel_manager.stop_all_tunnels()
    time.sleep(1)
    _setup_eval_proxy(vm_user, vm_ip)
    tunnel_manager.reset_reconnect_attempts()
    statuses = tunnel_manager.start_tunnels_for_vm(vm_ip, ssh_user=vm_user)
    any_active = any(s.active for s in statuses.values())
    if any_active:
        time.sleep(3)
        if _probe(server) and (evaluate_url is None or _probe(evaluate_url)):
            print("  Tunnel reconnected, WAA ready!")
            return True

    # Wait for WAA to come back
    deadline = time.time() + max_wait
    last_print = 0
    while time.time() < deadline:
        elapsed = int(time.time() - (deadline - max_wait))
        if elapsed - last_print >= 15:
            print(f"  [{elapsed}s] Waiting for WAA server...")
            last_print = elapsed
        if _probe(server, timeout=10) and (evaluate_url is None or _probe(evaluate_url, timeout=10)):
            print(f"  WAA ready after {elapsed}s!")
            return True
        time.sleep(10)

    print(f"  TIMEOUT: WAA not ready after {max_wait}s")
    return False


def _setup_connectivity(
    vm_ip: str,
    vm_user: str,
    tunnel_manager: SSHTunnelManager,
) -> bool:
    """Kill stale tunnels, establish fresh ones, set up socat proxy."""
    print("[conn] Killing stale SSH tunnels...")
    tunnel_manager.stop_all_tunnels()
    time.sleep(1)

    print("[conn] Setting up socat proxy for evaluate server...")
    _setup_eval_proxy(vm_user, vm_ip)

    print("[conn] Establishing SSH tunnels (5001, 5050, 8006)...")
    tunnel_manager.reset_reconnect_attempts()
    statuses = tunnel_manager.start_tunnels_for_vm(vm_ip, ssh_user=vm_user)

    failed = [name for name, s in statuses.items() if not s.active]
    if failed:
        print(f"[conn] ERROR: Failed to establish SSH tunnels: {', '.join(failed)}")
        return False

    time.sleep(2)
    print("[conn] Tunnels established")
    return True


def _wait_waa_ready(
    server: str = "http://localhost:5001",
    evaluate_url: str | None = None,
    timeout: int = 1200,
) -> bool:
    """Wait for WAA server and evaluate server to respond."""
    import requests

    deadline = time.time() + timeout
    last_print = 0
    start = time.time()

    print(f"[waa] Waiting for WAA readiness (timeout {timeout}s)...")
    while time.time() < deadline:
        elapsed = int(time.time() - start)
        if elapsed - last_print >= 15:
            print(f"[waa]   [{elapsed}s] probing...")
            last_print = elapsed

        try:
            waa_ok = requests.get(f"{server}/probe", timeout=10).ok
        except Exception:
            waa_ok = False

        eval_ok = True  # default to True when no separate evaluate URL
        if evaluate_url:
            try:
                eval_ok = requests.get(f"{evaluate_url}/probe", timeout=10).ok
            except Exception:
                eval_ok = False

        if waa_ok and eval_ok:
            print(f"[waa] WAA server ready after {elapsed}s")
            return True
        if waa_ok and not eval_ok:
            # WAA is up but evaluate isn't — acceptable for ZS-only runs
            if elapsed > 60:
                print("[waa] WAA ready but evaluate server not responding (continuing)")
                return True

        time.sleep(10)

    print(f"[waa] TIMEOUT: not ready after {timeout}s")
    return False


# ── Phase 3: Run evaluations ──────────────────────────────────────────────


def _build_conditions(
    task_ids: list[str],
    demo_dir: Path,
    zs_only: bool = False,
    dc_only: bool = False,
) -> list[tuple[str, str, Path | None]]:
    """Build (task_id, run_name, demo_path_or_None) tuples."""
    conditions = []
    for tid in task_ids:
        sid = tid[:8]
        if not dc_only:
            conditions.append((tid, f"val_zs_{sid}", None))
        if not zs_only:
            # Prefer multilevel demo (Option D format) over plain .txt
            demo_path = demo_dir / f"{tid}_multilevel.txt"
            if not demo_path.exists():
                demo_path = demo_dir / f"{tid}.txt"
            if not demo_path.exists():
                demo_path = demo_dir / f"{tid}.json"
            if demo_path.exists():
                conditions.append((tid, f"val_dc_{sid}", demo_path))
            else:
                print(f"[eval] WARNING: No demo for {tid[:12]}..., skipping DC condition")
    return conditions


def _run_eval(
    conditions: list[tuple[str, str, Path | None]],
    agent: str,
    server: str,
    evaluate_url: str | None,
    max_steps: int,
    output_dir: Path,
    vm_ip: str,
    vm_user: str,
    tunnel_manager: SSHTunnelManager,
    clean_desktop: bool = False,
    force_tray_icons: bool = False,
    waa_image_version: str | None = None,
) -> dict[str, dict]:
    """Run all eval conditions sequentially with health checks."""
    results = {}

    for i, (tid, run_name, demo_path) in enumerate(conditions):
        cond_label = "DC" if demo_path else "ZS"
        print(f"\n{'=' * 60}")
        print(f"[{i+1}/{len(conditions)}] {cond_label}: {tid[:40]}...")
        if demo_path:
            print(f"  Demo: {demo_path.name} ({demo_path.stat().st_size} bytes)")
        print(f"  Run: {run_name}")
        print(f"{'=' * 60}")

        # Health check before each run
        if not _ensure_waa_ready(
            server, vm_user, vm_ip,
            tunnel_manager=tunnel_manager,
            evaluate_url=evaluate_url,
        ):
            print(f"  Skipping {run_name} — WAA unreachable after recovery")
            results[run_name] = {
                "status": "SKIP",
                "returncode": -1,
                "elapsed_s": 0,
                "task_id": tid,
                "condition": cond_label,
            }
            continue

        task_start = time.time()

        cmd = [
            sys.executable, "-m", "openadapt_evals.benchmarks.cli",
            "run",
            "--agent", agent,
            "--tasks", tid,
            "--server", server,
            "--max-steps", str(max_steps),
            "--output", str(output_dir),
            "--run-name", run_name,
        ]
        if evaluate_url:
            cmd.extend(["--evaluate-url", evaluate_url])
        if clean_desktop:
            cmd.append("--clean-desktop")
        if force_tray_icons:
            cmd.append("--force-tray-icons")
        if waa_image_version:
            cmd.extend(["--waa-image-version", waa_image_version])
        if demo_path:
            cmd.extend(["--demo", str(demo_path.resolve())])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        elapsed = time.time() - task_start

        # Log captured output to a file and print summary
        log_dir = output_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{run_name}.log"
        with open(log_path, "w") as f:
            f.write(f"=== STDOUT ===\n{result.stdout}\n")
            f.write(f"=== STDERR ===\n{result.stderr}\n")
        print(f"  Output logged to: {log_path}")

        # Print last few lines of stdout for quick feedback
        stdout_lines = result.stdout.strip().splitlines()
        if stdout_lines:
            tail = stdout_lines[-min(5, len(stdout_lines)):]
            for line in tail:
                print(f"  | {line}")

        if result.stderr.strip():
            stderr_lines = result.stderr.strip().splitlines()
            tail = stderr_lines[-min(3, len(stderr_lines)):]
            for line in tail:
                print(f"  ! {line}")

        status = "OK" if result.returncode == 0 else f"FAIL (rc={result.returncode})"
        results[run_name] = {
            "status": status,
            "returncode": result.returncode,
            "elapsed_s": elapsed,
            "task_id": tid,
            "condition": cond_label,
            "log_path": str(log_path),
        }
        print(f"\n  -> {status} ({elapsed:.0f}s)")

    return results


# ── Phase 4: Summary ──────────────────────────────────────────────────────


def _print_summary(results: dict[str, dict], agent: str) -> None:
    ok = sum(1 for r in results.values() if r["returncode"] == 0)
    total_time = sum(r["elapsed_s"] for r in results.values())

    print(f"\n{'=' * 60}")
    print(f"PIPELINE SUMMARY ({agent})")
    print(f"{'=' * 60}")
    print(f"  Runs: {ok}/{len(results)} completed")
    print(f"  Total eval time: {total_time:.0f}s ({total_time/60:.1f}min)")
    print()

    for name, r in results.items():
        cond = r.get("condition", "?")
        print(f"  {name:30s}  {cond:2s}  {r['status']:15s}  {r['elapsed_s']:.0f}s")

    # Print log file locations
    log_paths = [r.get("log_path") for r in results.values() if r.get("log_path")]
    if log_paths:
        print("\n  Logs:")
        for p in log_paths:
            print(f"    {p}")

    print()


# ── Main ──────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the eval pipeline.

    Extracted so tests can use the same parser without reconstructing it.
    """
    parser = argparse.ArgumentParser(
        description="End-to-end eval pipeline: demos + VM + ZS/DC evaluation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--tasks",
        help="Comma-separated task IDs or prefixes (default: all with recordings)",
    )
    parser.add_argument(
        "--recordings", default=str(DEFAULT_RECORDINGS),
        help=f"Recordings directory (default: {DEFAULT_RECORDINGS.name})",
    )
    parser.add_argument(
        "--demo-dir", default=str(DEFAULT_DEMO_DIR),
        help=f"Demo output directory (default: {DEFAULT_DEMO_DIR.name})",
    )
    parser.add_argument("--agent", default="api-claude-cu", help="Agent type")
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
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
    parser.add_argument("--server", default="http://localhost:5001")
    parser.add_argument("--evaluate-url", default=None,
                        help="Evaluate server URL (default: same as --server)")
    parser.add_argument(
        "--vm-name", default=DEFAULT_VM_NAME, help="VM name",
    )
    parser.add_argument(
        "--resource-group", default=DEFAULT_RESOURCE_GROUP,
        help="Azure resource group (ignored for AWS)",
    )
    parser.add_argument(
        "--cloud", default="azure", choices=["azure", "aws"],
        help="Cloud provider (default: azure)",
    )
    parser.add_argument("--vm-ip", default=None, help="VM IP (skip auto-detection)")
    parser.add_argument(
        "--vlm-provider", default="openai", help="VLM provider for demo generation",
    )
    parser.add_argument("--vlm-model", default=None, help="VLM model override")
    parser.add_argument("--zs-only", action="store_true", help="Zero-shot only")
    parser.add_argument("--dc-only", action="store_true", help="Demo-conditioned only")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would happen without running",
    )
    parser.add_argument(
        "--skip-vm", action="store_true",
        help="Skip VM start (assume tunnels already up)",
    )
    parser.add_argument(
        "--vnc", action="store_true", default=True,
        help="Open VNC viewer in browser (default: True)",
    )
    parser.add_argument(
        "--no-vnc", dest="vnc", action="store_false",
        help="Do not open VNC viewer",
    )
    parser.add_argument(
        "--deallocate-after", action="store_true",
        help="Deallocate VM after eval completes (stops billing)",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    recordings_dir = Path(args.recordings)
    demo_dir = Path(args.demo_dir)
    output_dir = Path(args.output)

    # Resolve VM manager and user lazily — skip if --dry-run or --skip-vm
    vm_manager = None
    vm_user = None
    if not args.dry_run:
        vm_manager = _create_vm_manager(cloud=args.cloud, resource_group=args.resource_group)
        vm_user = vm_manager.ssh_username

    # Resolve task filter
    task_filter = None
    if args.tasks:
        task_filter = [t.strip() for t in args.tasks.split(",")]

    # ── Discover what we have ──────────────────────────────────────────

    # Find all recorded task IDs
    recorded_tasks = []
    for d in (sorted(recordings_dir.iterdir()) if recordings_dir.exists() else []):
        if d.is_dir() and ((d / "meta.json").exists() or (d / "meta_refined.json").exists()):
            if task_filter is None or any(d.name.startswith(f) for f in task_filter):
                recorded_tasks.append(d.name)

    # Find all task IDs that already have demos
    existing_demos = set()
    if demo_dir.exists():
        for f in demo_dir.iterdir():
            if f.suffix == ".txt":
                existing_demos.add(f.stem)

    missing_demos = [t for t in recorded_tasks if t not in existing_demos]

    # Tasks eligible for eval = those with demos (or will have demos after generation)
    eval_tasks = list(recorded_tasks)  # copy; demos will be generated for missing ones

    print("Pipeline Configuration")
    print(f"  Recordings: {recordings_dir} ({len(recorded_tasks)} task(s))")
    print(f"  Demo dir:   {demo_dir} ({len(existing_demos)} existing)")
    print(f"  Missing:    {len(missing_demos)} demo(s) to generate")
    print(f"  Agent:      {args.agent}")
    print(f"  Cloud:      {args.cloud}")
    resource_scope = vm_manager.resource_scope if vm_manager else args.resource_group
    print(f"  VM:         {args.vm_name} ({resource_scope})")
    print(f"  VM user:    {vm_user or '(resolved at runtime)'}")
    print(f"  Conditions: {'ZS only' if args.zs_only else 'DC only' if args.dc_only else 'ZS + DC'}")
    print()

    if not recorded_tasks:
        print("ERROR: No recordings found. Record demos first.")
        return 1

    if args.dry_run:
        print("[dry-run] Phase 1a: Would generate demos for:")
        for t in missing_demos:
            print(f"  - {t}")
        if not missing_demos:
            print("  (none — all demos exist)")

        print(f"\n[dry-run] Phase 1b: Would check/start VM {args.vm_name}")

        conditions = _build_conditions(eval_tasks, demo_dir, args.zs_only, args.dc_only)
        # For dry-run, also count tasks that will have demos after generation
        if missing_demos and not args.zs_only:
            for t in missing_demos:
                if not any(c[0] == t and c[2] is not None for c in conditions):
                    conditions.append((t, f"val_dc_{t[:8]}", Path("(will be generated)")))

        print(f"\n[dry-run] Phase 3: Would run {len(conditions)} evaluation(s):")
        for tid, run_name, demo_path in conditions:
            cond = "DC" if demo_path else "ZS"
            print(f"  - {run_name} ({cond})")

        print("\n[dry-run] No actions taken.")
        return 0

    # ── Phase 1: Parallel setup ────────────────────────────────────────

    print(f"\n{'─' * 60}")
    print("PHASE 1: Parallel setup (demo generation + VM start)")
    print(f"{'─' * 60}\n")

    vm_ip = args.vm_ip
    phase1_errors = []

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {}

        # Thread A: Generate missing demos
        if missing_demos:
            futures["demos"] = pool.submit(
                _generate_demos,
                recordings_dir,
                demo_dir,
                task_filter,
                args.vlm_provider,
                args.vlm_model,
            )
        else:
            print("[demos] All demos already exist, skipping generation")

        # Thread B: Ensure VM is running
        if not args.skip_vm and vm_ip is None:
            futures["vm"] = pool.submit(
                _ensure_vm_running,
                args.vm_name,
                args.cloud,
                args.resource_group,
            )
        elif vm_ip:
            print(f"[vm] Using provided IP: {vm_ip}")
        else:
            print("[vm] Skipping VM start (--skip-vm)")

        # Wait for results
        for key, future in futures.items():
            try:
                result = future.result(timeout=600)
                if key == "vm":
                    if result is None:
                        phase1_errors.append("VM failed to start")
                    else:
                        vm_ip = result
                elif key == "demos":
                    if result:
                        print(f"[demos] Generated {len(result)} demo(s)")
            except Exception as e:
                phase1_errors.append(f"{key}: {e}")
                print(f"[{key}] ERROR: {e}")

    if phase1_errors and not args.skip_vm:
        print(f"\nPhase 1 errors: {phase1_errors}")
        if vm_ip is None:
            print("ERROR: Cannot proceed without VM IP")
            return 1

    # ── Phase 2: Connectivity ──────────────────────────────────────────

    # Create tunnel manager for the session
    tunnel_manager = SSHTunnelManager()

    if not args.skip_vm:
        print(f"\n{'─' * 60}")
        print("PHASE 2: Connectivity (SSH tunnels + WAA readiness)")
        print(f"{'─' * 60}\n")

        print(f"[vm] Waiting for SSH on {vm_ip}...")
        if not wait_for_ssh(vm_ip, timeout=180, username=vm_user):
            print("ERROR: SSH not reachable")
            return 1
        print("[vm] SSH ready")

        if not _ensure_container_running(vm_user, vm_ip):
            print("ERROR: WAA container not running and could not be started")
            return 1

        if not _setup_connectivity(vm_ip, vm_user, tunnel_manager):
            print("ERROR: Failed to set up tunnels")
            return 1

        if args.vnc:
            vnc_url = "http://localhost:8006"
            print(f"[vnc] Opening VNC viewer: {vnc_url}")
            webbrowser.open(vnc_url)

        if not _wait_waa_ready(args.server, args.evaluate_url):
            print("ERROR: WAA server not ready")
            return 1
    else:
        print("\n[conn] Skipping connectivity setup (--skip-vm)")

    # ── Phase 3: Run evaluations ───────────────────────────────────────

    print(f"\n{'─' * 60}")
    print("PHASE 3: Evaluation")
    print(f"{'─' * 60}")

    conditions = _build_conditions(eval_tasks, demo_dir, args.zs_only, args.dc_only)

    if not conditions:
        print("ERROR: No evaluation conditions to run")
        return 1

    print(f"\n  {len(conditions)} run(s) queued:")
    for tid, run_name, demo_path in conditions:
        cond = "DC" if demo_path else "ZS"
        print(f"    {run_name} ({cond})")
    print()

    results = _run_eval(
        conditions,
        agent=args.agent,
        server=args.server,
        evaluate_url=args.evaluate_url,
        max_steps=args.max_steps,
        output_dir=output_dir,
        vm_ip=vm_ip or "",
        vm_user=vm_user,
        tunnel_manager=tunnel_manager,
        clean_desktop=args.clean_desktop,
        force_tray_icons=args.force_tray_icons,
        waa_image_version=args.waa_image_version,
    )

    # ── Phase 4: Summary ──────────────────────────────────────────────

    _print_summary(results, args.agent)

    # Clean up tunnels
    tunnel_manager.stop_all_tunnels()

    # ── Optional: Deallocate VM ───────────────────────────────────────

    if args.deallocate_after and not args.skip_vm:
        _deallocate_vm(args.vm_name, args.cloud, args.resource_group)

    ok = sum(1 for r in results.values() if r["returncode"] == 0)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
