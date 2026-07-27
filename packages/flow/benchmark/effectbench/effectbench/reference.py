"""The reference fixture's pinned expected values -- the regression anchor.

Runs the two shipped baselines (:class:`~effectbench.adapter.ScreenOnlySUT` and
:class:`~effectbench.adapter.EffectVerifiedSUT`) live against the synthetic
MockMed anchor and reproduces its pinned expected values:

- screen-only SWER ``50/90 = 55.6%`` (wrong-write 40, phantom 10);
- effect-verified SWER ``0/90 = 0.0%``;
- 5 of the 7 transactional faults silently mishandled by screen-only verification.

If a change to the schema, the classifier, the judge, or the metrics moves these
numbers, the pinned test ``tests/test_reference.py`` fails. The same numbers are
produced by the OpenAdapt engine's in-tree re-expression, so the standalone port
is verifiable against the reference implementation.

These counts are NOT a measured or published result. MockMed is a deterministic,
hand-authored fixture, so they reproduce exactly on every run -- which is what
makes them a good regression anchor and equally why they carry no empirical
weight. OpenAdapt's MEASURED end-to-end silent-wrong-effect result is
``benchmark/effect_e2e/`` in the engine repository (real replayer, real HTTP
write to an on-disk SQLite system of record, ground truth read directly from
storage over every table discovered from ``sqlite_master``). Its measured ladder,
90 runs per arm, is screen-verify ``54/90 = 60.0%`` -> effect-verify with one
out-of-band REST record oracle ``9/90 = 10.0%`` -> effect-verify with the
complete SQL read path ``0/90 = 0.0%``. The middle rung is the number a real
deployment ships; all nine residual misses are the single ``collateral_unaudited``
class. See
https://github.com/OpenAdaptAI/openadapt-flow/blob/main/benchmark/effect_e2e/EFFECT_E2E.md
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
    """The fixture's pinned expected values as a document (counts + per-arm summary).

    A regression anchor over a deterministic synthetic fixture -- not a measured
    result. See the module docstring for where the measured end-to-end number is.
    """
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
    print(
        "EffectBench reference fixture -- synthetic MockMed anchor\n"
        "Pinned expected values (a regression anchor), NOT a measured result.\n"
    )
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
    print(
        "\nMeasured end-to-end result (real replayer, on-disk SQLite system of "
        "record):\n"
        "  screen-verify                          54/90 = 60.0%\n"
        "  effect-verify, one out-of-band oracle    9/90 = 10.0%  <- ships\n"
        "  effect-verify, complete SQL read path    0/90 =  0.0%\n"
        "  https://github.com/OpenAdaptAI/openadapt-flow/blob/main/"
        "benchmark/effect_e2e/EFFECT_E2E.md"
    )


if __name__ == "__main__":
    main()
