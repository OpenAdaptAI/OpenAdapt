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


def test_select_all_needs_network_capture():
    steps = [_click(0, "#lname", "textbox"), _key(1, "Control+a"), _type(2, "lname")]
    assert local_edits(steps, _events(3), network=None) == []


def test_select_all_with_a_write_request_sent_stays_with_a_person():
    steps = [_click(0, "#lname", "textbox"), _key(1, "Control+a"), _type(2, "lname")]
    sent = [{"method": "PUT", "t": 1.5, "url": URL + "/draft"}]
    assert local_edits(steps, _events(3), network=sent) == []


def test_a_changed_record_store_stays_with_a_person():
    steps = [_click(0, "#lname", "textbox"), _key(1, "Control+a"), _type(2, "lname")]
    events = _events(3, e1={"sor_before": {"n": 1}, "sor_after": {"n": 2}})
    assert local_edits(steps, events, network=[]) == []
    same = _events(3, e1={"sor_before": {"n": 1}, "sor_after": {"n": 1}})
    assert _ids(local_edits(steps, same, network=[])) == {"step_001": "select_all"}


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


def _entry(network: Optional[list] = None, **changes):
    steps = [_click(0, "#q", "textbox"), _type(1, "q"), _key(2, "Enter")]
    events = _events(3, e1={"param": "q"}, **changes)
    return local_edits(steps, events, network=[] if network is None else network)


def test_enter_after_typing_into_a_text_field_is_a_local_edit():
    (edit,) = _entry()
    assert edit.step_id == "step_002" and edit.pattern == "confirm_entry"
    kinds = [p.kind for p in edit.postconditions]
    assert kinds == [PostconditionKind.LOCAL_EDIT, PostconditionKind.FIELD_VALUE]
    assert edit.postconditions[0].field_selector == "#q"
    assert edit.postconditions[1].param == "q"


def test_enter_in_a_text_field_with_a_write_request_sent_stays_with_a_person():
    assert _entry(network=[{"method": "POST", "t": 2.4, "url": URL}]) == []
    steps = [_click(0, "#q", "textbox"), _type(1, "q"), _key(2, "Enter")]
    assert local_edits(steps, _events(3, e1={"param": "q"}), network=None) == []


def test_enter_after_a_literal_or_nothing_typed_stays_with_a_person():
    steps = [_click(0, "#q", "textbox"), _key(1, "Enter")]
    assert local_edits(steps, _events(2), network=[]) == []


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


def test_a_tab_role_click_in_the_same_frame_is_a_local_edit():
    steps = [
        _click(0, "#tab-contact", "tab", "Contact"),
        _click(1, "#street", "textbox"),
    ]
    (edit,) = local_edits(steps, _events(2), network=[])
    assert edit.pattern == "in_form_tab"


def test_a_tab_click_with_a_write_request_sent_stays_with_a_person():
    steps = [
        _click(0, "#tab-contact", "tab", "Contact"),
        _click(1, "#street", "textbox"),
    ]
    sent = [{"method": "PATCH", "t": 0.8, "url": URL + "/autosave"}]
    assert local_edits(steps, _events(2), network=sent) == []


def test_a_write_shaped_click_is_never_a_local_edit():
    save = _click(0, None, "button", "Save")
    save.risk = "irreversible"
    steps = [save, _click(1, "#street", "textbox")]
    assert local_edits(steps, _events(2), network=[]) == []


# -- the dismiss of an informational dialog (#1377) ------------------------------------------


def _infer(step: Step) -> Step:
    inference = infer_step_risk(step)
    step.risk = inference.risk
    step.risk_explanation = inference.explanation
    step.risk_review_required = inference.requires_review
    return step


def _whats_new() -> list[Step]:
    # MockMed's What's New notice: "Continue to tasks" closes it, the page stays.
    dismiss = _infer(_click(0, "#notice-continue", "button", "Continue to tasks"))
    assert dismiss.risk == "irreversible"  # "continue" is a write word
    return [dismiss, _click(1, "#task-search", "textbox", "Search tasks")]


def test_dismissing_an_informational_dialog_is_a_local_edit():
    (edit,) = local_edits(_whats_new(), _events(2), network=[])
    assert edit.step_id == "step_000" and edit.pattern == "dismiss_dialog"
    local, gone = edit.postconditions
    assert local.kind is PostconditionKind.LOCAL_EDIT and local.field_selector is None
    assert gone.kind is PostconditionKind.TEXT_ABSENT
    assert gone.text == "Continue to tasks"


def test_a_dismiss_with_a_write_request_sent_stays_with_a_person():
    sent = [{"method": "POST", "t": 0.4, "url": URL + "/notices/seen"}]
    assert local_edits(_whats_new(), _events(2), network=sent) == []
    assert local_edits(_whats_new(), _events(2), network=None) == []
    moved = _events(2, e0={"url_after": URL + "/tasks"})
    assert local_edits(_whats_new(), moved, network=[]) == []


@pytest.mark.parametrize(
    "name", ["Continue to checkout", "Continue and save", "Next: submit", "Save"]
)
def test_a_dismiss_shaped_label_with_another_write_word_stays_irreversible(name):
    steps = [_infer(_click(0, "#b", "button", name)), _click(1, "#x", "textbox")]
    assert steps[0].risk == "irreversible"
    assert local_edits(steps, _events(2), network=[]) == []


def test_the_click_that_confirms_a_dialog_is_never_reconsidered():
    ok = _infer(_click(0, "#ok", "button", "OK"))
    ok.risk = "irreversible"
    ok.risk_explanation = "confirms the dialog opened by step_000; this click saves"
    steps = [ok, _click(1, "#x", "textbox")]
    assert local_edits(steps, _events(2), network=[]) == []


def test_the_risk_reinference_reads_a_sealed_dismiss():
    steps = _whats_new()
    (edit,) = local_edits(steps, _events(2), network=[])
    dismiss = steps[0]
    dismiss.risk_explanation = edit.explanation
    dismiss.expect = list(edit.postconditions)
    inference = infer_step_risk(dismiss)
    assert inference.risk == "reversible" and not inference.requires_review
    # Without the label-gone check the write word wins again.
    dismiss.expect = [edit.postconditions[0]]
    assert infer_step_risk(dismiss).risk == "irreversible"


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
    def __init__(
        self, *, url=URL, focused=True, label="Massachusetts", text=None
    ) -> None:
        self.url, self.page_title, self.page_count = url, "Form", 1
        self._focused, self._label, self._text = focused, label, text

    def focused_element_matches(self, selector):
        return self._focused

    def focused_select_label(self):
        return self._label

    def focused_text_value(self):
        return self._text


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


@pytest.mark.parametrize(
    "text, passes",
    [
        ("Massachusetts", True),
        ("massachusetts", False),
        ("Massachusetts ", False),
        (None, False),
    ],
)
def test_the_field_value_check_reads_a_text_field_exactly(text, passes):
    pc = Postcondition(kind=PostconditionKind.FIELD_VALUE, param="state")
    replayer = _replayer(_Backend(label=None, text=text))
    assert replayer._postcondition_passes(pc, b"", None, START) is passes


# -- compiled end to end: the What's New dismiss gets no record_written (#1377) ---------------


def _whats_new_recording(tmp_path, *, dismiss_post_t: Optional[float]):
    import json

    from openadapt_flow.compiler.effect_mining import SOR_AFTER_KEY, SOR_BEFORE_KEY

    page = {
        "url_before": URL,
        "url_after": URL,
        "title_before": "MockMed",
        "title_after": "MockMed",
        "pages_before": 1,
        "pages_after": 1,
    }
    chart = [{"id": "e1", "type": "Encounter", "patient_id": "p1"}]
    events = [
        {
            "i": 0,
            "kind": "click",
            "x": 129,
            "y": 236,
            "t": 1.0,
            "structural": {
                "selector": "#notice-continue",
                "role": "button",
                "name": "Continue to tasks",
            },
            SOR_BEFORE_KEY: [],
            SOR_AFTER_KEY: [],
            **page,
        },
        {
            "i": 1,
            "kind": "click",
            "x": 300,
            "y": 400,
            "t": 3.0,
            "structural": {
                "selector": "#save",
                "role": "button",
                "name": "Save encounter",
            },
            SOR_BEFORE_KEY: [],
            SOR_AFTER_KEY: chart,
            **page,
        },
    ]
    rec = tmp_path / "recording"
    (rec / "frames").mkdir(parents=True)
    from PIL import Image

    for i in range(len(events)):
        for side in ("before", "after"):
            Image.new("RGB", (1280, 800), "white").save(
                rec / "frames" / f"{i:04d}_{side}.png"
            )
    (rec / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (rec / "meta.json").write_text(json.dumps({"params": {}}))
    lines = [{"method": "POST", "url": URL + "/api/encounters", "t": 3.2, "seq": 1}]
    if dismiss_post_t is not None:
        lines.insert(
            0,
            {"method": "POST", "url": URL + "/api/seen", "t": dismiss_post_t, "seq": 0},
        )
    header = {"schema": "openadapt.network-capture/1", "exchanges": len(lines)}
    (rec / "network.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in [header, *lines])
    )
    return rec


def test_a_compiled_whats_new_dismiss_gets_no_record_written(tmp_path):
    from openadapt_flow.compiler.compile import compile_recording
    from openadapt_flow.runtime.effects import EffectKind

    rec = _whats_new_recording(tmp_path, dismiss_post_t=None)
    wf = compile_recording(rec, tmp_path / "bundle", name="notice", mine_effects=True)
    dismiss, save = wf.steps
    assert dismiss.risk == "reversible" and not dismiss.risk_review_required
    assert is_local_edit(dismiss) and dismiss.effects == []
    assert EffectKind.RECORD_WRITTEN in [e.kind for e in save.effects]


def test_a_compiled_dismiss_that_sent_a_write_keeps_its_risk(tmp_path):
    from openadapt_flow.compiler.compile import compile_recording

    rec = _whats_new_recording(tmp_path, dismiss_post_t=1.1)
    wf = compile_recording(rec, tmp_path / "bundle", name="notice", mine_effects=True)
    dismiss = wf.steps[0]
    assert dismiss.risk == "irreversible" and not is_local_edit(dismiss)
