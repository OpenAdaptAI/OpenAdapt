"""A click that only opens a confirmation dialog is not the save."""

from __future__ import annotations

import pytest

from openadapt_flow.ir import ActionKind, Anchor, Step, StructuralLocator
from openadapt_flow.risk import confirmation_opener_step_ids, infer_step_risk


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
