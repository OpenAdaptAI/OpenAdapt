"""Opt-in COMPILE-TIME model annotation (``compiler.annotate``).

A model PROPOSES richer labels, risk refinements, and typed-parameter
inferences at COMPILE time; the compiler applies them with a confirm-don't-trust
asymmetry (safe upgrades applied, weakenings / consequential changes FLAGGED).
The model call lives behind the ``StepAnnotator`` Protocol; every test here uses
the deterministic ``FakeStepAnnotator`` (or a stub client) -- ZERO network, ZERO
model calls, no API key. The runtime (replayer) must make ZERO model calls too;
the final test asserts exactly that against an annotated bundle.
"""

from __future__ import annotations

import json

import pytest

from openadapt_flow.compiler import compile_recording
from openadapt_flow.compiler.annotate import (
    AnthropicStepAnnotator,
    FakeStepAnnotator,
    LabelProposal,
    ParamProposal,
    RiskProposal,
    StepAnnotation,
    WorkflowProposals,
    apply_annotations,
)
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    ParamKind,
    ParamSpec,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.replayer import Replayer

# Reuse the compile-recording synthetic builder + drawing helpers.
from tests.test_compiler import (
    _write_recording,
    blank,
    draw_button,
    draw_text,
)

# Reuse the faked backend/vision + step builders (ZERO model, no Playwright).
from tests.test_replayer import FakeBackend, FakeVision, Match, click_step, make_png


# -- helpers -----------------------------------------------------------------


def _wf(*steps: Step, params=None, param_specs=None) -> Workflow:
    return Workflow(
        name="wf",
        params=params or {},
        param_specs=param_specs or {},
        steps=list(steps),
    )


def _click(step_id="step_000", *, risk="reversible", ocr="Open") -> Step:
    return Step(
        id=step_id,
        intent=f"click '{ocr}'",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template=f"templates/{step_id}.png",
            region=(10, 10, 50, 20),
            click_point=(30, 20),
            ocr_text=ocr,
        ),
        risk=risk,
    )


# -- proposals attach: richer labels, param types, risk proposals ------------


def test_label_proposal_attaches_as_advisory_intent():
    wf = _wf(_click("step_000", ocr="Open"))
    ann = FakeStepAnnotator(
        WorkflowProposals(
            steps=[
                StepAnnotation(
                    step_id="step_000",
                    label=LabelProposal(label="open the triage encounter"),
                )
            ]
        )
    )
    result = apply_annotations(wf, ann)
    assert result.labels["step_000"] == "open the triage encounter"
    assert any(a.kind == "label" for a in result.applied)
    # A label never changes replay behaviour, so nothing is flagged.
    assert result.clean is True
    # The step's own intent is untouched (label is advisory, in the sidecar).
    assert result.workflow.steps[0].intent == "click 'Open'"


@pytest.mark.parametrize("kind", [ParamKind.DATE, ParamKind.ENUM, ParamKind.ENTITY_REF])
def test_param_type_enrichment_applies_richer_than_phase1_string(kind):
    # Phase 1 types every recorded value as a bare string; the model enriches it.
    wf = _wf(
        Step(
            id="step_000",
            intent="type <dob>",
            action=ActionKind.TYPE,
            param="dob",
            text="1980-02-03",
        ),
        params={"dob": "1980-02-03"},
        param_specs={
            "dob": ParamSpec(name="dob", type=ParamKind.STRING, example="1980-02-03")
        },
    )
    choices = ["Triage", "Consult"] if kind is ParamKind.ENUM else []
    ann = FakeStepAnnotator(
        WorkflowProposals(
            steps=[
                StepAnnotation(
                    step_id="step_000",
                    params=[
                        ParamProposal(
                            name="dob", type=kind, choices=choices, consequential=False
                        )
                    ],
                )
            ]
        )
    )
    result = apply_annotations(wf, ann)
    assert result.workflow.param_specs["dob"].type is kind
    if kind is ParamKind.ENUM:
        assert result.workflow.param_specs["dob"].choices == choices
    # example/required carried over unchanged; only the TYPE was enriched.
    assert result.workflow.param_specs["dob"].example == "1980-02-03"
    assert any(a.kind == "param_type" for a in result.applied)
    assert result.clean is True


def test_risk_upgrade_the_keyword_heuristic_missed_applies():
    # "Commit charges" is write-shaped but not in the keyword list -> heuristic
    # leaves it reversible; the model upgrades it (safe direction) and it sticks.
    wf = _wf(_click("step_000", risk="reversible", ocr="Commit charges"))
    ann = FakeStepAnnotator(
        WorkflowProposals(
            steps=[
                StepAnnotation(
                    step_id="step_000",
                    risk=RiskProposal(
                        proposed_risk="irreversible",
                        rationale="commits a billing write",
                    ),
                )
            ]
        )
    )
    result = apply_annotations(wf, ann)
    assert result.workflow.steps[0].risk == "irreversible"
    assert any(a.kind == "risk_upgrade" for a in result.applied)
    assert result.clean is True  # arming a safeguard needs no confirmation


# -- confirm-don't-trust: downgrade / consequential are FLAGGED, not applied --


def test_risk_downgrade_is_flagged_not_applied():
    wf = _wf(_click("step_000", risk="irreversible", ocr="Delete"))
    ann = FakeStepAnnotator(
        WorkflowProposals(
            steps=[
                StepAnnotation(
                    step_id="step_000",
                    risk=RiskProposal(
                        proposed_risk="reversible",
                        rationale="thinks it is a soft delete",
                    ),
                )
            ]
        )
    )
    result = apply_annotations(wf, ann)
    # The safeguard is NEVER silently weakened.
    assert result.workflow.steps[0].risk == "irreversible"
    assert not any(a.kind == "risk_upgrade" for a in result.applied)
    flags = [f for f in result.flagged if f.kind == "risk_downgrade"]
    assert len(flags) == 1
    assert flags[0].needs_operator_confirmation is True
    assert result.clean is False


def test_consequential_param_is_flagged_not_applied():
    # Turning a demonstrated constant into a run-varying parameter changes what
    # the workflow DOES -> flagged, never applied.
    wf = _wf(
        Step(
            id="step_000",
            intent="type 'Acme Corp'",
            action=ActionKind.TYPE,
            text="Acme Corp",
        )
    )
    ann = FakeStepAnnotator(
        WorkflowProposals(
            steps=[
                StepAnnotation(
                    step_id="step_000",
                    params=[
                        ParamProposal(
                            name="company",
                            type=ParamKind.ENTITY_REF,
                            example="Acme Corp",
                            consequential=True,
                        )
                    ],
                )
            ]
        )
    )
    result = apply_annotations(wf, ann)
    assert "company" not in result.workflow.param_specs
    assert result.workflow.params == {}
    flags = [f for f in result.flagged if f.kind == "consequential_param"]
    assert len(flags) == 1
    assert flags[0].needs_operator_confirmation is True
    assert result.clean is False


def test_param_naming_a_non_parameter_value_is_flagged_not_applied():
    # Even non-`consequential` flagged, a proposal for a value that is NOT an
    # already-declared parameter cannot be a safe type-enrichment -> flag.
    wf = _wf(Step(id="step_000", intent="type 'x'", action=ActionKind.TYPE, text="x"))
    ann = FakeStepAnnotator(
        WorkflowProposals(
            steps=[
                StepAnnotation(
                    step_id="step_000",
                    params=[
                        ParamProposal(
                            name="new_param", type=ParamKind.DATE, consequential=False
                        )
                    ],
                )
            ]
        )
    )
    result = apply_annotations(wf, ann)
    assert "new_param" not in result.workflow.param_specs
    assert any(f.kind == "consequential_param" for f in result.flagged)


# -- purity / robustness -----------------------------------------------------


def test_original_workflow_is_never_mutated():
    wf = _wf(_click("step_000", risk="reversible", ocr="Save"))
    ann = FakeStepAnnotator(
        WorkflowProposals(
            steps=[
                StepAnnotation(
                    step_id="step_000",
                    risk=RiskProposal(proposed_risk="irreversible"),
                )
            ]
        )
    )
    apply_annotations(wf, ann)
    # apply_annotations deep-copies; the caller's workflow is untouched.
    assert wf.steps[0].risk == "reversible"


def test_unknown_step_id_is_ignored():
    wf = _wf(_click("step_000"))
    ann = FakeStepAnnotator(
        WorkflowProposals(
            steps=[
                StepAnnotation(
                    step_id="does_not_exist",
                    risk=RiskProposal(proposed_risk="irreversible"),
                )
            ]
        )
    )
    result = apply_annotations(wf, ann)
    assert result.applied == []
    assert result.flagged == []


def test_empty_annotator_leaves_workflow_unchanged():
    wf = _wf(_click("step_000", risk="reversible"))
    result = apply_annotations(wf, FakeStepAnnotator())
    assert result.workflow.model_dump() == wf.model_dump()
    assert result.clean is True


# -- compile.py opt-in hook --------------------------------------------------


def _annotatable_recording(tmp_path):
    """A 2-step recording: a benign nav click + a write-shaped 'Save' click."""
    before = blank()
    draw_button(before, 40, 40, 120, 40, "Home")
    draw_button(before, 560, 400, 160, 48, "Save")
    mid = before.copy()
    draw_text(mid, 120, 244, "Patient opened")
    after = mid.copy()
    draw_text(after, 380, 560, "Saved")
    events = [
        {"i": 0, "kind": "click", "x": 100, "y": 60, "t": 1.0},
        {"i": 1, "kind": "click", "x": 640, "y": 424, "t": 2.0},
    ]
    recording = tmp_path / "rec"
    _write_recording(recording, events, {0: (before, mid), 1: (mid, after)})
    return recording


def test_default_off_is_byte_identical_and_writes_no_sidecar(tmp_path):
    recording = _annotatable_recording(tmp_path)
    b_off = tmp_path / "b_off"
    b_default = tmp_path / "b_default"
    # annotate defaults to False; passing it explicitly must be identical.
    off = compile_recording(recording, b_off, name="wf", annotate=False)
    default = compile_recording(recording, b_default, name="wf")
    assert not (b_off / "annotations.json").exists()
    assert not (b_default / "annotations.json").exists()
    # Byte-identical modulo the always-varying created_at timestamp: annotate
    # off changes nothing about compilation (heuristic-only, no model).
    a = off.model_dump()
    b = default.model_dump()
    # created_at and the schema-v2 manifest (per-save provenance timestamp +
    # content digest that hashes created_at) always vary between two compiles;
    # they are integrity/provenance metadata, not compiled semantics.
    a.pop("created_at"), b.pop("created_at")
    a.pop("manifest", None), b.pop("manifest", None)
    assert a == b


def test_annotate_on_attaches_proposals_and_applies_safe_upgrade(tmp_path):
    recording = _annotatable_recording(tmp_path)
    bundle = tmp_path / "bundle"
    # A fake that (a) proposes a richer label for step_000, (b) upgrades the
    # nav click to irreversible (safe), (c) proposes a downgrade for the Save
    # click (flagged, not applied).
    fake = FakeStepAnnotator(
        WorkflowProposals(
            steps=[
                StepAnnotation(
                    step_id="step_000",
                    label=LabelProposal(label="open the patient chart"),
                    risk=RiskProposal(proposed_risk="irreversible"),
                ),
                StepAnnotation(
                    step_id="step_001", risk=RiskProposal(proposed_risk="reversible")
                ),
            ]
        )
    )
    wf = compile_recording(recording, bundle, name="wf", annotate=True, annotator=fake)
    # Safe upgrade landed in the saved workflow.
    assert wf.steps[0].risk == "irreversible"
    # The Save click's heuristic risk (irreversible) was NOT weakened.
    assert wf.steps[1].risk == "irreversible"
    # Sidecar carries the full audit trail.
    sidecar = json.loads((bundle / "annotations.json").read_text())
    assert sidecar["labels"]["step_000"] == "open the patient chart"
    assert any(a["kind"] == "risk_upgrade" for a in sidecar["applied"])
    downgrades = [f for f in sidecar["flagged"] if f["kind"] == "risk_downgrade"]
    assert len(downgrades) == 1
    assert downgrades[0]["needs_operator_confirmation"] is True
    # The saved workflow.json reflects the applied upgrade on disk.
    saved = json.loads((bundle / "workflow.json").read_text())
    assert saved["steps"][0]["risk"] == "irreversible"


# -- the real annotator parses without ANY network ---------------------------


class _StubMessages:
    def __init__(self, text):
        self._text = text

    def create(self, **kwargs):  # mimics anthropic client.messages.create
        block = type("B", (), {"type": "text", "text": self._text})()
        return type("R", (), {"content": [block]})()


class _StubClient:
    def __init__(self, text):
        self.messages = _StubMessages(text)


def test_anthropic_annotator_parses_json_with_a_stub_client():
    # Exercises the REAL annotator's prompt + parse path with a stub client --
    # NO network, NO key, NO anthropic import. Proves the wiring, not the model.
    wf = _wf(_click("step_000", ocr="Pay invoice"))
    reply = json.dumps(
        {
            "steps": [
                {
                    "step_id": "step_000",
                    "label": {"label": "pay the outstanding invoice"},
                    "risk": {
                        "proposed_risk": "irreversible",
                        "rationale": "money moves",
                    },
                }
            ]
        }
    )
    annotator = AnthropicStepAnnotator(model="stub-model", client=_StubClient(reply))
    proposals = annotator.annotate(wf)
    sa = proposals.for_step("step_000")
    assert sa is not None and sa.risk.proposed_risk == "irreversible"
    # And it flows through apply with the same confirm-don't-trust rules.
    result = apply_annotations(wf, annotator)
    assert result.workflow.steps[0].risk == "irreversible"


def test_anthropic_annotator_ignores_a_junk_reply_fail_safe():
    wf = _wf(_click("step_000"))
    annotator = AnthropicStepAnnotator(
        model="stub", client=_StubClient("sorry, no JSON here")
    )
    proposals = annotator.annotate(wf)
    assert proposals.steps == []


# -- the RUNTIME stays $0: an annotated bundle replays with ZERO model calls --


def test_runtime_replay_of_annotated_bundle_makes_zero_model_calls(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    run_dir = tmp_path / "run"

    wf = Workflow(
        name="wf",
        steps=[
            click_step(
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT, text="Saved", timeout_s=0.2
                    )
                ]
            ),
            Step(id="s2", intent="type note", action=ActionKind.TYPE, param="note"),
        ],
    )
    # Annotate the compiled workflow (label-only: behaviour-preserving) and
    # replay the ANNOTATED workflow -- the replayer must never touch a model.
    fake = FakeStepAnnotator(
        WorkflowProposals(
            steps=[
                StepAnnotation(
                    step_id="s1", label=LabelProposal(label="save the record")
                )
            ]
        )
    )
    annotated = apply_annotations(wf, fake).workflow

    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    vision.text_results = {
        "Saved": Match(point=(50, 10), region=(30, 5, 40, 10), confidence=0.9)
    }
    backend = FakeBackend()
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        annotated, params={"note": "hello"}, bundle_dir=bundle, run_dir=run_dir
    )

    assert report.success is True
    assert report.model_calls == 0
    assert report.est_model_cost_usd == 0.0
    assert report.rung_counts.get("grounder", 0) == 0  # no VLM rung either
