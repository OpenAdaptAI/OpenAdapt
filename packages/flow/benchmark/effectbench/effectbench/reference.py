"""The reference result -- the regression anchor for the whole benchmark.

Runs the two shipped baselines (:class:`~effectbench.adapter.ScreenOnlySUT` and
:class:`~effectbench.adapter.EffectVerifiedSUT`) live against the synthetic
MockMed anchor and reproduces the published headline:

- screen-only SWER ``50/90 = 55.6%`` (wrong-write 40, phantom 10);
- effect-verified SWER ``0/90 = 0.0%``;
- 5 of the 7 transactional faults silently mishandled by screen-only verification.

If a change to the schema, the classifier, the judge, or the metrics moves these
numbers, the pinned test ``tests/test_reference.py`` fails. The same numbers are
produced by the OpenAdapt engine's in-tree re-expression, so the standalone port
is verifiable against the reference implementation.
"""

from __future__ import annotations

from typing import Any

from effectbench.adapter import EffectVerifiedSUT, ScreenOnlySUT
from effectbench.metrics import summarize
from effectbench.runner import evaluate
from effectbench.schema import EpisodeRecord
from effectbench.tasks.mockmed import MOCKMED_TASKS, TRANSACTIONAL_MODES

#: Trials per task in the reference run (9 tasks x 10 = 90 episodes per arm).
REFERENCE_TRIALS = 10


def build_reference_episodes(trials: int = REFERENCE_TRIALS) -> list[EpisodeRecord]:
    """Score both baselines against the MockMed anchor, ``trials`` per task."""
    episodes: list[EpisodeRecord] = []
    for sut in (ScreenOnlySUT(), EffectVerifiedSUT()):
        episodes.extend(evaluate(sut, trials=trials))
    return episodes


def reference_result(trials: int = REFERENCE_TRIALS) -> dict[str, Any]:
    """A machine-readable reference-result document (headline + per-arm summary)."""
    episodes = build_reference_episodes(trials)
    screen = summarize(episodes, arm="screen_only")
    effect = summarize(episodes, arm="effect_verified")

    by_key: dict[tuple[str, str], EpisodeRecord] = {}
    for e in episodes:
        by_key[(e.arm, e.task_id)] = e
    silent_transactional = sum(
        1
        for mode in TRANSACTIONAL_MODES
        if by_key[("screen_only", f"mockmed::{mode}")].is_silent_wrong
    )

    return {
        "suite": "mockmed-anchor",
        "trials_per_task": trials,
        "n_tasks": len(MOCKMED_TASKS),
        "arms": {
            "screen_only": {
                "swer": screen.swer.model_dump(),
                "swer_wrong_write": screen.swer_wrong_write.model_dump(),
                "swer_phantom": screen.swer_phantom.model_dump(),
                "over_halt": screen.over_halt.model_dump(),
                "task_success": screen.task_success.model_dump(),
                "screen_success": screen.screen_success.model_dump(),
                "success_effect_gap": screen.success_effect_gap,
                "outcome_counts": screen.outcome_counts,
            },
            "effect_verified": {
                "swer": effect.swer.model_dump(),
                "over_halt": effect.over_halt.model_dump(),
                "task_success": effect.task_success.model_dump(),
                "screen_success": effect.screen_success.model_dump(),
                "success_effect_gap": effect.success_effect_gap,
                "outcome_counts": effect.outcome_counts,
            },
        },
        "transactional_silently_mishandled": {
            "silent": silent_transactional,
            "total": len(TRANSACTIONAL_MODES),
        },
    }


def main() -> None:
    result = reference_result()
    screen = result["arms"]["screen_only"]
    effect = result["arms"]["effect_verified"]
    sw = screen["swer"]
    ev = effect["swer"]
    print("EffectBench reference result -- synthetic MockMed anchor\n")
    print(
        f"screen_only    SWER : {sw['numerator']}/{sw['denominator']} "
        f"= {sw['rate']:.1%}  (wrong-write "
        f"{screen['swer_wrong_write']['numerator']}, phantom "
        f"{screen['swer_phantom']['numerator']})"
    )
    print(
        f"effect_verified SWER: {ev['numerator']}/{ev['denominator']} "
        f"= {ev['rate']:.1%}"
    )
    print(
        f"effect_verified over-halt: {effect['over_halt']['numerator']}/"
        f"{effect['over_halt']['denominator']} "
        f"= {effect['over_halt']['rate']:.1%}  (the availability cost of SWER=0)"
    )
    tm = result["transactional_silently_mishandled"]
    print(
        f"transactional silently mishandled by screen-only: "
        f"{tm['silent']}/{tm['total']}"
    )


if __name__ == "__main__":
    main()
