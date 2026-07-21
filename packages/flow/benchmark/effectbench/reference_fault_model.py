"""Reference re-expression: the ``fault_model`` study through EffectBench.

This proves the new schema + oracle harness reproduce the KNOWN result of the
original ``benchmark/fault_model`` study — *5 of the 7 transactional fault
classes are silently mishandled by screen-only verification, and 0 by the
independent effect oracle* — and the ``benchmark/silent_wrong_action`` proto-SWER
rate (**55.6% -> 0.0%** over 90 runs). It is the regression anchor for the
whole benchmark contract: if a change to the schema, the classifier, or the
metrics moves these numbers, this study (and its pinned test
``tests/test_effectbench_reference.py``) fails.

How it re-expresses the study, faithfully and without hardcoding verdicts:

- Each of the study's nine scenarios (``benchmark.fault_model.faults.FAULTS``)
  becomes a :class:`~openadapt_flow.benchmark.effectbench.schema.TaskSpec` plus
  the KNOWN post-action system-of-record state that fault produces (the same DB
  states the study's own taxonomy tests assert). The states live in
  :data:`SCENARIOS` — the record shapes are MockMed ``FaultDB`` rows.
- The TRUE effect is read by the substrate-agnostic
  :class:`~openadapt_flow.benchmark.effectbench.oracle.RecordSnapshotOracle`
  running the SAME shared ``judge_records`` decision logic the runtime uses —
  once for the at-most-once/collateral ``record_written`` contract and once for
  the ``field_equals`` note read-back — reduced by
  :func:`~openadapt_flow.benchmark.effectbench.oracle.combine_true_states`.
- Two arms are scored against the identical independent oracle: ``screen_only``
  (the weak banner oracle — reports success whenever the screen looks saved) and
  ``effect_verify`` (the agent halts unless the oracle CONFIRMS). Only the
  agent's ``reported_success`` differs between arms; the oracle is constant.

Run ``python -m benchmark.effectbench.reference_fault_model`` to print the
matrix and the SWER for each arm.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from benchmark.fault_model import faults as F
from openadapt_flow.benchmark.effectbench import (
    AgentReport,
    DivergenceCategory,
    Effect,
    EffectKind,
    EpisodeRecord,
    OracleSpec,
    RecordSnapshotOracle,
    Substrate,
    TaskSpec,
    classify_outcome,
    combine_true_states,
    effect_state,
    oracle_verdict_of,
    summarize,
)
from openadapt_flow.benchmark.effectbench.schema import OracleChannel

# The consequential-save contract the study verifies: exactly one Triage
# encounter for the target patient (at-most-once + no collateral loss) whose
# note equals the run's note. Two typed effects, one contract.
RECORD_EFFECT = Effect(
    kind=EffectKind.RECORD_WRITTEN,
    match={"patient_id": F.TARGET_PATIENT, "type": F.TARGET_TYPE},
    expected_count=1,
    forbid_collateral_loss=True,
    timeout_s=0.0,
)
NOTE_EFFECT = Effect(
    kind=EffectKind.FIELD_EQUALS,
    match={"patient_id": F.TARGET_PATIENT, "type": F.TARGET_TYPE},
    field="note",
    value=F.NOTE_TEXT,
    timeout_s=0.0,
)

# Map each fault to the divergence category the design doc assigns it.
_CATEGORY = {
    "ok": DivergenceCategory.CONTROL,
    "partial": DivergenceCategory.C1_PARTIAL_SAVE,
    "duplicate": DivergenceCategory.C2_DUPLICATE_SUBMISSION,
    "timeout": DivergenceCategory.C3_OPTIMISTIC_THEN_REJECT,
    "optimistic": DivergenceCategory.C3_OPTIMISTIC_THEN_REJECT,
    "session": DivergenceCategory.C3_OPTIMISTIC_THEN_REJECT,
    "stale": DivergenceCategory.C4_STALE_OVERWRITE,
    "double": DivergenceCategory.C5_DOUBLE_DELIVERED_INPUT,
    "idempotent": DivergenceCategory.CONTROL,
}


def _row(id_: int, *, note: str = F.NOTE_TEXT, source: str = "replay",
         type_: str = F.TARGET_TYPE, patient: str = F.TARGET_PATIENT) -> dict:
    return {
        "id": id_,
        "patient_id": patient,
        "type": type_,
        "note": note,
        "source": source,
        "key": None,
    }


# A concurrent clinician's row (a DIFFERENT encounter type, so it does NOT match
# the target selector — its disappearance is collateral loss, not our write).
_CONCURRENT = _row(9, type_="Urgent", source="other")


@dataclass(frozen=True)
class Scenario:
    """One fault scenario's KNOWN pre/post system-of-record state + screen verdict.

    ``pre`` / ``post`` are the MockMed ``FaultDB`` record lists the study's own
    taxonomy tests assert for this fault; ``screen_success`` is the documented
    weak-oracle "saved banner" verdict; ``correct_action_available`` is False for
    every fault (the fault makes a correct effect unattainable) and True for the
    clean/idempotent controls.
    """

    mode: str
    pre: list[dict]
    post: list[dict]
    screen_success: bool
    correct_action_available: bool


# The nine scenarios, states straight from the fault_model study (and its
# tests/e2e/test_fault_model.py taxonomy fixtures).
SCENARIOS: tuple[Scenario, ...] = (
    Scenario("ok", [], [_row(1)], True, True),
    Scenario("partial", [], [_row(1, note="")], True, False),
    Scenario("duplicate", [], [_row(1), _row(2)], True, False),
    Scenario("timeout", [], [_row(1)], False, True),
    Scenario("optimistic", [], [], True, False),
    Scenario("session", [], [], False, False),
    Scenario("stale", [_CONCURRENT], [_row(1)], True, False),
    Scenario("double", [], [_row(1), _row(2)], True, False),
    Scenario("idempotent", [], [_row(1)], True, True),
)


def _task_spec(scenario: Scenario) -> TaskSpec:
    fault = F.FAULTS_BY_MODE[scenario.mode]
    return TaskSpec(
        task_id=f"fault_model::{scenario.mode}",
        title=fault.title,
        substrate=Substrate.WEB,
        category=_CATEGORY[scenario.mode],
        goal=(
            "Record a Triage encounter for the referred patient with the "
            "clinical note, then save it."
        ),
        expected_effect=RECORD_EFFECT,
        oracle=OracleSpec(
            channel=OracleChannel.REST,
            description=(
                "MockMed persistence-boundary readback (GET /api/db); "
                "record_written at-most-once + note field read-back."
            ),
            isolated_from_agent=True,
            trial_unique_payload=False,  # study uses a fixed note; a real task randomizes
            refusal_controls=False,
        ),
        notes=fault.headline,
    )


def _true_state(scenario: Scenario):
    """Read the TRUE effect for a scenario through the record-snapshot oracle."""
    # A single oracle whose read flips pre -> post once the action has "run".
    box = {"acted": False}

    def read() -> Optional[list[dict]]:
        return list(scenario.post if box["acted"] else scenario.pre)

    oracle = RecordSnapshotOracle(read, substrate="rest")
    before = oracle.capture_pre_state()
    box["acted"] = True
    record_verdict = oracle.verify(RECORD_EFFECT, before)
    note_verdict = oracle.verify(NOTE_EFFECT, before)
    combined = combine_true_states(
        effect_state(record_verdict), effect_state(note_verdict)
    )
    # The DECIDING verdict for the compound contract: the first sub-effect that
    # is not CONFIRMED (so a partial save is represented by the refuting note
    # read-back, not the record_written check that passed), else the (confirmed)
    # note read-back. Its effect_state equals ``combined`` by construction.
    deciding = record_verdict if not record_verdict.confirmed else note_verdict
    return combined, deciding, before


def build_reference_episodes(repeats: int = 10) -> list[EpisodeRecord]:
    """Score every scenario under both arms, ``repeats`` deterministic trials.

    ``screen_only`` reports the documented banner verdict; ``effect_verify``
    halts unless the independent oracle CONFIRMS the combined contract. The
    oracle (and hence ``true_state``) is identical across arms.
    """
    episodes: list[EpisodeRecord] = []
    for scenario in SCENARIOS:
        spec = _task_spec(scenario)
        combined, record_verdict, before = _true_state(scenario)
        oracle_view = oracle_verdict_of(
            record_verdict, before_reachable=before.reachable
        )
        arms = {
            "screen_only": scenario.screen_success,
            "effect_verify": combined.name == "CORRECT",
        }
        for arm, reported_success in arms.items():
            label, variant, reason = classify_outcome(
                reported_success=reported_success,
                true_state=combined,
                correct_action_available=scenario.correct_action_available,
            )
            for trial in range(repeats):
                episodes.append(
                    EpisodeRecord(
                        episode_id=f"{arm}::{scenario.mode}::{trial}",
                        task_id=spec.task_id,
                        arm=arm,
                        trial=trial,
                        substrate=spec.substrate,
                        category=spec.category,
                        seed=trial,
                        expected_effect_hash=RECORD_EFFECT.contract_hash(),
                        correct_action_available=scenario.correct_action_available,
                        agent=AgentReport(
                            reported_success=reported_success,
                            halted=not reported_success,
                        ),
                        oracle=oracle_view,
                        outcome=label,
                        swer_variant=variant,
                        reason=reason,
                    )
                )
    return episodes


# Fault modes that are true transactional fault classes (exclude the clean and
# idempotent-fix controls) — the denominator for the "5 of 7" headline.
TRANSACTIONAL_MODES = tuple(
    s.mode for s in SCENARIOS if s.mode not in ("ok", "idempotent")
)


def main() -> None:
    episodes = build_reference_episodes(repeats=10)
    screen = summarize(episodes, arm="screen_only")
    effect = summarize(episodes, arm="effect_verify")

    print("EffectBench re-expression of benchmark/fault_model\n")
    print(f"{'scenario':12s} {'true effect':16s} "
          f"{'screen_only':22s} {'effect_verify':22s}")
    print("-" * 74)
    by_key: dict[tuple[str, str], EpisodeRecord] = {}
    for e in episodes:
        by_key[(e.arm, e.task_id)] = e
    for s in SCENARIOS:
        tid = f"fault_model::{s.mode}"
        so = by_key[("screen_only", tid)]
        ev = by_key[("effect_verify", tid)]
        true = (
            "correct" if so.oracle.effect_correct
            else "absent" if so.oracle.effect_absent
            else "wrong_persisted"
        )
        print(f"{s.mode:12s} {true:16s} "
              f"{so.outcome.value:22s} {ev.outcome.value:22s}")

    silent_transactional = sum(
        1
        for s in SCENARIOS
        if s.mode in TRANSACTIONAL_MODES
        and by_key[("screen_only", f"fault_model::{s.mode}")].is_silent_wrong
    )
    print(
        f"\nscreen_only SWER  : {screen.swer.numerator}/{screen.swer.denominator}"
        f" = {screen.swer.rate:.1%}  "
        f"(wrong-write {screen.swer_wrong_write.numerator}, "
        f"phantom {screen.swer_phantom.numerator})"
    )
    print(
        f"effect_verify SWER: {effect.swer.numerator}/{effect.swer.denominator}"
        f" = {effect.swer.rate:.1%}"
    )
    print(
        f"transactional silently mishandled by screen-only: "
        f"{silent_transactional}/{len(TRANSACTIONAL_MODES)}"
    )


if __name__ == "__main__":
    main()
