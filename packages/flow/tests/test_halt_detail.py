"""The local halt detail must be actionable AND unable to carry PHI.

Two invariants are pinned here, and they pull against each other on purpose:

1. an operator reading only the phone can tell WHAT broke and what "I fixed it"
   will make the engine re-check;
2. nothing on that surface can carry protected content, and the signed
   Cloud-safe task is byte-for-byte what it was before the enrichment.

The second is the reason the first is not solved by shipping the step intent:
``click 'Open'`` is harmless, but a TYPE step's intent embeds the typed value.
"""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_flow.console import human_decisions
from openadapt_flow.console.attention import attention_item
from openadapt_flow.console.halt_detail import RUNG_ORDER
from openadapt_flow.console.human_decisions import RemoteDecisionProjection
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.runtime import resolver
from openadapt_flow.runtime.durable.attended import AttendedActionStore
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision

#: A value a real deployment would consider PHI. It is a workflow parameter,
#: so it reaches the run's bindings, the step intent, and the durable pause --
#: every place a naive "just include the intent" enrichment would read from.
PROTECTED_VALUE = "Marta Quilligan 1974-03-08 MRN 40182"


def _anchor(*, template: str, role: str, name: str, ocr_text: str) -> Anchor:
    return Anchor(
        template=template,
        structural=StructuralLocator(selector="#target", role=role, name=name),
        region=(10, 10, 40, 20),
        click_point=(30, 20),
        ocr_text=ocr_text,
        landmarks=[],
    )


def _run(tmp_path: Path, workflow: Workflow, *, name: str, params=None):
    """Drive a real durable run until the resolution ladder halts it.

    ``FakeVision`` matches no template and finds no text, so every visual rung
    genuinely runs and genuinely fails -- the same shape as the founder's live
    halt, not a hand-written pause fixture.
    """
    bundle = tmp_path / "bundles" / name
    run = tmp_path / "runs" / name
    workflow.save(bundle)
    (bundle / "templates").mkdir(parents=True, exist_ok=True)
    for step in workflow.steps:
        if step.anchor is not None:
            (bundle / step.anchor.template).write_bytes(b"crop")
    report = Replayer(
        FakeBackend(),
        vision=FakeVision(),
        durable=True,
        poll_interval_s=0.0,
    ).run(workflow, bundle_dir=bundle, run_dir=run, params=params or {})
    assert report.success is False
    item = attention_item(run.parent, run)
    assert item is not None
    return run, item


def _detail(run: Path, item):
    return human_decisions.decision_detail(run, item)


def test_rung_vocabulary_is_the_engine_ladder_not_a_parallel_one():
    """A second rung vocabulary would silently drift from the real ladder."""
    assert RUNG_ORDER == resolver.RUNG_ORDER


def test_resolution_halt_says_what_broke_and_what_continue_will_recheck(tmp_path):
    workflow = Workflow(
        name="halt-detail-click",
        steps=[
            Step(
                id="step_000",
                intent="click 'Open'",
                action=ActionKind.CLICK,
                anchor=_anchor(
                    template="templates/step_000.png",
                    role="button",
                    name="Open",
                    ocr_text="Open",
                ),
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="ENCOUNTER",
                        timeout_s=0.01,
                    )
                ],
            ),
            Step(id="step_001", intent="confirm", action=ActionKind.KEY, key="B"),
        ],
    )
    run, item = _run(tmp_path, workflow, name="click")
    halt = _detail(run, item)["presentation"]["halt"]

    # WHAT broke: the engine's own typed category, the step's position, the
    # action it was performing, and the target it could not find.
    assert halt["category"] == "resolution"
    assert (halt["step_ordinal"], halt["step_count"]) == (1, 2)
    assert halt["action_kind"] == "click"
    assert halt["target_role"] == "button"
    assert halt["target_label"] == "Open"
    assert halt["target_label_withheld"] is False

    # WHICH rungs were tried. Every rung whose evidence the bundle carries ran
    # and failed; a rung with no recorded evidence is reported as never tried,
    # not as a failure.
    ladder = {row["rung"]: row for row in halt["resolution_ladder"]}
    assert list(ladder) == list(RUNG_ORDER)
    assert ladder["template"]["evidence"] == "recorded"
    assert ladder["template"]["verdict"] == "failed"
    assert ladder["ocr"]["verdict"] == "failed"
    assert ladder["geometry"]["evidence"] == "absent"
    assert ladder["geometry"]["verdict"] == "not_attempted"

    # WHAT "I fixed it" buys: the engine re-resolves the target and re-proves
    # the step's declared contracts before anything continues.
    checks = {row["check"]: row["count"] for row in halt["will_recheck"]}
    assert checks["target_resolution"] is None
    assert checks["postconditions"] == 1


def test_a_typed_value_degrades_to_the_field_shape_and_never_appears(tmp_path):
    """The exact leak this projection must not become.

    The step types a protected value into a field whose accessible name is the
    record it belongs to. The detail may say "a type step on a text field" and
    must say nothing else about it.
    """
    workflow = Workflow(
        name="halt-detail-type",
        params={"patient": PROTECTED_VALUE},
        steps=[
            Step(
                id="step_000",
                intent=f"type '{PROTECTED_VALUE}'",
                action=ActionKind.TYPE,
                param="patient",
                anchor=_anchor(
                    template="templates/step_000.png",
                    role="textbox",
                    name=PROTECTED_VALUE,
                    ocr_text=PROTECTED_VALUE,
                ),
            ),
        ],
    )
    run, item = _run(
        tmp_path, workflow, name="type", params={"patient": PROTECTED_VALUE}
    )
    detail = _detail(run, item)
    halt = detail["presentation"]["halt"]

    assert halt["action_kind"] == "type"
    assert halt["target_role"] == "textbox"  # the shape survives
    assert halt["target_label"] is None  # the content does not
    assert halt["target_label_withheld"] is True

    serialized = json.dumps(detail)
    for protected in (
        PROTECTED_VALUE,
        "Marta",
        "40182",
        f"type '{PROTECTED_VALUE}'",
        str(run),
        str(run.parent.parent / "bundles"),
    ):
        assert protected not in serialized


def test_a_record_row_label_is_withheld_even_when_it_is_the_click_target(tmp_path):
    """A patient-name row is a record, not control chrome, so it is withheld.

    The accessible name and the OCR of the target crop agree here -- agreement
    alone is not release. The role says this element is a row of record
    content, and that is disqualifying on its own.
    """
    workflow = Workflow(
        name="halt-detail-row",
        steps=[
            Step(
                id="step_000",
                intent="click the patient row",
                action=ActionKind.CLICK,
                anchor=_anchor(
                    template="templates/step_000.png",
                    role="row",
                    name=PROTECTED_VALUE,
                    ocr_text=PROTECTED_VALUE,
                ),
            ),
        ],
    )
    run, item = _run(tmp_path, workflow, name="row")
    detail = _detail(run, item)
    halt = detail["presentation"]["halt"]

    assert halt["target_role"] == "row"
    assert halt["target_label"] is None
    assert halt["target_label_withheld"] is True
    assert PROTECTED_VALUE not in json.dumps(detail)


def test_the_local_detail_carries_only_closed_values(tmp_path):
    """No free-text field: every leaf is an enum, a bounded int, or a bool.

    A free-text explanation is exactly how protected content escapes a closed
    contract, so the *shape* is asserted, not a list of forbidden strings.
    """
    workflow = Workflow(
        name="halt-detail-shape",
        steps=[
            Step(
                id="step_000",
                intent="click 'Open'",
                action=ActionKind.CLICK,
                anchor=_anchor(
                    template="templates/step_000.png",
                    role="button",
                    name="Open",
                    ocr_text="Open",
                ),
            ),
        ],
    )
    run, item = _run(tmp_path, workflow, name="shape")
    halt = _detail(run, item)["presentation"]["halt"]

    categories = {
        "effect_refuted",
        "effect_indeterminate",
        "effect_escalated",
        "placeholder_effect",
        "effect_unverifiable",
        "unmet_guard",
        "disambiguation",
        "identity",
        "postcondition",
        "resolution",
        "human_required",
        "halt",
        "operator_review",
    }
    assert halt["category"] in categories
    assert halt["action_kind"] in {kind.value for kind in ActionKind}
    assert isinstance(halt["step_ordinal"], int)
    for row in halt["resolution_ladder"]:
        assert set(row) == {"rung", "evidence", "verdict"}
        assert row["rung"] in RUNG_ORDER
        assert row["evidence"] in {"recorded", "absent", "unknown"}
        assert row["verdict"] in {
            "resolved",
            "failed",
            "not_attempted",
            "not_reached",
            "unavailable",
            "unknown",
        }
    for row in halt["will_recheck"]:
        assert set(row) == {"check", "count"}
        assert row["check"] in {
            "target_resolution",
            "record_identity",
            "postconditions",
            "system_of_record_effects",
            "delivery_reconciliation",
        }
        assert row["count"] is None or isinstance(row["count"], int)


def test_the_cloud_safe_task_is_unchanged_by_the_local_enrichment(tmp_path):
    """The signed envelope keeps null safe slots and local-only evidence.

    The enrichment lives beside the task, not inside it: the task's own fields
    are asserted here, and the halt detail is proven absent from the projection
    a remote surface receives.
    """
    workflow = Workflow(
        name="halt-detail-task",
        params={"patient": PROTECTED_VALUE},
        steps=[
            Step(
                id="step_000",
                intent=f"type '{PROTECTED_VALUE}'",
                action=ActionKind.TYPE,
                param="patient",
                anchor=_anchor(
                    template="templates/step_000.png",
                    role="textbox",
                    name=PROTECTED_VALUE,
                    ocr_text=PROTECTED_VALUE,
                ),
            ),
        ],
    )
    run, item = _run(
        tmp_path, workflow, name="task", params={"patient": PROTECTED_VALUE}
    )
    detail = _detail(run, item)
    task = detail["task"]
    capability = AttendedActionStore(run).read()

    assert task["schema_version"] == "openadapt.human-decision-task/v1"
    assert task["capability_digest"] == capability.digest
    assert set(task["question"]["safe_slots"]) == {
        "candidate_count",
        "required_signal_count",
        "confirmed_signal_count",
    }
    assert all(value is None for value in task["question"]["safe_slots"].values())
    assert task["evidence"]["sensitive_evidence_local_only"] is True
    # The enrichment is not in the signed envelope, under any name.
    assert "halt" not in task
    assert "resolution_ladder" not in json.dumps(task)
    assert PROTECTED_VALUE not in json.dumps(task)
    # ...and it is structurally unreachable from the remote lane: the relayed
    # projection has no field the local presentation could ride in.
    assert set(RemoteDecisionProjection.model_fields) == {
        "schema_version",
        "task",
        "task_digest",
        "phase",
        "event_sequence",
        "expected_transition_digest",
        "idempotency_scope_digest",
        "binding_digest",
    }
