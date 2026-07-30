"""Auto risk-classification: unit tests for the heuristic plus an end-to-end
compiler test that a write-shaped step compiles ``irreversible`` while a benign
navigation step stays ``reversible``, and that ``risk_overrides`` still wins.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from openadapt_flow.compiler import compile_recording
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    ApiBinding,
    Postcondition,
    PostconditionKind,
    Step,
    StructuralLocator,
)
from openadapt_flow.risk import (
    ApplicationRiskRule,
    classify_step_risk,
    infer_step_risk,
    is_write_shaped,
)

VIEWPORT = (1280, 800)


def _click_step(intent: str, ocr: str | None) -> Step:
    return Step(
        id="s",
        intent=intent,
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="t.png", region=(0, 0, 1, 1), click_point=(0, 0), ocr_text=ocr
        ),
    )


class TestHeuristic:
    @pytest.mark.parametrize(
        "text",
        [
            "Save",
            "Save as new message",
            "Submit",
            "Submit order",
            "Confirm",
            "Create patient",
            "Delete row",
            "Update record",
            "Send message",
            "+Add",
            "Add note",
            "Approve",
            "Pay now",
            "Sign up",
            "Next",
            "Continue",
            "Cancel",
        ],
    )
    def test_write_shaped_true(self, text: str) -> None:
        assert is_write_shaped(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Login",
            "Sign In",
            "Search",
            "Belford, Phil",
            "Address book",  # 'add' must not trip inside 'address'
            "Postal code",  # 'post' must not trip inside 'postal'
            "Open chart",
            "",
        ],
    )
    def test_write_shaped_false(self, text: str) -> None:
        assert not is_write_shaped(text)

    def test_submit_click_is_irreversible(self) -> None:
        assert (
            classify_step_risk(_click_step("click 'Save as new message'", "Save"))
            == "irreversible"
        )

    def test_benign_navigation_is_reversible(self) -> None:
        assert classify_step_risk(_click_step("click 'Login'", "Login")) == "reversible"
        assert (
            classify_step_risk(_click_step("click 'ford,Phil'", "Belford, Phil"))
            == "reversible"
        )

    def test_keyboard_and_drag_risks_are_explicit(self) -> None:
        typing = Step(id="s", intent="type 'save the world'", action=ActionKind.TYPE)
        assert classify_step_risk(typing) == "reversible"
        key = Step(id="s", intent="press Enter", action=ActionKind.KEY, key="Enter")
        key_inference = infer_step_risk(key)
        assert key_inference.risk == "reversible"
        assert key_inference.requires_review is True
        submit_key = Step(
            id="s",
            intent="press Enter to submit",
            action=ActionKind.KEY,
            key="Enter",
        )
        assert classify_step_risk(submit_key) == "irreversible"
        hotkey = Step(
            id="s",
            intent="press Control+s",
            action=ActionKind.HOTKEY,
            key="s",
            modifiers=["Control"],
        )
        assert classify_step_risk(hotkey) == "irreversible"
        select_all = Step(
            id="s",
            intent="press Control+a",
            action=ActionKind.HOTKEY,
            key="a",
            modifiers=["Control"],
        )
        select_inference = infer_step_risk(select_all)
        assert select_inference.risk == "reversible"
        assert select_inference.requires_review is True
        drag = Step(id="s", intent="drag item", action=ActionKind.DRAG)
        drag_inference = infer_step_risk(drag)
        assert drag_inference.risk == "reversible"
        assert drag_inference.requires_review is True
        destructive_drag = Step(
            id="s",
            intent="drag item to Delete",
            action=ActionKind.DRAG,
        )
        assert classify_step_risk(destructive_drag) == "irreversible"

    def test_unlabelled_coordinate_click_requires_review(self) -> None:
        step = Step(
            id="s",
            intent="click at (10, 12)",
            action=ActionKind.CLICK,
            anchor=Anchor(template="t.png", region=(0, 0, 1, 1), click_point=(10, 12)),
        )
        inference = infer_step_risk(step)
        assert inference.risk == "reversible"
        assert inference.requires_review is True

    def test_role_only_text_field_still_requires_review(self) -> None:
        step = Step(
            id="s",
            intent="click at (10, 12)",
            action=ActionKind.CLICK,
            anchor=Anchor(
                template="t.png",
                region=(0, 0, 1, 1),
                click_point=(10, 12),
                structural=StructuralLocator(selector="#username", role="textbox"),
            ),
        )
        assert infer_step_risk(step).requires_review is True

    def test_structural_accessible_name_is_risk_evidence(self) -> None:
        step = Step(
            id="s",
            intent="click at (10, 12)",
            action=ActionKind.CLICK,
            anchor=Anchor(
                template="t.png",
                region=(0, 0, 1, 1),
                click_point=(10, 12),
                structural=StructuralLocator(
                    selector="#username", role="textbox", name="Username"
                ),
            ),
        )
        inference = infer_step_risk(step)
        assert inference.risk == "reversible"
        assert inference.requires_review is False

        step.anchor.structural.name = "Save"
        assert infer_step_risk(step).risk == "irreversible"

    @pytest.mark.parametrize("kind", ["rest", "fhir", "mcp", "tool"])
    def test_external_actuator_binding_is_consequential(self, kind: str) -> None:
        step = _click_step("click Open", "Open")
        step.api_binding = ApiBinding(
            kind=kind,
            method="POST" if kind in {"rest", "fhir"} else "invoke",
            url_template="/bounded-operation",
        )

        inference = infer_step_risk(step)

        assert inference.risk == "irreversible"
        assert f"binding:{kind}" in inference.evidence
        assert inference.requires_review is False

    def test_clipboard_and_file_operations_are_governed(self) -> None:
        sensitive = Step(
            id="copy",
            intent="copy patient member ID to clipboard",
            action=ActionKind.HOTKEY,
            key="c",
            modifiers=["Control"],
        )
        unknown = Step(
            id="copy-unknown",
            intent="copy selected value",
            action=ActionKind.HOTKEY,
            key="c",
            modifiers=["Control"],
        )

        assert infer_step_risk(sensitive).risk == "irreversible"
        assert infer_step_risk(unknown).requires_review is True
        assert infer_step_risk(_click_step("click Download", "Download")).risk == (
            "irreversible"
        )

    def test_navigation_and_special_keys_require_review(self) -> None:
        navigation = _click_step("click Open chart", "Open chart")
        navigation.expect = [Postcondition(kind=PostconditionKind.URL_CHANGED)]
        special_key = Step(
            id="rename",
            intent="press F2",
            action=ActionKind.KEY,
            key="F2",
        )

        assert infer_step_risk(navigation).requires_review is True
        assert infer_step_risk(special_key).requires_review is True

    def test_drag_destination_and_right_click_supply_risk_evidence(self) -> None:
        drag = Step(
            id="drag",
            intent="drag recorded target to recorded destination",
            action=ActionKind.DRAG,
            drag_end_anchor=Anchor(
                template="end.png",
                region=(0, 0, 1, 1),
                click_point=(0, 0),
                structural=StructuralLocator(name="Delete"),
            ),
        )
        right_click = _click_step("right-click Delete", "Delete")
        right_click.action = ActionKind.RIGHT_CLICK

        assert infer_step_risk(drag).risk == "irreversible"
        assert infer_step_risk(right_click).risk == "irreversible"

    def test_application_rule_resolves_ambiguous_control_but_not_builtin_write(
        self,
    ) -> None:
        icon = Step(
            id="icon",
            intent="click recorded target",
            action=ActionKind.CLICK,
            anchor=Anchor(
                template="t.png",
                region=(0, 0, 1, 1),
                click_point=(0, 0),
                structural=StructuralLocator(window_name="Accuro"),
            ),
        )
        rule = ApplicationRiskRule(
            rule_id="accuro.open_chart",
            risk="reversible",
            explanation="the configured icon opens the selected chart",
            actions=(ActionKind.CLICK,),
            window_names=("Accuro",),
        )

        inference = infer_step_risk(icon, application_rules=(rule,))

        assert inference.risk == "reversible"
        assert inference.requires_review is False
        assert (
            infer_step_risk(
                _click_step("click Save", "Save"), application_rules=(rule,)
            ).risk
            == "irreversible"
        )


# --- end-to-end through the compiler ---------------------------------------


def _blank() -> np.ndarray:
    return np.full((VIEWPORT[1], VIEWPORT[0], 3), 245, dtype=np.uint8)


def _draw_button(img: np.ndarray, x: int, y: int, w: int, h: int, label: str) -> None:
    cv2.rectangle(img, (x, y), (x + w, y + h), (205, 205, 205), -1)
    cv2.rectangle(img, (x, y), (x + w, y + h), (70, 70, 70), 2)
    cv2.putText(
        img,
        label,
        (x + 12, y + h // 2 + 8),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )


def _write_frame(recording: Path, i: int, suffix: str, img: np.ndarray) -> None:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    (recording / "frames" / f"{i:04d}_{suffix}.png").write_bytes(buf.tobytes())


@pytest.fixture(scope="module")
def two_button_bundle(tmp_path_factory):
    """A 2-click recording: a benign 'Search' button then a 'Save' button."""
    recording = tmp_path_factory.mktemp("rec")
    bundle = tmp_path_factory.mktemp("bundle")
    (recording / "frames").mkdir()

    screen0 = _blank()
    _draw_button(screen0, 560, 400, 200, 48, "Search")
    screen1 = screen0.copy()
    cv2.putText(
        screen1,
        "Results loaded",
        (400, 244),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )
    _draw_button(screen1, 560, 500, 200, 48, "Save")
    screen2 = screen1.copy()
    cv2.putText(
        screen2,
        "Encounter Saved OK",
        (400, 620),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )

    events = [
        {"i": 0, "kind": "click", "x": 660, "y": 424, "t": 1.0},  # Search
        {"i": 1, "kind": "click", "x": 660, "y": 524, "t": 2.0},  # Save
    ]
    for i, (before, after) in {0: (screen0, screen1), 1: (screen1, screen2)}.items():
        _write_frame(recording, i, "before", before)
        _write_frame(recording, i, "after", after)
    (recording / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "id": "rec-risk",
                "created_at": "2026-07-06T00:00:00+00:00",
                "viewport": list(VIEWPORT),
                "params": {},
            }
        )
    )
    return {"recording": recording, "bundle": bundle}


def test_compiler_auto_classifies_write_step(two_button_bundle, tmp_path):
    wf = compile_recording(
        two_button_bundle["recording"], tmp_path / "b", name="risk-e2e"
    )
    by_id = {s.id: s for s in wf.steps}
    # step_000 clicks 'Search' (benign), step_001 clicks 'Save' (write).
    assert by_id["step_000"].risk == "reversible", by_id["step_000"].intent
    assert by_id["step_001"].risk == "irreversible", by_id["step_001"].intent


def test_compiler_uses_complete_full_frame_label_for_a_wide_write_control(
    tmp_path: Path,
) -> None:
    recording = tmp_path / "wide-recording"
    bundle = tmp_path / "wide-bundle"
    (recording / "frames").mkdir(parents=True)

    before = _blank()
    _draw_button(before, 550, 570, 400, 64, "Save appointment")
    after = before.copy()
    cv2.putText(
        after,
        "Appointment saved",
        (550, 680),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )
    _write_frame(recording, 0, "before", before)
    _write_frame(recording, 0, "after", after)
    (recording / "events.jsonl").write_text(
        json.dumps({"i": 0, "kind": "click", "x": 720, "y": 600, "t": 1.0}) + "\n"
    )
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "id": "rec-wide-write",
                "created_at": "2026-07-30T00:00:00+00:00",
                "viewport": list(VIEWPORT),
                "params": {},
            }
        )
    )

    workflow = compile_recording(recording, bundle, name="wide-write")

    assert workflow.steps[0].anchor is not None
    assert workflow.steps[0].anchor.ocr_text == "Save appointment"
    assert workflow.steps[0].risk == "irreversible"


def test_risk_overrides_still_win_both_directions(two_button_bundle, tmp_path):
    wf = compile_recording(
        two_button_bundle["recording"],
        tmp_path / "b2",
        name="risk-e2e",
        risk_overrides={"step_000": "irreversible", "step_001": "reversible"},
    )
    by_id = {s.id: s for s in wf.steps}
    # Overrides flip BOTH the auto-reversible and the auto-irreversible step.
    assert by_id["step_000"].risk == "irreversible"
    assert by_id["step_001"].risk == "reversible"
    assert all(not step.risk_review_required for step in by_id.values())
