"""A click that only opens a confirmation dialog is not the save."""

from __future__ import annotations

import pytest

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    StructuralLocator,
)
from openadapt_flow.policy import effective_step_risk
from openadapt_flow.risk import (
    CONFIRMATION_OPENER_PREFIX,
    confirmation_opener_step_ids,
    infer_step_risk,
    is_sealed_confirmation_opener,
)
from openadapt_flow.runtime.effects import Effect, EffectKind


def _click(step_id: str, label: str, selector: str = "button") -> Step:
    step = Step(
        id=step_id,
        intent=f"click '{label}'",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template=f"templates/{step_id}.png",
            region=(0, 0, 160, 64),
            click_point=(80, 32),
            ocr_text=label,
            structural=StructuralLocator(selector=selector, role="button", name=label),
        ),
    )
    step.risk = infer_step_risk(step).risk
    return step


def _type(step_id: str) -> Step:
    return Step(
        id=step_id, intent="type <fname>", action=ActionKind.TYPE, param="fname"
    )


def test_openemr_referral_create_then_confirm():
    """The referral recording: "Create New Patient" opens the duplicate check,
    and its "Confirm" button creates the chart."""

    create = _click("step_010", "Create New Patient", "#create")
    confirm = _click("step_011", "Confirm", "#confirmCreate")
    assert create.risk == confirm.risk == "irreversible"
    assert confirmation_opener_step_ids([_type("step_009"), create, confirm]) == (
        ("step_010", "step_011"),
    )


@pytest.mark.parametrize("label", ["Yes", "Confirm Create New Patient", "yes, delete"])
def test_confirm_shaped_labels_match(label):
    pair = [_click("a", "Submit claim"), _click("b", label)]
    if pair[1].risk != "irreversible":
        pytest.skip("label not inferred as a write")
    assert confirmation_opener_step_ids(pair) == (("a", "b"),)


@pytest.mark.parametrize("label", ["OK", "Close", "Done", "Back"])
def test_acknowledgements_never_move_the_save(label):
    assert confirmation_opener_step_ids([_click("a", "Save"), _click("b", label)]) == ()


def test_a_step_between_them_breaks_the_pair():
    steps = [_click("a", "Create New Patient"), _type("t"), _click("b", "Confirm")]
    assert confirmation_opener_step_ids(steps) == ()


def test_an_opener_with_its_own_effect_or_binding_stays_the_save():

    opener = _click("a", "Create New Patient")
    opener.effects = [object()]  # type: ignore[list-item]
    assert confirmation_opener_step_ids([opener, _click("b", "Confirm")]) == ()


def test_two_confirms_in_a_row_keep_the_first():
    steps = [_click("a", "Confirm"), _click("b", "Confirm")]
    assert confirmation_opener_step_ids(steps) == ()


def test_sealed_confirmation_opener_infers_reversible():
    create = _click("step_010", "Create New Patient", "#create")
    confirm = _click("step_011", "Confirm", "#confirmCreate")
    create.risk = "reversible"
    create.risk_explanation = f"{CONFIRMATION_OPENER_PREFIX} ({confirm.id})"
    create.expect = [
        Postcondition(
            kind=PostconditionKind.TEXT_PRESENT,
            text="Confirm Create New Patient",
            timeout_s=10.0,
        )
    ]
    assert is_sealed_confirmation_opener(create) is True
    inference = infer_step_risk(create)
    assert inference.risk == "reversible"
    assert "pattern:confirmation_opener" in inference.evidence
    assert effective_step_risk(create) == "reversible"


def test_unsealed_confirmation_opener_infers_irreversible():
    create = _click("step_010", "Create New Patient", "#create")
    create.risk = "reversible"
    create.risk_explanation = f"{CONFIRMATION_OPENER_PREFIX} (step_011)"
    create.expect = []
    assert is_sealed_confirmation_opener(create) is False
    assert infer_step_risk(create).risk == "irreversible"
    assert effective_step_risk(create) == "irreversible"


def test_confirmation_opener_with_effect_stays_irreversible():
    create = _click("step_010", "Create New Patient", "#create")
    create.risk = "reversible"
    create.risk_explanation = f"{CONFIRMATION_OPENER_PREFIX} (step_011)"
    create.expect = [
        Postcondition(
            kind=PostconditionKind.TEXT_PRESENT,
            text="Confirm Create New Patient",
            timeout_s=10.0,
        )
    ]
    create.effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"email": "test@example.com"},
            expected_count=1,
            forbid_collateral_loss=True,
            risk="reversible",
            probe="synthetic test",
            timeout_s=5.0,
        )
    ]
    assert is_sealed_confirmation_opener(create) is False
    assert infer_step_risk(create).risk == "irreversible"
