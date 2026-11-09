"""EffectBench command-line interface.

    python -m effectbench reference          # run the two baselines, print SWER
    python -m effectbench run --baseline screen_only --trials 10
    python -m effectbench manifest           # print the task-pack manifest (JSON)
    python -m effectbench submission --baseline effect_verified > sub.json
    python -m effectbench score sub.json     # verify a submission reproduces

A third party plugs in their own system by importing :func:`effectbench.evaluate`
and passing their :class:`~effectbench.adapter.SystemUnderTest`; this CLI drives
the shipped baselines and the submission tooling.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from effectbench import __version__, evaluate, summarize
from effectbench.adapter import EffectVerifiedSUT, ScreenOnlySUT
from effectbench.leaderboard import (
    build_submission,
    pack_manifest,
    score_submission,
)
from effectbench.reference import main as reference_main

_BASELINES = {"screen_only": ScreenOnlySUT, "effect_verified": EffectVerifiedSUT}


def _print_summary(arm: str, episodes) -> None:
    s = summarize(episodes, arm=arm)
    print(f"arm: {arm}  (n={s.n_episodes}, tasks={s.n_tasks})")
    print(
        f"  SWER          : {s.swer.numerator}/{s.swer.denominator} "
        f"= {s.swer.rate:.1%}  (wrong-write {s.swer_wrong_write.numerator}, "
        f"phantom {s.swer_phantom.numerator})"
    )
    print(
        f"  over-halt     : {s.over_halt.numerator}/{s.over_halt.denominator} "
        f"= {s.over_halt.rate:.1%}"
    )
    print(
        f"  task success  : {s.task_success.numerator}/{s.task_success.denominator} "
        f"= {s.task_success.rate:.1%}"
    )
    print(
        f"  screen success: {s.screen_success.numerator}/"
        f"{s.screen_success.denominator} = {s.screen_success.rate:.1%}"
    )
    print(f"  success-effect gap: {s.success_effect_gap:.1%}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="effectbench", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("reference", help="run both baselines and print the headline")

    p_run = sub.add_parser("run", help="run one baseline and print its SWER")
    p_run.add_argument("--baseline", choices=sorted(_BASELINES), required=True)
    p_run.add_argument("--trials", type=int, default=10)

    sub.add_parser("manifest", help="print the task-pack manifest as JSON")

    p_sub = sub.add_parser("submission", help="emit a reproducible submission JSON")
    p_sub.add_argument("--baseline", choices=sorted(_BASELINES), required=True)
    p_sub.add_argument("--trials", type=int, default=10)

    p_score = sub.add_parser("score", help="verify a submission reproduces its numbers")
    p_score.add_argument("submission", type=Path)

    args = parser.parse_args(argv)

    if args.command == "reference":
        reference_main()
        return 0

    if args.command == "run":
        sut = _BASELINES[args.baseline]()
        episodes = evaluate(sut, trials=args.trials)
        _print_summary(args.baseline, episodes)
        return 0

    if args.command == "manifest":
        print(json.dumps(pack_manifest(), indent=2))
        return 0

    if args.command == "submission":
        sut = _BASELINES[args.baseline]()
        episodes = evaluate(sut, trials=args.trials)
        doc = build_submission(
            system_name=args.baseline,
            episodes=episodes,
            trials=args.trials,
            description=f"EffectBench shipped baseline: {args.baseline}",
        )
        print(json.dumps(doc, indent=2))
        return 0

    if args.command == "score":
        submission = json.loads(args.submission.read_text(encoding="utf-8"))
        result = score_submission(submission)
        print(json.dumps({"ok": result["ok"], "errors": result["errors"]}, indent=2))
        return 0 if result["ok"] else 1

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
