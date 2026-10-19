"""Extra looks on the real replayer path, real render and real OCR.

A record row whose identifier carries look-alike glyphs (``AC50061``). OCR
abstains on it. The select-and-copy look settles the record only when the
copied identifier equals the run's parameter; the look-alike twin
``AC5OO61`` stops the run. The actuation-time check reuses the step-entry
copy only when the identifier cell shows the same pixels.
"""

from __future__ import annotations

import io
from datetime import date

import pytest

pytest.importorskip("playwright")
pytest.importorskip("rapidocr_onnxruntime")

from PIL import Image, ImageDraw  # noqa: E402

import openadapt_flow.vision as vision  # noqa: E402
from openadapt_flow.ir import (  # noqa: E402
    Anchor,
    IdentityCheck,
    Resolution,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.runtime import identity as I  # noqa: E402
from openadapt_flow.runtime.identity_looks import IdentityCopyConfig  # noqa: E402
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402
from tests.test_identity_homonym_8th import _ROW_K, _render  # noqa: E402

_FRAMES: dict[str, object] = {}


def _frame(mrn: str):
    if mrn not in _FRAMES:
        _FRAMES[mrn] = _render(mrn)
    return _FRAMES[mrn]


def _identifier_region(frame) -> tuple[int, int, int, int]:
    """The MRN + name + DOB cells of the row under test."""
    _, _, _, row = frame.points[_ROW_K]
    lines = sorted(vision.ocr(frame.png, region=row), key=lambda ln: ln.region[0])
    mrn = next(ln for ln in lines if "AC5" in ln.text.upper())
    dob = next(ln for ln in lines if "1980" in ln.text)
    x0 = mrn.region[0] - 4
    x1 = dob.region[0] + dob.region[2] + 4
    return (x0, row[1], x1 - x0, row[3])


class _Clipboard:
    def __init__(self) -> None:
        self.text = "operator's clipboard"

    def read_text(self):
        return self.text

    def write_text(self, text):
        self.text = text


def _highlight(png: bytes, region) -> bytes:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    x, y, w, h = region
    draw = ImageDraw.Draw(img)
    draw.rectangle((x + 2, y + 2, x + w - 3, y + 4), fill=(51, 144, 255))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


class _CopyBackend:
    """Pixel-only session whose identifier cell can be selected and copied."""

    def __init__(self, frame, mrn: str, clipboard: _Clipboard, *, spill=False):
        self.viewport = frame.viewport
        self.frame = frame.png
        self.region = _identifier_region(frame)
        self.copy_text = mrn  # the drag spans the identifier's own text box
        self.clipboard = clipboard
        self.spill = spill
        self.drags: list[tuple[int, int, int, int]] = []
        self.selected = False

    def screenshot(self) -> bytes:
        return self.frame

    def drag(self, x, y, ex, ey):
        self.drags.append((x, y, ex, ey))
        rx, ry, rw, rh = self.region
        self.selected = ry <= y <= ry + rh
        if self.selected:  # a selection paints between the drag's ends
            self.frame = _highlight(self.frame, (x, y - 8, ex - x, 16))
        if self.spill:  # the gesture opened something elsewhere on screen
            self.frame = _highlight(self.frame, (5, 5, 60, 20))

    def press(self, key):
        assert key == "Control+c"
        if self.selected:
            self.clipboard.text = self.copy_text


def _setup(live_mrn: str, *, param_mode: bool, spill=False, copy=True):
    rec = _frame("AC50061")
    live = _frame(live_mrn)
    _, open_pt, _, _ = rec.points[_ROW_K]
    _, live_pt, _, _ = live.points[_ROW_K]
    region = _identifier_region(rec)
    context = I.identifier_text_from_lines(
        vision.ocr(rec.png), region=region, reference_date=date.today()
    )
    assert context and "1980" in context
    anchor = Anchor(
        template="t.png",
        region=(open_pt[0] - 30, open_pt[1] - 12, 60, 24),
        click_point=(open_pt[0], open_pt[1]),
        context_text=context,
        identifier_region=region,
    )
    step = Step(
        id="open_record",
        intent="open record",
        action="click",
        anchor=anchor,
        risk="irreversible",
    )
    params = {"mrn": "AC50061"} if param_mode else {}
    wf = Workflow(name="looks", params=dict(params), steps=[step])
    res = Resolution(rung="ocr", point=live_pt, confidence=0.9, elapsed_ms=1.0)
    clip = _Clipboard()
    backend = _CopyBackend(live, live_mrn, clip, spill=spill)
    replayer = Replayer(
        backend,
        vision=vision,
        read_clipboard=clip if copy else None,
        identity_copy=IdentityCopyConfig() if copy else None,
    )
    return replayer, backend, step, res, wf, params, live


def _entry(replayer, step, res, wf, params, png) -> IdentityCheck:
    replayer._identity_input_looks = True
    try:
        return replayer._verify_identity(step, res, png, params, wf, bundle_dir=None)
    finally:
        replayer._identity_input_looks = False


def test_without_copy_look_the_correct_row_still_abstains():
    r, b, step, res, wf, params, live = _setup("AC50061", param_mode=True, copy=False)
    check = _entry(r, step, res, wf, params, live.png)
    assert check.status == "abstain"
    assert not b.drags


def test_copy_equal_to_the_run_value_verifies_the_correct_row():
    r, b, step, res, wf, params, live = _setup("AC50061", param_mode=True)
    check = _entry(r, step, res, wf, params, live.png)
    assert check.status == "verified", check
    kinds = [(look.kind, look.outcome) for look in check.looks]
    assert ("clipboard", "resolves") in kinds
    assert [k for k, _ in kinds].count("frame") == 2
    assert len(b.drags) == 1
    assert b.clipboard.text == "operator's clipboard"


def test_copy_of_the_look_alike_twin_stops_the_run():
    r, b, step, res, wf, params, live = _setup("AC5OO61", param_mode=True)
    check = _entry(r, step, res, wf, params, live.png)
    assert check.status == "mismatch"
    assert check.looks[-1].kind == "clipboard"
    assert check.looks[-1].outcome == "contradicts"


def test_recorded_record_mode_copy_cannot_accept():
    r, b, step, res, wf, params, live = _setup("AC50061", param_mode=False)
    check = _entry(r, step, res, wf, params, live.png)
    assert check.status == "abstain"
    assert check.looks[-1].kind == "clipboard"
    assert check.looks[-1].outcome == "agrees"


def test_screen_change_outside_the_cell_halts(tmp_path):
    r, b, step, res, wf, params, live = _setup("AC50061", param_mode=True, spill=True)
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)
    error = r._entry_identity_gate_error(
        step, res, live.png, params, wf, tmp_path, result
    )
    assert result.identity is not None
    assert result.identity.look_halt == "identity_look_changed_screen"
    assert error is not None and "identity_look_changed_screen" in error
    assert result.safety_halt is True


def test_governed_runs_take_no_copy_look():
    r, b, step, res, wf, params, live = _setup("AC50061", param_mode=True)
    r.governed_authorization = object()  # type: ignore[assignment]
    check = _entry(r, step, res, wf, params, live.png)
    assert check.status == "abstain"
    assert not b.drags


def test_actuation_check_reuses_the_copy_only_on_matching_pixels():
    r, b, step, res, wf, params, live = _setup("AC50061", param_mode=True)
    assert _entry(r, step, res, wf, params, live.png).status == "verified"
    # Actuation-time check: no input, the selection highlight is still drawn.
    again = r._verify_identity(step, res, b.frame, params, wf, bundle_dir=None)
    assert len(b.drags) == 1
    assert again.status == "verified"
    assert ("clipboard", "resolves") in [(x.kind, x.outcome) for x in again.looks]
    # The twin now sits where the record was: the binding must not carry over.
    twin = _frame("AC5OO61")
    b.frame = twin.png
    swapped = r._verify_identity(step, res, twin.png, params, wf, bundle_dir=None)
    assert swapped.status != "verified"
    assert len(b.drags) == 1
