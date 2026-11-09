"""Lint refuses a same-channel effect oracle on a consequential write.

A bundle whose only effect is on-screen success shares the acting surface.
An independent API, SQL, second-session, or file read is channel-disjoint.
This is static and model-free. Runtime VERIFIED refusal lives in PR 435.
"""

from __future__ import annotations

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    ApiBinding,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.policy import lint_workflow
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    ReadbackNav,
    ReadbackSpec,
)
from openadapt_flow.verification import declared_effect_is_independent_read

_PC = [Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved OK")]
_INDEPENDENT = Effect(
    kind=EffectKind.RECORD_WRITTEN,
    match={"patient_id": "p1"},
    expected_count=1,
)
_ONSCREEN = Effect(
    kind=EffectKind.FIELD_EQUALS,
    match={"patient_id": "p1"},
    field="status",
    value="saved",
    readback=ReadbackSpec(region=(0, 0, 120, 24), different_path=False),
)
_DIFFERENT_PATH = Effect(
    kind=EffectKind.FIELD_EQUALS,
    match={"patient_id": "p1"},
    field="status",
    value="saved",
    readback=ReadbackSpec(
        region=(0, 0, 120, 24),
        different_path=True,
        renavigation=[
            ReadbackNav(action="click", point=(8, 8)),
            ReadbackNav(action="key", key="Enter"),
        ],
    ),
)


def _step(step_id: str, *, effects=None) -> Step:
    return Step(
        id=step_id,
        intent=f"click '{step_id}'",
        action=ActionKind.CLICK,
        risk="irreversible",
        anchor=Anchor(
            template=f"{step_id}.png",
            region=(0, 0, 10, 10),
            click_point=(5, 5),
            ocr_text="Save",
            context_text="Row 42 Jane Doe",
        ),
        identity_armed=True,
        expect=list(_PC),
        effects=list(effects or []),
    )


def _wf(*steps: Step) -> Workflow:
    return Workflow(name="channel-disjoint", steps=list(steps))


def _codes(workflow: Workflow) -> set[str]:
    return {finding.code for finding in lint_workflow(workflow).findings}


def test_declared_onscreen_read_is_not_independent() -> None:
    assert not declared_effect_is_independent_read(_ONSCREEN)
    assert not declared_effect_is_independent_read(_DIFFERENT_PATH)
    assert declared_effect_is_independent_read(_INDEPENDENT)


def test_on_screen_success_only_is_a_finding() -> None:
    report = lint_workflow(_wf(_step("save", effects=[_ONSCREEN])))
    assert any(
        finding.code == "same_channel_oracle" and finding.step_id == "save"
        for finding in report.findings
    )
    assert report.max_severity == "warn"


def test_banner_only_bundle_is_a_finding() -> None:
    assert "same_channel_oracle" in _codes(_wf(_step("save")))


def test_different_path_onscreen_is_still_same_channel() -> None:
    assert "same_channel_oracle" in _codes(
        _wf(_step("save", effects=[_DIFFERENT_PATH]))
    )


def test_separate_read_is_clean() -> None:
    report = lint_workflow(_wf(_step("save", effects=[_INDEPENDENT])))
    assert not any(finding.code == "same_channel_oracle" for finding in report.findings)
    assert not any(
        finding.code == "missing_effect_contract" for finding in report.findings
    )


def test_onscreen_plus_independent_read_is_clean() -> None:
    report = lint_workflow(_wf(_step("save", effects=[_ONSCREEN, _INDEPENDENT])))
    assert not any(finding.code == "same_channel_oracle" for finding in report.findings)


def test_api_path_independent_read_does_not_cover_gui_onscreen() -> None:
    step = _step("save", effects=[_ONSCREEN])
    step.api_binding = ApiBinding(url_template="/records/{id}", effects=[_INDEPENDENT])
    codes = _codes(_wf(step))
    assert "same_channel_oracle" in codes
    finding = next(
        item
        for item in lint_workflow(_wf(step)).findings
        if item.code == "same_channel_oracle"
    )
    assert "gui" in finding.message


def test_api_only_independent_read_is_clean() -> None:
    step = _step("save", effects=[])
    step.api_binding = ApiBinding(
        url_template="/records/{id}",
        on_unavailable="halt",
        effects=[_INDEPENDENT],
    )
    report = lint_workflow(_wf(step))
    assert not any(finding.code == "same_channel_oracle" for finding in report.findings)
    assert not any(
        finding.code == "missing_effect_contract" for finding in report.findings
    )
