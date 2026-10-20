"""A record-selecting click follows the run's record (``Step.target_param``).

The click on a search result or worklist row must land on the row that shows
the run's value, and the identity check must still verify that row before
any input. Anything that would let the click act on an unverified row halts.
"""

from __future__ import annotations

import pytest

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    ParamSpec,
    Step,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.runtime.identity_template import build_identity_template
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.runtime.target_param import NO_TEMPLATE, bind_target_param
from tests.test_replayer import (
    FakeBackend,
    Match,
    OcrLine,
    bundle,
    resolving_vision,
    run_dir,
)

__all__ = ["bundle", "run_dir"]  # pytest fixtures re-exported


def _row_click(
    *,
    context: str | None = "Open chart for Phil (active)",
    ocr_text: str = "Phil",
    structural: StructuralLocator | None = None,
    target_param: str | None = "patient",
) -> Step:
    return Step(
        id="s_open",
        intent="open the patient's chart",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/btn.png",
            region=(100, 100, 50, 20),
            click_point=(110, 105),
            ocr_text=ocr_text,
            context_text=context,
            structural=structural,
        ),
        target_param=target_param,
    )


def _wf(step: Step, **params: str) -> Workflow:
    return Workflow(name="open", params=params or {"patient": "Phil"}, steps=[step])


# --------------------------------------------------------------------------
# IR: additive and validated
# --------------------------------------------------------------------------


def test_unset_target_param_is_not_serialized():
    step = _row_click(target_param=None)
    assert "target_param" not in step.model_dump(mode="json")
    assert _row_click().model_dump(mode="json")["target_param"] == "patient"


def test_target_param_round_trips():
    step = _row_click()
    assert Step.model_validate(step.model_dump(mode="json")) == step


def test_target_param_only_on_clicks():
    with pytest.raises(ValueError, match="CLICK"):
        Step(
            id="t",
            intent="type",
            action=ActionKind.TYPE,
            text="x",
            target_param="patient",
        )


# --------------------------------------------------------------------------
# Binding rules
# --------------------------------------------------------------------------


def test_demo_value_keeps_the_step_unchanged():
    step = _row_click()
    bound, refusal = bind_target_param(step, {"patient": "Phil"}, _wf(step))
    assert refusal is None
    assert bound is step


def test_other_value_looks_for_the_value_without_the_demo_crop():
    step = _row_click(
        structural=StructuralLocator(role="link", name="Phil", selector="#row-17")
    )
    bound, refusal = bind_target_param(step, {"patient": "Susan"}, _wf(step))
    assert refusal is None
    assert bound.anchor.template == NO_TEMPLATE
    assert bound.anchor.ocr_text == "Susan"
    assert bound.anchor.structural.name == "Susan"
    assert bound.anchor.structural.selector is None  # pointed at the old row
    # Identity evidence is untouched: the gate substitutes the run's value.
    assert bound.anchor.context_text == step.anchor.context_text
    assert step.anchor.ocr_text == "Phil"  # the bundle's step is not mutated


def test_label_around_the_value_keeps_its_words():
    step = _row_click(ocr_text="Open Phil")
    bound, _ = bind_target_param(step, {"patient": "Susan"}, _wf(step))
    assert bound.anchor.ocr_text == "Open Susan"


def test_structural_name_that_is_not_the_value_is_dropped():
    step = _row_click(structural=StructuralLocator(role="link", name="Belford, Phil"))
    bound, _ = bind_target_param(step, {"patient": "Susan"}, _wf(step))
    assert bound.anchor.structural is None


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_value_refuses(value):
    step = _row_click()
    params = {} if value is None else {"patient": value}
    _, refusal = bind_target_param(step, params, _wf(step))
    assert refusal is not None and "no value" in refusal


def test_no_identity_evidence_refuses():
    step = _row_click(context=None)
    _, refusal = bind_target_param(step, {"patient": "Susan"}, _wf(step))
    assert refusal is not None and "no identity evidence" in refusal


def test_identity_band_without_the_parameter_refuses():
    step = _row_click(context="Open chart for Alice (active)")
    _, refusal = bind_target_param(step, {"patient": "Susan"}, _wf(step))
    assert refusal is not None and "does not contain" in refusal


def test_unsupplied_embedded_identity_parameter_refuses():
    """The hashed band also embeds a date of birth the run must name."""
    step = _row_click(context=None)
    step.anchor.identity_template = build_identity_template(
        "Open chart for Phil 1972-02-09",
        param_examples={"patient": "Phil", "dob": "1972-02-09"},
        salt_hex="00" * 16,
    )
    workflow = Workflow(
        name="open",
        params={"patient": "Phil"},
        param_specs={"dob": ParamSpec(name="dob", required=True)},
        steps=[step],
    )
    _, ok = bind_target_param(step, {"patient": "Susan", "dob": "1980-01-01"}, workflow)
    assert ok is None
    _, refusal = bind_target_param(step, {"patient": "Susan"}, workflow)
    assert refusal is not None and "'dob'" in refusal


# --------------------------------------------------------------------------
# Replay through the interpreter
# --------------------------------------------------------------------------


def _ocr_vision(row_text: str):
    vision = resolving_vision()
    vision.template_results = []  # the demo crop must not be consulted
    vision.text_results = {
        "Susan": Match(point=(120, 140), region=(100, 132, 60, 16), confidence=0.95)
    }
    vision.ocr_lines = [OcrLine(row_text, region=(170, 132, 240, 16))]
    return vision


def test_replay_clicks_the_run_s_row_and_verifies_it(bundle, run_dir):
    step = _row_click()
    vision = _ocr_vision("Open chart for Susan (active)")
    backend = FakeBackend()
    report = Replayer(backend, vision=vision).run(
        _wf(step), params={"patient": "Susan"}, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True, report.results[0].error
    assert vision.template_calls == []
    assert "Susan" in vision.text_calls
    assert ("click", 120, 140, False) in backend.actions
    identity = report.results[0].identity
    assert identity.status == "verified"
    assert identity.mode == "param"
    assert identity.param == "patient"


def test_replay_halts_on_a_row_naming_another_record(bundle, run_dir):
    step = _row_click()
    vision = _ocr_vision("Open chart for Susan (archived) Getting, Robert")
    backend = FakeBackend()
    report = Replayer(backend, vision=vision).run(
        _wf(step), params={"patient": "Susan"}, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False, report.results[0].identity
    assert backend.actions == []
    assert report.results[0].identity.status == "mismatch"


def test_replay_refusal_sends_no_input(bundle, run_dir):
    step = _row_click(context="Open chart for Alice (active)")
    vision = _ocr_vision("Open chart for Susan (active)")
    backend = FakeBackend()
    report = Replayer(backend, vision=vision).run(
        _wf(step), params={"patient": "Susan"}, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert backend.actions == []
    result = report.results[0]
    assert result.safety_halt is True
    assert "does not contain" in (result.error or "")


def test_replay_unreadable_row_halts_a_reversible_selection(bundle, run_dir):
    """An ordinary reversible click proceeds on an unreadable band; a click
    that selects a record by a run value must verify the row first."""
    step = _row_click()
    vision = _ocr_vision("Open chart for Susan (active)")
    vision.ocr_lines = []
    backend = FakeBackend()
    report = Replayer(backend, vision=vision).run(
        _wf(step), params={"patient": "Susan"}, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert backend.actions == []
    result = report.results[0]
    assert result.identity.status == "unreadable"
    assert "selects a record by a run value" in (result.error or "")


def test_healing_never_adopts_another_record_s_row(bundle, run_dir):
    step = _row_click()
    vision = _ocr_vision("Open chart for Susan (active)")
    report = Replayer(FakeBackend(), vision=vision).run(
        _wf(step), params={"patient": "Susan"}, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert report.results[0].heal is None


# --------------------------------------------------------------------------
# Not admitted for certified, production or governed runs (yet)
# --------------------------------------------------------------------------


def test_clinical_write_certification_refuses_a_record_selecting_click():
    from openadapt_flow.policy import evaluate_policy, load_policy

    workflow = _wf(_row_click())
    report = evaluate_policy(workflow, load_policy("clinical-write"))
    hits = [v for v in report.violations if v.rule == "unqualified_record_selection"]
    assert [v.step_id for v in hits] == ["s_open"]
    assert "fault driver" in hits[0].reason

    plain = evaluate_policy(
        _wf(_row_click(target_param=None)), load_policy("clinical-write")
    )
    assert not any(v.rule == "unqualified_record_selection" for v in plain.violations)

    permissive = evaluate_policy(workflow, load_policy("permissive"))
    assert not any(
        v.rule == "unqualified_record_selection" for v in permissive.violations
    )


def test_governed_run_gate_refuses_whatever_the_policy():
    from openadapt_flow.run_gate import GATE_IDENTITY, _gate_identity

    workflow = _wf(_row_click())
    result = _gate_identity(
        workflow,
        list(workflow.steps),
        require_current_risk_certification=False,
        certifying_policy=None,
    )
    assert result.gate == GATE_IDENTITY
    assert result.passed is False
    assert result.offenders == ["s_open"]
    assert "target_param" in result.detail
