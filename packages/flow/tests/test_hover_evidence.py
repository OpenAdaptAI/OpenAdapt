"""Hover-before-click evidence (runtime.hover_evidence) and its wiring.

Synthetic frames: an icon button that lightens under the pointer and opens a
tooltip. Two icons look the same and differ only by their tooltip.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw

from openadapt_flow.ir import (
    Anchor,
    HoverEvidence,
    Resolution,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.runtime import hover_evidence as H
from openadapt_flow.runtime.replayer import Replayer

ICON = (100, 100, 40, 40)
POINT = (120, 120)


def _frame(
    pointer: Optional[tuple[int, int]] = None, tooltip: str = "Save note"
) -> bytes:
    img = Image.new("RGB", (480, 320), (244, 246, 248))
    d = ImageDraw.Draw(img)
    x, y, w, h = ICON
    over = pointer is not None and x <= pointer[0] < x + w and y <= pointer[1] < y + h
    d.rectangle(
        (x, y, x + w - 1, y + h - 1), fill=(90, 140, 230) if over else (45, 111, 216)
    )
    d.ellipse((x + 10, y + 10, x + 30, y + 30), fill="white")
    if over and tooltip:
        tx, ty = pointer[0] + 12, pointer[1] + 20
        d.rectangle(
            (tx, ty, tx + 9 * len(tooltip), ty + 18),
            fill=(255, 255, 225),
            outline="black",
        )
        d.text((tx + 4, ty + 3), tooltip, fill="black")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def test_measure_response_finds_highlight_and_tooltip():
    r = H.measure_response(_frame(), _frame(POINT), POINT)
    assert r is not None
    x, y, w, h = r.region
    assert x <= ICON[0] and y <= ICON[1]
    assert x + w > POINT[0] + 12 and y + h > POINT[1] + 20  # tooltip included
    assert H.measure_response(_frame(), _frame(), POINT) is None


def _recorded(tooltip: str = "Save note"):
    r = H.measure_response(_frame(), _frame(POINT, tooltip), POINT)
    assert r is not None
    return H.relative(r.region, POINT), r.crop


def _compare(live_tooltip: Optional[str], *, same_render=True, recorded_response=True):
    region, crop = _recorded()
    live = (
        H.measure_response(_frame(), _frame(POINT, live_tooltip), POINT)
        if live_tooltip is not None
        else None
    )
    return H.compare(
        recorded_response=recorded_response,
        recorded_region=region if recorded_response else None,
        recorded_crop=crop if recorded_response else None,
        live=live,
        point=POINT,
        same_render=same_render,
    )


def test_same_response_verifies():
    assert _compare("Save note") == ("verified", "same_pixels")


def test_other_tooltip_on_a_look_alike_icon_is_a_mismatch():
    verdict, _ = _compare("Delete note")
    assert verdict == "mismatch"


def test_no_live_response_is_absent_not_a_stop():
    assert _compare(None)[0] == "absent"


def test_response_where_none_was_recorded_is_unexpected():
    assert _compare("Save note", recorded_response=False)[0] == "unexpected"
    assert _compare(None, recorded_response=False)[0] == "unverifiable"


def test_moved_response_is_a_mismatch():
    region, crop = _recorded()
    far = H.Response(region=(300, 250, 40, 20), crop=crop, changed_px=100)
    verdict, reason = H.compare(
        recorded_response=True,
        recorded_region=region,
        recorded_crop=crop,
        live=far,
        point=POINT,
        same_render=True,
    )
    assert verdict == "mismatch" and reason == "response_moved_or_resized"


def test_other_render_without_text_is_unverifiable():
    verdict, _ = _compare("Save note", same_render=False)
    assert verdict == "unverifiable"


def test_same_text_redrawn_by_another_rasterizer_verifies():
    class Line:
        def __init__(self, text):
            self.text = text

    region, crop = _recorded()
    live = H.measure_response(_frame(), _frame(POINT, "Save note"), POINT)
    assert live is not None
    blurred = H.Response(
        region=live.region,
        crop=_invert(live.crop),
        changed_px=live.changed_px,
    )
    verdict, reason = H.compare(
        recorded_response=True,
        recorded_region=region,
        recorded_crop=crop,
        live=blurred,
        point=POINT,
        same_render=True,
        ocr=lambda png: [Line("Save note")],
    )
    assert (verdict, reason) == ("verified", "same_text")


def _invert(png: bytes) -> bytes:
    from PIL import ImageOps

    img = ImageOps.invert(Image.open(io.BytesIO(png)).convert("RGB"))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def test_other_render_compares_ocr_text():
    class Line:
        def __init__(self, text):
            self.text = text

    region, crop = _recorded()
    live = H.measure_response(_frame(), _frame(POINT, "Delete note"), POINT)
    texts = iter(["Save note", "Delete note"])

    def ocr(png):
        return [Line(next(texts))]

    verdict, reason = H.compare(
        recorded_response=True,
        recorded_region=region,
        recorded_crop=crop,
        live=live,
        point=POINT,
        same_render=False,
        ocr=ocr,
    )
    assert (verdict, reason) == ("mismatch", "different_text")


# ------------------------------------------------------------- replayer


class _HoverBackend:
    viewport = (480, 320)

    def __init__(self, tooltip: str = "Save note", can_hover: bool = True):
        self.tooltip = tooltip
        self.pointer: Optional[tuple[int, int]] = None
        self.hovers: list[tuple[int, int]] = []
        if not can_hover:
            self.hover = None  # type: ignore[assignment]

    def screenshot(self) -> bytes:
        return _frame(self.pointer, self.tooltip)

    def hover(self, x, y):  # noqa: D401 - backend seam
        self.hovers.append((x, y))
        self.pointer = (x, y)


class _Vision:
    @staticmethod
    def wait_settled(backend):
        return backend.screenshot()

    @staticmethod
    def ocr(png, region=None):
        return []


def _bundle(tmp_path: Path) -> tuple[Path, Step, Workflow]:
    region, crop = _recorded()
    (tmp_path / "templates" / "hover").mkdir(parents=True)
    (tmp_path / "templates" / "hover" / "step_000.png").write_bytes(crop)
    step = Step(
        id="step_000",
        intent="click recorded visual target",
        action="click",
        anchor=Anchor(template="t.png", region=ICON, click_point=POINT),
        risk="irreversible",
        hover=HoverEvidence(
            response=True,
            region=region,
            crop="templates/hover/step_000.png",
            dwell_ms=0,
        ),
    )
    return tmp_path, step, Workflow(name="hover", params={}, steps=[step])


def _gate(tmp_path, backend, *, entry=True, rung="template"):
    bundle, step, wf = _bundle(tmp_path)
    replayer = Replayer(backend, vision=_Vision())
    res = Resolution(rung=rung, point=POINT, confidence=0.9, elapsed_ms=1.0)
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)
    call = (
        replayer._entry_identity_gate_error if entry else replayer._identity_gate_error
    )
    error = call(step, res, _frame(), {}, wf, bundle, result)
    return error, result


def test_same_hover_response_verifies_the_icon(tmp_path):
    error, result = _gate(tmp_path, _HoverBackend("Save note"))
    assert error is None
    assert result.identity is not None and result.identity.mode == "hover"
    assert result.identity.status == "verified"
    assert result.identity.hover is not None
    assert result.identity.hover.verdict == "verified"


def test_look_alike_icon_with_other_tooltip_stops_before_the_click(tmp_path):
    backend = _HoverBackend("Delete note")
    error, result = _gate(tmp_path, backend)
    assert error is not None and "refusing to click" in error
    assert result.identity is not None and result.identity.status == "mismatch"
    assert backend.hovers == [POINT]


def test_missing_tooltip_changes_the_response_and_stops(tmp_path):
    # A highlight with no tooltip (the tooltip opened late) is another shape.
    error, result = _gate(tmp_path, _HoverBackend(""))
    assert result.identity is not None and result.identity.status == "mismatch"
    assert result.identity.hover.reason == "response_moved_or_resized"
    assert error is not None


def test_absent_response_is_recorded_and_does_not_stop(tmp_path):
    class Frozen(_HoverBackend):
        def screenshot(self):
            return _frame()

    error, result = _gate(tmp_path, Frozen())
    assert error is None
    assert result.identity is not None and result.identity.status == "abstain"
    assert result.identity.hover.verdict == "absent"


def test_actuation_check_takes_no_hover(tmp_path):
    backend = _HoverBackend("Delete note")
    error, result = _gate(tmp_path, backend, entry=False)
    assert error is None and result.identity is None
    assert backend.hovers == []


def test_structural_resolution_takes_no_hover(tmp_path):
    backend = _HoverBackend("Delete note")
    error, result = _gate(tmp_path, backend, rung="structural")
    assert error is None and backend.hovers == []


def test_backend_without_hover_keeps_old_behaviour(tmp_path):
    backend = _HoverBackend("Delete note", can_hover=False)
    error, result = _gate(tmp_path, backend)
    assert error is None and result.identity is None


# -------------------------------------------------------------- compiler


def test_compiler_keeps_the_hover_response(tmp_path):
    from openadapt_flow.compiler.compile import _hover_evidence

    hover_path = tmp_path / "0000_hover.png"
    hover_path.write_bytes(_frame(POINT))
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    ev = _hover_evidence(bundle, "step_000", _frame(), hover_path, POINT, dwell_ms=800)
    assert ev is not None and ev.response and ev.crop == "templates/hover/step_000.png"
    assert (bundle / ev.crop).is_file()
    hover_path.write_bytes(_frame())
    assert _hover_evidence(bundle, "step_001", _frame(), hover_path, POINT) is None
    assert (
        _hover_evidence(bundle, "s", _frame(), tmp_path / "missing.png", POINT) is None
    )


def test_recorder_saves_a_hover_frame_before_the_click(tmp_path):
    from openadapt_flow.recorder import Recorder

    class Backend(_HoverBackend):
        def click(self, x, y, *, double=False):
            self.pointer = (x, y)

    backend = Backend()
    rec = Recorder(backend, tmp_path / "rec", capture_hover=True, settle_timeout_s=0.2)
    rec.click(*POINT)
    events = [
        json.loads(line)
        for line in (tmp_path / "rec" / "events.jsonl").read_text().splitlines()
    ]
    assert events[0]["hover_dwell_ms"] == H.HOVER_DWELL_MS
    assert (tmp_path / "rec" / "frames" / "0000_hover.png").is_file()
    assert backend.hovers == [POINT]


def test_recorder_without_capture_hover_does_not_hover(tmp_path):
    from openadapt_flow.recorder import Recorder

    class Backend(_HoverBackend):
        def click(self, x, y, *, double=False):
            self.pointer = (x, y)

    backend = Backend()
    rec = Recorder(backend, tmp_path / "rec", settle_timeout_s=0.2)
    rec.click(*POINT)
    assert backend.hovers == []
    assert not (tmp_path / "rec" / "frames" / "0000_hover.png").exists()
