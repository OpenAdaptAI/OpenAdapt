"""EffectBench runner CLI — the CI-fast dry-run over the MockMed reference pack.

``python -m openadapt_flow.benchmark.effectbench.runner`` drives the nine MockMed
reference tasks through the in-repo LIVE arms (compiler, screen-only ablation,
mock) over a real in-process HTTP fault server — no Docker, no paid API, no
spend — and prints the per-arm summary. It proves the pipeline end-to-end: the
ablation surfaces a non-zero Silent Wrong-Effect Rate (a green banner over a bad
record), while the compiler arm's effect gate drives SWER to zero.

Flags:

* ``--trials N``        trials per (task, arm) (default 3; the SWER rate is
                        trial-invariant, more trials just tighten CIs / feed pass^k).
* ``--arms a,b``        restrict to named arms (default: all live arms).
* ``--json PATH``       also write the raw episodes + per-arm summaries as JSON.
* ``--list-arms``       list live vs scaffolded arms and exit (no run).
* ``--include-scaffolded`` attempt scaffolded external baselines too — they
                        RAISE (never spend); only for a wired, funded run.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from openadapt_flow.benchmark.effectbench.metrics import BenchmarkSummary, summarize
from openadapt_flow.benchmark.effectbench.runner import LIVE_ARMS
from openadapt_flow.benchmark.effectbench.runner.arms import AgentArm
from openadapt_flow.benchmark.effectbench.runner.baselines import SCAFFOLDED_ARMS
from openadapt_flow.benchmark.effectbench.runner.harness import run_matrix
from openadapt_flow.benchmark.effectbench.runner.reference_tasks import (
    MockMedEnvProvider,
    reference_tasks,
)
from openadapt_flow.benchmark.effectbench.schema import EpisodeRecord


def _select_arms(names: Optional[str]) -> list[AgentArm]:
    if not names:
        return list(LIVE_ARMS)
    wanted = {n.strip() for n in names.split(",") if n.strip()}
    chosen = [a for a in LIVE_ARMS if a.name in wanted]
    missing = wanted - {a.name for a in chosen}
    if missing:
        raise SystemExit(
            f"unknown arm(s): {sorted(missing)}; "
            f"available live arms: {[a.name for a in LIVE_ARMS]}"
        )
    return chosen


def _print_arms() -> None:
    print("Live arms (run in the dry-run, no spend):")
    for arm in LIVE_ARMS:
        print(f"  - {arm.name:12s} live")
    print("\nScaffolded external baselines (NOT wired; raise until funded):")
    for scaffold in SCAFFOLDED_ARMS:
        req = scaffold.requires
        print(
            f"  - {scaffold.name:12s} scaffolded  "
            f"provider={req.provider}  creds={list(req.credentials)}"
        )


def _fmt_pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def _print_summary(arm: str, s: BenchmarkSummary) -> None:
    print(f"\n=== arm: {arm}  (n={s.n_episodes} episodes, {s.n_tasks} tasks) ===")
    print(
        f"  SWER              : {s.swer.numerator}/{s.swer.denominator} = "
        f"{_fmt_pct(s.swer.rate)}  "
        f"(wrong_write {s.swer_wrong_write.numerator}, "
        f"phantom {s.swer_phantom.numerator})  "
        f"[95% CI {_fmt_pct(s.swer.ci.lo)}..{_fmt_pct(s.swer.ci.hi)}]"
    )
    print(
        f"  over-halt         : {s.over_halt.numerator}/{s.over_halt.denominator} "
        f"= {_fmt_pct(s.over_halt.rate)}"
    )
    print(
        f"  task success      : {s.task_success.numerator}/"
        f"{s.task_success.denominator} = {_fmt_pct(s.task_success.rate)}"
    )
    print(
        f"  screen success    : {s.screen_success.numerator}/"
        f"{s.screen_success.denominator} = {_fmt_pct(s.screen_success.rate)}"
    )
    print(
        f"  success-effect gap: {_fmt_pct(s.success_effect_gap)}  "
        f"[bootstrap 95% CI {_fmt_pct(s.success_effect_gap_ci.lo)}.."
        f"{_fmt_pct(s.success_effect_gap_ci.hi)}]"
    )
    print(
        f"  cost / latency    : ${s.total_cost_usd:.4f} total, "
        f"${s.mean_cost_usd:.4f}/ep, {s.mean_latency_s:.3f}s/ep"
    )
    print(f"  pass^k            : {s.pass_hat_k}")
    print(f"  outcome counts    : {s.outcome_counts}")


def run_dry_run(
    *, trials: int, arm_names: Optional[str], include_scaffolded: bool
) -> tuple[list[EpisodeRecord], dict[str, BenchmarkSummary]]:
    """Drive the MockMed reference pack and return (episodes, per-arm summaries)."""
    tasks = reference_tasks()
    arms: Sequence[AgentArm]
    if include_scaffolded:
        arms = list(_select_arms(arm_names)) + list(SCAFFOLDED_ARMS)
    else:
        arms = _select_arms(arm_names)
    with MockMedEnvProvider() as provider:
        episodes = run_matrix(
            tasks,
            arms,
            env_factory=provider.factory,
            trials=trials,
            include_scaffolded=include_scaffolded,
        )
    summaries = {
        arm: summarize(episodes, arm=arm) for arm in sorted({e.arm for e in episodes})
    }
    return episodes, summaries


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m openadapt_flow.benchmark.effectbench.runner",
        description="EffectBench multi-baseline runner — MockMed dry-run.",
    )
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--arms", type=str, default=None)
    parser.add_argument("--json", type=str, default=None)
    parser.add_argument("--list-arms", action="store_true")
    parser.add_argument("--include-scaffolded", action="store_true")
    args = parser.parse_args(argv)

    if args.list_arms:
        _print_arms()
        return 0

    episodes, summaries = run_dry_run(
        trials=args.trials,
        arm_names=args.arms,
        include_scaffolded=args.include_scaffolded,
    )

    print(
        "EffectBench dry-run over the MockMed reference pack "
        f"({len(episodes)} episodes; in-process HTTP, no Docker, no spend)"
    )
    for arm in sorted(summaries):
        _print_summary(arm, summaries[arm])

    # The thesis, made explicit for the reader.
    if "screen_only" in summaries and "compiler" in summaries:
        so = summaries["screen_only"].swer.rate
        co = summaries["compiler"].swer.rate
        print(
            f"\nThesis check: screen-only SWER {_fmt_pct(so)} vs "
            f"compiler SWER {_fmt_pct(co)} — "
            f"effect verification {'closes' if co < so else 'does NOT close'} "
            "the silent wrong-effect gap."
        )

    if args.json:
        payload = {
            "episodes": [e.model_dump(mode="json") for e in episodes],
            "summaries": {a: s.model_dump(mode="json") for a, s in summaries.items()},
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
