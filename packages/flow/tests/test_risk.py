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
from openadapt_flow.ir import ActionKind, Anchor, Step
from openadapt_flow.risk import classify_step_risk, is_write_shaped

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
            "Next",
            "Open chart",
            "Cancel",
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

    def test_only_clicks_can_be_irreversible(self) -> None:
        # A TYPE step's text is write-shaped, but typing is reversible: only
        # CLICK/DOUBLE_CLICK actuators can classify irreversible.
        typing = Step(id="s", intent="type 'save the world'", action=ActionKind.TYPE)
        assert classify_step_risk(typing) == "reversible"
        key = Step(id="s", intent="press Enter", action=ActionKind.KEY, key="Enter")
        assert classify_step_risk(key) == "reversible"

    def test_unlabelled_coordinate_click_is_reversible(self) -> None:
        # No signal -> stays reversible (we do not fabricate risk).
        step = Step(
            id="s",
            intent="click at (10, 12)",
            action=ActionKind.CLICK,
            anchor=Anchor(template="t.png", region=(0, 0, 1, 1), click_point=(10, 12)),
        )
        assert classify_step_risk(step) == "reversible"


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
