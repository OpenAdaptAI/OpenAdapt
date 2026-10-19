"""Local edits classified from the recording (docs/design/LOCAL_EDIT_CLASSIFICATION.md).

Each pattern classifies only with all of its evidence; each missing piece
leaves the step for a person; each outcome check fails when the action did
anything else at run time. Synthetic events only.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from openadapt_flow.compiler.local_edit import local_edits
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.policy import step_tags
from openadapt_flow.risk import infer_step_risk, is_local_edit

FRAME = ["div > iframe"]
URL = "http://app/form"


def _click(i: int, selector: Optional[str], role: str, name: str = "Field") -> Step:
    return Step(
        id=f"step_{i:03d}",
        intent=f"click {name}",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template=f"templates/{i}.png",
            region=(0, 0, 10, 10),
            click_point=(5, 5),
            structural=StructuralLocator(
                selector=selector, frame_path=list(FRAME), role=role, name=name
            ),
        ),
        risk_review_required=False,
    )


def _key(i: int, key: str) -> Step:
    return Step(
        id=f"step_{i:03d}",
        intent=f"press {key}",
        action=ActionKind.KEY,
        key=key,
        risk_review_required=True,
        risk_explanation="special key may navigate, edit, or invoke an application shortcut",
    )


def _type(i: int, param: str) -> Step:
    return Step(
        id=f"step_{i:03d}",
        intent="type a value",
        action=ActionKind.TYPE,
        text="{" + param + "}",
    )


def _event(i: int, **changes: Any) -> dict[str, Any]:
    event = {
        "i": i,
        "t": float(i),
        "url_before": URL,
        "url_after": URL,
        "title_before": "Form",
        "title_after": "Form",
        "pages_before": 1,
        "pages_after": 1,
    }
    event.update(changes)
    return event


def _events(n: int, **at: dict[str, Any]) -> list[dict[str, Any]]:
    return [_event(i, **at.get(f"e{i}", {})) for i in range(n)]


def _ids(edits) -> dict[str, str]:
    return {e.step_id: e.pattern for e in edits}


# -- select-all before typing ---------------------------------------------------------


def test_select_all_in_a_text_field_before_typing_is_a_local_edit():
    steps = [_click(0, "#lname", "textbox"), _key(1, "Control+a"), _type(2, "lname")]
    (edit,) = local_edits(steps, _events(3), network=[])
    assert edit.step_id == "step_001" and edit.pattern == "select_all"
    (check,) = edit.postconditions
    assert check.kind is PostconditionKind.LOCAL_EDIT
    assert check.field_selector == "#lname"


@pytest.mark.parametrize(
    "steps, events, network, why",
    [
        (
            [_click(0, "#lname", "button"), _key(1, "Control+a"), _type(2, "lname")],
            _events(3),
            [],
            "not a text field",
        ),
        (
            [
                _click(0, "#lname", "textbox"),
                _key(1, "Control+a"),
                _click(2, "#x", "textbox"),
            ],
            _events(3),
            [],
            "nothing is typed next",
        ),
        (
            [_click(0, "#lname", "textbox"), _key(1, "Control+a"), _type(2, "lname")],
            _events(3, e1={"url_after": URL + "?next"}),
            [],
            "the URL changed",
        ),
        (
            [_click(0, "#lname", "textbox"), _key(1, "Control+a"), _type(2, "lname")],
            _events(3),
            [{"method": "POST", "t": 1.2, "url": URL}],
            "a write request in the window",
        ),
        (
            [_click(0, None, "textbox"), _key(1, "Control+a"), _type(2, "lname")],
            _events(3),
            [],
            "no field selector to keep focus in",
        ),
    ],
)
def test_select_all_without_its_evidence_stays_with_a_person(
    steps, events, network, why
):
    assert local_edits(steps, events, network=network) == [], why


def test_select_all_needs_no_network_capture():
    steps = [_click(0, "#lname", "textbox"), _key(1, "Control+a"), _type(2, "lname")]
    assert _ids(local_edits(steps, _events(3), network=None)) == {
        "step_001": "select_all"
    }


# -- Enter on a typed dropdown option ---------------------------------------------------


def _dropdown(network: Optional[list] = None, **changes):
    steps = [_click(0, "#state", "combobox"), _type(1, "state"), _key(2, "Enter")]
    events = _events(3, e1={"param": "state"}, **changes)
    return local_edits(steps, events, network=[] if network is None else network)


def test_enter_after_typing_a_dropdown_option_is_a_local_edit():
    (edit,) = _dropdown()
    assert edit.step_id == "step_002" and edit.pattern == "confirm_option"
    kinds = [p.kind for p in edit.postconditions]
    assert kinds == [PostconditionKind.LOCAL_EDIT, PostconditionKind.FIELD_VALUE]
    assert edit.postconditions[1].param == "state"


def test_enter_needs_network_capture_and_no_write():
    steps = [_click(0, "#state", "combobox"), _type(1, "state"), _key(2, "Enter")]
    events = _events(3, e1={"param": "state"})
    assert local_edits(steps, events, network=None) == []
    assert _dropdown(network=[{"method": "POST", "t": 2.5, "url": URL}]) == []
    assert _dropdown(e2={"title_after": "Saved"}) == []


def test_enter_in_a_text_field_stays_with_a_person():
    steps = [_click(0, "#q", "textbox"), _type(1, "q"), _key(2, "Enter")]
    events = _events(3, e1={"param": "q"})
    assert local_edits(steps, events, network=[]) == []


# -- in-form tabs -------------------------------------------------------------------------


def test_a_tab_of_the_same_form_is_a_local_edit():
    steps = [_click(0, None, "button", "Contact"), _click(1, "#street", "textbox")]
    (edit,) = local_edits(steps, _events(2), network=[])
    assert edit.pattern == "in_form_tab"
    assert [p.kind for p in edit.postconditions] == [PostconditionKind.LOCAL_EDIT]


def test_a_tab_click_needs_capture_the_same_frame_and_no_write():
    steps = [_click(0, None, "tab", "Contact"), _click(1, "#street", "textbox")]
    assert local_edits(steps, _events(2), network=None) == []
    assert local_edits(steps, _events(2), network=[{"method": "POST", "t": 0.5}]) == []
    other = _click(1, "#street", "textbox")
    other.anchor.structural.frame_path = ["div > iframe:nth-of-type(2)"]
    assert local_edits([steps[0], other], _events(2), network=[]) == []


def test_a_write_shaped_click_is_never_a_local_edit():
    save = _click(0, None, "button", "Save")
    save.risk = "irreversible"
    steps = [save, _click(1, "#street", "textbox")]
    assert local_edits(steps, _events(2), network=[]) == []


# -- risk, tags and the outcome checks ------------------------------------------------------


def _classified_key() -> Step:
    step = _key(1, "Control+a")
    step.risk_review_required = False
    step.risk_explanation = "local edit: selects the text of the field"
    step.expect = [
        Postcondition(kind=PostconditionKind.LOCAL_EDIT, field_selector="#a")
    ]
    return step


def test_the_risk_reinference_reads_a_sealed_local_edit():
    step = _classified_key()
    assert is_local_edit(step)
    inference = infer_step_risk(step)
    assert inference.risk == "reversible" and not inference.requires_review
    # Without its runtime check the explanation alone proves nothing.
    step.expect = []
    assert infer_step_risk(step).requires_review


def test_a_local_edit_tab_is_not_entity_navigation():
    tab = _click(0, None, "button", "Contact")
    tab.anchor.ocr_text = "Contact"
    wf = Workflow(name="w", steps=[tab])
    assert "entity_navigation" in step_tags(
        tab, wf, require_current_certification=False
    )
    tab.risk_explanation = "local edit: switches the panel of the same form"
    tab.expect = [Postcondition(kind=PostconditionKind.LOCAL_EDIT)]
    assert "entity_navigation" not in step_tags(
        tab, wf, require_current_certification=False
    )


def test_unset_local_edit_fields_serialize_as_before():
    pc = Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved")
    assert set(pc.model_dump()) == {
        "kind",
        "text",
        "region",
        "phash",
        "phash_tolerance",
        "timeout_s",
        "template",
    }


class _Backend:
    def __init__(self, *, url=URL, focused=True, label="Massachusetts") -> None:
        self.url, self.page_title, self.page_count = url, "Form", 1
        self._focused, self._label = focused, label

    def focused_element_matches(self, selector):
        return self._focused

    def focused_select_label(self):
        return self._label


def _replayer(backend):
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_replayer import FakeVision

    replayer = Replayer(backend, vision=FakeVision())
    replayer._current_params = {"state": "Massachusetts"}
    return replayer


START = {"url": URL, "page_title": "Form", "page_count": 1}


@pytest.mark.parametrize(
    "backend, passes",
    [
        (_Backend(), True),
        (_Backend(url=URL + "?saved"), False),
        (_Backend(focused=False), False),
        (_Backend(focused=None), False),
    ],
)
def test_the_local_edit_check_fails_when_the_action_did_anything_else(backend, passes):
    pc = Postcondition(kind=PostconditionKind.LOCAL_EDIT, field_selector="#a")
    assert _replayer(backend)._postcondition_passes(pc, b"", None, START) is passes


def test_the_local_edit_check_fails_closed_when_it_cant_observe():
    pc = Postcondition(kind=PostconditionKind.LOCAL_EDIT)
    assert _replayer(_Backend())._postcondition_passes(pc, b"", None, {}) is False


@pytest.mark.parametrize(
    "label, passes",
    [
        ("Massachusetts", True),
        ("  massachusetts ", True),
        ("Maine", False),
        (None, False),
    ],
)
def test_the_field_value_check_reads_the_dropdown(label, passes):
    pc = Postcondition(kind=PostconditionKind.FIELD_VALUE, param="state")
    replayer = _replayer(_Backend(label=label))
    assert replayer._postcondition_passes(pc, b"", None, START) is passes
