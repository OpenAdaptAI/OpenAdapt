#!/usr/bin/env python3
"""Evaluate openadapt-flow on WindowsAgentArena (WAA).

Two eval modes, both scored by WAA's own task verifier:

  replay  -- demonstrate-then-replay: compile ONE demo into a bundle and replay
             it via WindowsBackend against the WAA in-guest server (~0 model
             calls). Paradigm-correct eval for a demonstration compiler.
  hybrid  -- compiled replay first, computer-use agent fallback ONLY on a
             detected halt. Drive-able by the WAA runner; directly comparable
             to a pure agent baseline on the same tasks.

Safety: this command is **dry-run by default**. It never provisions Azure,
never starts a VM, never spends money, and makes no network calls unless you
pass ``--live`` (which additionally requires a reachable WAA server and stays
under the hard cost caps). The prior $40-70 uncapped-run incident is why the
caps below are mandatory.

Examples:
    # Cost estimate + plan for a 10-task and full (154) replay run (no network):
    python scripts/eval_flow_on_waa.py --mode replay --tasks 10 --dry-run
    python scripts/eval_flow_on_waa.py --mode replay --tasks 154 --dry-run

    # Hybrid dry-run with an assumed 30% halt/fallback rate:
    python scripts/eval_flow_on_waa.py --mode hybrid --tasks 154 --fallback-rate 0.3 --dry-run

    # Live run (GATED: needs Azure + a revived waa-pool + maintainer go):
    python scripts/eval_flow_on_waa.py --mode replay --task-ids <id1>,<id2> \
        --bundles ./flow_bundles --server-url http://localhost:5001 --live
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

# Ensure repo root on path when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openadapt_evals.flow.cost import (  # noqa: E402
    DEFAULT_MODEL,
    MODELS,
    CostGuardConfig,
    SpendLedger,
    estimate_flow_waa_cost,
)

FULL_BENCHMARK_TASKS = 154


def _discover_bundle(bundles_dir: Optional[Path], task_id: str) -> Optional[Path]:
    """Return ``<bundles_dir>/<task_id>`` if it is a compiled bundle, else None."""
    if bundles_dir is None:
        return None
    candidate = bundles_dir / task_id
    if (candidate / "workflow.json").exists():
        return candidate
    return None


def build_plan(args: argparse.Namespace) -> dict:
    """Build the run plan (task list + bundle discovery). No network."""
    bundles_dir = Path(args.bundles) if args.bundles else None
    task_ids: list[str]
    if args.task_ids:
        task_ids = [t.strip() for t in args.task_ids.split(",") if t.strip()]
    else:
        task_ids = [f"task_{i:03d}" for i in range(args.tasks)]

    tasks = []
    missing = 0
    for tid in task_ids:
        bundle = _discover_bundle(bundles_dir, tid)
        if bundle is None:
            missing += 1
        tasks.append({"task_id": tid, "bundle_dir": str(bundle) if bundle else None})

    return {
        "mode": args.mode,
        "num_tasks": len(task_ids),
        "server_url": args.server_url,
        "bundles_dir": str(bundles_dir) if bundles_dir else None,
        "tasks_with_bundle": len(task_ids) - missing,
        "tasks_missing_bundle": missing,
        "tasks": tasks,
    }


def build_guard(args: argparse.Namespace) -> CostGuardConfig:
    return CostGuardConfig(
        per_run_usd=args.max_run_usd,
        total_usd=args.max_total_usd,
        per_task_tokens=args.max_task_tokens,
        billing_abort_after=args.billing_abort_after,
    )


def print_estimate(args: argparse.Namespace, guard: CostGuardConfig) -> dict:
    """Print + return the cost estimate for the requested N and the full 154.

    Parallels is a LOCAL environment: VM cost is $0 (native Apple-Silicon virt);
    only the hybrid agent-fallback tokens (a cloud API) cost money. WAA is a
    CLOUD environment: Azure VM-hours + agent tokens.
    """
    is_parallels = args.env == "parallels"
    vm_hourly = 0.0 if is_parallels else args.vm_hourly
    vm_label = "Local VM (Parallels):" if is_parallels else "Azure VM-hours:      "
    rows = []
    seen = []
    for n in [args.tasks if not args.task_ids else len(
        [t for t in args.task_ids.split(",") if t.strip()]
    ), FULL_BENCHMARK_TASKS]:
        if n in seen:
            continue
        seen.append(n)
        est = estimate_flow_waa_cost(
            n,
            mode=args.mode,
            model_name=args.model,
            fallback_rate=args.fallback_rate,
            vm_hourly=vm_hourly,
        )
        rows.append(est.as_dict())

    print()
    print("=" * 78)
    env_tag = "LOCAL $0 (Parallels)" if is_parallels else "CLOUD $ (WAA/Azure)"
    print(f"  COST ESTIMATE  ({env_tag}, mode={args.mode}, model={MODELS[args.model].name})")
    print("=" * 78)
    for r in rows:
        print(f"\n  {r['num_tasks']} tasks:")
        print(f"    {vm_label}  {r['vm_hours']:.2f} vm-hours @ ${r['vm_hourly_usd']}/hr"
              f"  = ${r['vm_cost_usd']:.2f}")
        print(f"    Agent token cost:      ${r['token_cost_usd']:.2f}"
              f"  (paid tasks={r['paid_tasks']}, {r['agent_steps_per_paid_task']} steps each)")
        print(f"    -> TOTAL:              ${r['total_cost_usd']:.2f}"
              f"   (${r['cost_per_task_usd']:.4f}/task)")
        print(f"    Pure-agent baseline:   ${r['baseline_pure_agent_cost_usd']:.2f}"
              f"   ({r['baseline_agent_steps_per_task']} steps/task, all paid)")
        print(f"    Savings vs baseline:   ${r['savings_vs_baseline_usd']:.2f}")

    print()
    print("  HARD GUARDRAILS (enforced on any --live paid run):")
    print(f"    per-run cap:      ${guard.per_run_usd:.2f}")
    print(f"    total cap:        ${guard.total_usd:.2f}")
    print(f"    per-task tokens:  {guard.per_task_tokens:,}")
    print(f"    billing-abort:    after {guard.billing_abort_after} consecutive errors")

    requested = rows[0]
    if requested["total_cost_usd"] > guard.total_usd:
        print()
        print(f"  WARNING: estimated total ${requested['total_cost_usd']:.2f} exceeds the "
              f"total cap ${guard.total_usd:.2f}. Raise --max-total-usd or reduce --tasks "
              f"before a --live run.")
    return {"estimates": rows, "guardrails": guard.__dict__}


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Evaluate openadapt-flow on WAA (dry-run by default).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--env", choices=["waa", "parallels"], default="waa",
                   help="Environment: 'waa' = CLOUD Azure ($); 'parallels' = "
                        "LOCAL Apple-Silicon VM ($0, opt-in OPENADAPT_PARALLELS=1).")
    p.add_argument("--mode", choices=["replay", "hybrid"], default="replay")
    p.add_argument("--tasks", type=int, default=10,
                   help="Number of tasks (used for the estimate when --task-ids is absent).")
    p.add_argument("--task-ids", type=str, default=None,
                   help="Comma-separated WAA task IDs to run.")
    p.add_argument("--bundles", type=str, default=None,
                   help="Directory of compiled bundles, one per task_id subdir.")
    p.add_argument("--server-url", type=str, default="http://localhost:5001")
    p.add_argument("--model", choices=sorted(MODELS), default=DEFAULT_MODEL,
                   help="Fallback / baseline computer-use model for cost.")
    p.add_argument("--fallback-rate", type=float, default=0.30,
                   help="Assumed fraction of tasks that halt -> paid fallback (hybrid).")
    p.add_argument("--vm-hourly", type=float, default=0.19,
                   help="VM $/hour (Azure D4_v3=0.19, AWS m8i.2xlarge=0.46).")
    # Guardrails (mandatory for any paid run).
    p.add_argument("--max-run-usd", type=float, default=0.50)
    p.add_argument("--max-total-usd", type=float, default=5.00)
    p.add_argument("--max-task-tokens", type=int, default=60_000)
    p.add_argument("--billing-abort-after", type=int, default=2)
    # Execution.
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan + cost estimate and exit (default when --live absent).")
    p.add_argument("--live", action="store_true",
                   help="Actually run against a reachable WAA server (GATED; needs Azure + $).")
    p.add_argument("--run-root", type=str, default="flow_waa_results")
    p.add_argument("--json", action="store_true", help="Also emit the plan+estimate as JSON.")
    args = p.parse_args(argv)

    guard = build_guard(args)
    plan = build_plan(args)

    is_parallels = args.env == "parallels"
    print()
    print("=" * 78)
    env_label = "Parallels LOCAL VM ($0)" if is_parallels else "WAA / Azure CLOUD ($)"
    print(f"  openadapt-flow on {env_label}  --  mode={args.mode}")
    print("=" * 78)
    print(f"  environment:         {args.env}")
    print(f"  tasks:               {plan['num_tasks']}")
    print(f"  server-url:          {plan['server_url']}")
    print(f"  bundles-dir:         {plan['bundles_dir']}")
    print(f"  tasks with bundle:   {plan['tasks_with_bundle']}")
    print(f"  tasks MISSING bundle:{plan['tasks_missing_bundle']}"
          + ("  (record + compile first)" if plan["tasks_missing_bundle"] else ""))
    if is_parallels:
        from openadapt_evals.flow.parallels_env import PARALLELS_ENV_VAR, parallels_enabled

        print(f"  opt-in gate:         {PARALLELS_ENV_VAR}="
              f"{'set' if parallels_enabled() else 'UNSET (skipped)'}")
        print("  snapshot-safe:       snapshot -> run -> revert (never deletes the VM)")

    estimate = print_estimate(args, guard)

    if args.json:
        print()
        print(json.dumps({"plan": plan, **estimate}, indent=2, default=str))

    if not args.live:
        print()
        if is_parallels:
            print("  DRY-RUN: no VM touched, no money spent.")
            print("  A local run needs: Parallels + the Win11 VM + openadapt-flow"
                  " (win_agent, PR #95),")
            print("  compiled bundles per task, OPENADAPT_PARALLELS=1, then --live. $0.")
        else:
            print("  DRY-RUN: no VM provisioned, no task run, no money spent.")
            print("  A live run needs: Azure creds + a revived waa-pool VM"
                  " (oa-vm pool-create/pool-wait),")
            print("  compiled bundles for each task, then re-run with --live.")
        return 0

    return _run_live(args, plan, guard)


def _run_live(args: argparse.Namespace, plan: dict, guard: CostGuardConfig) -> int:
    """Execute a real run. GATED: requires a reachable WAA server + bundles.

    This path does NOT create or start any Azure/AWS resource -- it assumes a
    WAA server is already reachable at ``--server-url`` (revive the pool with
    ``oa-vm pool-create`` / ``oa-vm pool-wait`` first). Kept minimal on purpose;
    the maintainer's explicit go is required before running it.
    """
    import requests  # local import: not needed for dry-run

    if plan["tasks_missing_bundle"]:
        print("\n  REFUSING --live: some tasks have no compiled bundle.", file=sys.stderr)
        return 2

    if args.env == "parallels":
        return _run_live_parallels(args, plan, guard)

    print(f"\n  Probing WAA server at {args.server_url} ...")
    try:
        resp = requests.get(f"{args.server_url}/probe", timeout=5)
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"  REFUSING --live: WAA server not reachable: {e}", file=sys.stderr)
        return 2

    ledger = SpendLedger(guard)
    run_root = Path(args.run_root)

    if args.mode == "replay":
        from openadapt_evals.flow.replay_runner import (
            FlowTask,
            aggregate_replay_metrics,
            run_demonstrate_then_replay,
        )

        tasks = [
            FlowTask(task_id=t["task_id"], bundle_dir=Path(t["bundle_dir"]))
            for t in plan["tasks"]
        ]
        # NOTE: wire a real WAA evaluator here (WAALiveAdapter.evaluate) once the
        # maintainer approves a live run; left None so replay is not falsely
        # self-scored.
        metrics = run_demonstrate_then_replay(tasks, args.server_url, run_root)
        summary = aggregate_replay_metrics(metrics)
        # Replay makes ~0 model calls, so the paid ledger should stay at $0;
        # report it for transparency (and to prove no silent spend).
        print(json.dumps({"summary": summary, "ledger": ledger.summary()}, indent=2))
        return 0

    print("\n  hybrid --live requires a base computer-use agent + WAALiveAdapter wiring;"
          "\n  construct HybridFlowAgent(base_agent, bundle, server_url, ledger) and drive it"
          "\n  with evaluate_agent_on_benchmark(agent, WAALiveAdapter(...)). Gated on go.",
          file=sys.stderr)
    return 3


def _run_live_parallels(args: argparse.Namespace, plan: dict, guard: CostGuardConfig) -> int:
    """Execute a LOCAL Parallels run ($0). GATED: opt-in + snapshot-safe.

    Requires ``OPENADAPT_PARALLELS=1``. Snapshots the clean VM state, launches
    the in-guest win_agent, replays each bundle via the SAME WindowsBackend path
    as WAA, scores with OUR built-in verifiers, then reverts (never deletes the
    VM). No cloud, no money. Only the (optional) hybrid agent-fallback tokens
    cost anything, and those stay under the same guardrails.
    """
    from openadapt_evals.flow.parallels_env import (
        ParallelsConfig,
        ParallelsTask,
        parallels_enabled,
        run_parallels_replay,
    )

    if not parallels_enabled():
        print("\n  REFUSING --live (parallels): opt-in gate is off. "
              "Set OPENADAPT_PARALLELS=1.", file=sys.stderr)
        return 2

    if args.mode != "replay":
        print("\n  Parallels --live currently supports the replay mode "
              "(the compiled arm); hybrid fallback needs a per-step local adapter.",
              file=sys.stderr)
        return 3

    config = ParallelsConfig(enabled=True)
    tasks = [
        ParallelsTask(
            task_id=t["task_id"],
            instruction=t["task_id"],
            verify=lambda vm: (False, "supply a verifier for custom parallels tasks"),
            bundle_dir=Path(t["bundle_dir"]),
        )
        for t in plan["tasks"]
    ]
    metrics, summary = run_parallels_replay(tasks, config, run_root=Path(args.run_root))
    print(json.dumps({"env": "parallels", "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
