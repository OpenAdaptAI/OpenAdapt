"""Structured-text identity tier + the extensible identity ladder.

Covers the escape from the OCR glyph-collapse impossibility result: identity
verified against STRUCTURED text (DOM / a11y) where the backend exposes it,
falling back to the OCR name+DOB-primary tier only on pixel-only substrates.

Fast unit tests (no browser/OCR) cover the string tier and the ladder; the
PlaywrightBackend DOM path is guarded by ``importorskip``. The OCR fallback
tier is exercised via the Replayer with a faked backend/vision.
"""

from __future__ import annotations

import pytest

from openadapt_flow.backend import Backend, IdentityBackend
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    IdentityCheck,
    Resolution,
    Step,
    Workflow,
)
from openadapt_flow.runtime.identity import (
    normalize_structured,
    run_identity_ladder,
    structured_identity_match,
    verify_structured_identity,
)

# ---------------------------------------------------------------------------
# normalize_structured: O/0 stay DISTINCT (that is the whole point)
# ---------------------------------------------------------------------------


def test_normalize_collapses_whitespace_and_casefolds() -> None:
    assert normalize_structured("  MG4408   Okafor,  Philip ") == (
        "mg4408 okafor, philip"
    )


def test_normalize_does_not_fold_o_zero_or_l_one() -> None:
    # If normalization folded OCR confusion classes it would re-open the very
    # class the structured tier closes.
    assert normalize_structured("MG44O8") != normalize_structured("MG4408")
    assert normalize_structured("PL1234") != normalize_structured("PLl234")


# ---------------------------------------------------------------------------
# structured_identity_match / the exact review probes
# ---------------------------------------------------------------------------


def test_same_row_reread_matches() -> None:
    rec = "MG4408 Okafor, Philip 1966-01-17 M Active"
    assert structured_identity_match(
        rec, "  mg4408 okafor, philip 1966-01-17 m active "
    )


@pytest.mark.parametrize(
    "target,sibling",
    [
        ("MG4408", "MG44O8"),  # digit 0 vs letter O, digit-flanked
        ("AC50061", "AC5OO61"),  # the review's second probe
        ("PL1234", "PLl234"),  # digit 1 vs letter l
    ],
)
def test_digit_flanked_siblings_mismatch(target, sibling) -> None:
    # Same name+DOB, MRN one glyph apart: OCR collapses these to one string,
    # the DOM does not -> structured compare MUST mismatch.
    rec = f"{target} Okafor, Philip 1966-01-17 M Active"
    live = f"{sibling} Okafor, Philip 1966-01-17 M Active"
    assert not structured_identity_match(rec, live)


# ---------------------------------------------------------------------------
# verify_structured_identity: verdicts + availability
# ---------------------------------------------------------------------------


def test_verify_structured_verified_and_mismatch() -> None:
    assert verify_structured_identity("MG4408 X", "MG4408 X").status == "verified"
    v = verify_structured_identity("MG4408 X", "MG44O8 X")
    assert v is not None and v.status == "mismatch"
    assert v.mode == "structured"


@pytest.mark.parametrize(
    "rec,live",
    [
        (None, "MG4408"),
        ("MG4408", None),
        (None, None),
        ("", "MG4408"),
    ],
)
def test_verify_structured_unavailable_when_either_side_missing(rec, live) -> None:
    # Unavailable (None) => ladder falls through to the next tier.
    assert verify_structured_identity(rec, live) is None


# ---------------------------------------------------------------------------
# run_identity_ladder: first definitive verdict wins; mismatch is final
# ---------------------------------------------------------------------------


def _verified():
    return IdentityCheck(status="verified", mode="structured")


def _mismatch():
    return IdentityCheck(status="mismatch", mode="structured")


def test_ladder_first_non_none_wins() -> None:
    called = []

    def t1():
        called.append("t1")
        return _verified()

    def t2():
        called.append("t2")
        return IdentityCheck(status="mismatch")

    out = run_identity_ladder([t1, t2])
    assert out.status == "verified"
    assert called == ["t1"]  # t2 never consulted


def test_ladder_falls_through_unavailable_tier() -> None:
    out = run_identity_ladder([lambda: None, lambda: IdentityCheck(status="verified")])
    assert out.status == "verified"


def test_ladder_ocr_never_overrides_structured_mismatch() -> None:
    # Structured tier says mismatch; an OCR tier that WOULD verify must never
    # run (higher tier's verdict is final).
    ocr_ran = []

    def ocr():
        ocr_ran.append(True)
        return IdentityCheck(status="verified")

    out = run_identity_ladder([_mismatch, ocr])
    assert out.status == "mismatch"
    assert not ocr_ran


def test_ladder_all_unavailable_is_unreadable() -> None:
    assert run_identity_ladder([lambda: None, lambda: None]).status == "unreadable"


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_identity_backend_runtime_checkable() -> None:
    class HasIt:
        def structured_text_at(self, x, y):
            return None

    class Lacks:
        pass

    assert isinstance(HasIt(), IdentityBackend)
    assert not isinstance(Lacks(), IdentityBackend)


# ---------------------------------------------------------------------------
# WindowsBackend UIA structured text (fake WAA session)
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp
        self.posted = []

    def post(self, url, json=None, timeout=None):
        self.posted.append(json)
        if url.endswith("/execute_windows"):
            return self._resp
        return _Resp(status_code=404, text="not found")


def _win_backend(resp):
    from openadapt_flow.backends.windows_backend import WindowsBackend

    return WindowsBackend(
        session=_FakeSession(resp), viewport=(800, 600), allow_legacy_exec=True
    )


def test_windows_structured_text_returns_echoed_uia_text() -> None:
    # Server echoes the sentinel-wrapped JSON payload -> real text returned.
    payload = '<<OAFLOW_STRUCTURED>>"MG4408 Okafor, Philip"<<END_OAFLOW_STRUCTURED>>'
    be = _win_backend(_Resp(text="some log\n" + payload + "\ntrailing"))
    assert be.structured_text_at(100, 200) == "MG4408 Okafor, Philip"


def test_windows_structured_text_none_when_no_echo() -> None:
    # Older WAA server that does not return command output -> None (fallback
    # to the OCR tier preserved; pinned behavior).
    be = _win_backend(_Resp(text=""))
    assert be.structured_text_at(100, 200) is None


def test_windows_structured_text_none_on_uia_null() -> None:
    payload = "<<OAFLOW_STRUCTURED>>null<<END_OAFLOW_STRUCTURED>>"
    be = _win_backend(_Resp(text=payload))
    assert be.structured_text_at(100, 200) is None


def test_windows_structured_text_json_envelope() -> None:
    payload = '<<OAFLOW_STRUCTURED>>"ROW TEXT"<<END_OAFLOW_STRUCTURED>>'
    be = _win_backend(_Resp(text="", json_data={"output": payload}))
    assert be.structured_text_at(1, 1) == "ROW TEXT"


# ---------------------------------------------------------------------------
# PlaywrightBackend DOM structured text (end-to-end, importorskip)
# ---------------------------------------------------------------------------

_TWO_ROW_HTML = (
    "<!doctype html><html><body><table><tbody>"
    "<tr data-row='0'><td>MG4408</td><td>Okafor, Philip</td>"
    "<td>1966-01-17</td></tr>"
    "<tr data-row='1'><td>MG44O8</td><td>Okafor, Philip</td>"
    "<td>1966-01-17</td></tr>"
    "</tbody></table></body></html>"
)


def test_playwright_structured_text_distinguishes_glyph_siblings() -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 800, "height": 400}, device_scale_factor=1
        )
        page.set_content(_TWO_ROW_HTML, wait_until="networkidle")
        be = PlaywrightBackend(page)
        boxes = {}
        for i in (0, 1):
            bb = page.eval_on_selector(
                f"[data-row='{i}']",
                "el => { const r = el.getBoundingClientRect();"
                " return [r.x + r.width/2, r.y + r.height/2]; }",
            )
            boxes[i] = bb
        t0 = be.structured_text_at(int(boxes[0][0]), int(boxes[0][1]))
        t1 = be.structured_text_at(int(boxes[1][0]), int(boxes[1][1]))
        off = be.structured_text_at(5, 380)
        browser.close()

    assert t0 and "MG4408" in t0
    assert t1 and "MG44O8" in t1
    # The digit-0 row and the letter-O row are DIFFERENT strings in the DOM.
    assert not structured_identity_match(t0, t1)
    assert isinstance(be, IdentityBackend) and isinstance(be, Backend)
    # off-target point returns text of whatever/None, but must never raise.
    assert off is None or isinstance(off, str)


# ---------------------------------------------------------------------------
# Replayer identity ladder wiring (_verify_identity)
# ---------------------------------------------------------------------------

import io as _io  # noqa: E402

from PIL import Image as _Image  # noqa: E402

from openadapt_flow.runtime.replayer import Replayer  # noqa: E402

_VP = (300, 200)


def _png(size=_VP, color=(240, 240, 240)) -> bytes:
    buf = _io.BytesIO()
    _Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class _FakeVision:
    def __init__(self, ocr_lines=None):
        self.ocr_lines = ocr_lines or []
        self.ocr_calls = 0

    def ocr(self, png, *, region=None):
        self.ocr_calls += 1
        return self.ocr_lines

    def wait_settled(self, backend, **kw):
        return backend.screenshot()


class _PixelBackend:
    """Pure-pixel backend: no structured_text_at (OCR fallback only)."""

    def __init__(self):
        self._f = _png()
        self.actions = []

    @property
    def viewport(self):
        return _VP

    def screenshot(self):
        return self._f

    def click(self, x, y, *, double=False):
        self.actions.append((x, y))

    def type_text(self, text): ...

    def press(self, key): ...

    def scroll(self, dx, dy): ...


class _StructuredBackend(_PixelBackend):
    """Backend that exposes structured_text_at (DOM/a11y)."""

    def __init__(self, text):
        super().__init__()
        self._text = text
        self.queried = []

    def structured_text_at(self, x, y):
        self.queried.append((x, y))
        return self._text


def _step(structured_identity=None, context_text=None):
    return Step(
        id="s1",
        intent="click row",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/x.png",
            region=(100, 100, 50, 20),
            click_point=(110, 105),
            ocr_text="Open",
            context_text=context_text,
            structured_identity=structured_identity,
        ),
    )


def _resolution():
    return Resolution(rung="template", point=(110, 105), confidence=0.9, elapsed_ms=1.0)


def test_replayer_structured_tier_verifies_and_skips_ocr() -> None:
    rec = "MG4408 Okafor, Philip 1966-01-17"
    backend = _StructuredBackend(rec)
    vision = _FakeVision()  # ocr would be used only by the OCR tier
    rp = Replayer(backend, vision=vision, poll_interval_s=0.01)
    check = rp._verify_identity(
        _step(structured_identity=rec, context_text="ignored band text here"),
        _resolution(),
        backend.screenshot(),
        {},
        None,
    )
    assert check.status == "verified"
    assert check.mode == "structured"
    assert vision.ocr_calls == 0  # OCR tier never consulted
    assert backend.queried == [(110, 105)]


def test_replayer_structured_mismatch_halts_and_not_overridden() -> None:
    rec = "MG4408 Okafor, Philip 1966-01-17"
    live = "MG44O8 Okafor, Philip 1966-01-17"  # sibling glyph-collapse
    backend = _StructuredBackend(live)
    vision = _FakeVision()
    rp = Replayer(backend, vision=vision, poll_interval_s=0.01)
    check = rp._verify_identity(
        _step(structured_identity=rec, context_text="band"),
        _resolution(),
        backend.screenshot(),
        {},
        None,
    )
    assert check.status == "mismatch"
    assert check.mode == "structured"
    assert vision.ocr_calls == 0  # OCR must NOT override a structured mismatch


def test_replayer_falls_back_to_ocr_when_structured_unavailable() -> None:
    # Backend exposes structured_text_at but returns None at this point
    # (pixel-only region); bundle has structured_identity -> tier unavailable,
    # ladder falls through to the OCR tier (which reads the band via vision).
    backend = _StructuredBackend(None)
    vision = _FakeVision(ocr_lines=[])  # empty band -> unreadable
    rp = Replayer(backend, vision=vision, poll_interval_s=0.01)
    check = rp._verify_identity(
        _step(
            structured_identity="MG4408 X 1966-01-17",
            context_text="MG4408 X 1966-01-17",
        ),
        _resolution(),
        backend.screenshot(),
        {},
        Workflow(name="wf"),
    )
    assert vision.ocr_calls > 0  # OCR tier WAS consulted
    assert check.mode != "structured"  # verdict came from the OCR tier


def test_replayer_pixel_backend_uses_ocr_tier_only() -> None:
    # No structured_text_at at all (Citrix/VDI-style pixel backend).
    backend = _PixelBackend()
    vision = _FakeVision(ocr_lines=[])
    rp = Replayer(backend, vision=vision, poll_interval_s=0.01)
    check = rp._verify_identity(
        _step(
            structured_identity="MG4408 X 1966-01-17",
            context_text="MG4408 X 1966-01-17",
        ),
        _resolution(),
        backend.screenshot(),
        {},
        Workflow(name="wf"),
    )
    assert vision.ocr_calls > 0
    assert check.mode != "structured"


# ---------------------------------------------------------------------------
# Recorder captures structured identity into the event (record time)
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from openadapt_flow.recorder import Recorder  # noqa: E402


class _RecStructuredBackend(_PixelBackend):
    def __init__(self, text):
        super().__init__()
        self._text = text
        # Recorder expects a full-size frame; reuse 1280x800.
        self._f = _png((1280, 800))

    @property
    def viewport(self):
        return (1280, 800)

    def structured_text_at(self, x, y):
        return self._text


def _events(rec_dir: _Path):
    return [
        _json.loads(ln) for ln in (rec_dir / "events.jsonl").read_text().splitlines()
    ]


def test_recorder_captures_structured_identity_on_click(tmp_path) -> None:
    be = _RecStructuredBackend("MG4408 Okafor, Philip 1966-01-17")
    rec = Recorder(be, tmp_path / "rec")
    rec.click(10, 20)
    rec.type_text("hello")  # non-pointer: no structured_identity
    rec_dir = rec.finish()
    evs = _events(rec_dir)
    click_ev = next(e for e in evs if e["kind"] == "click")
    assert click_ev["structured_identity"] == "MG4408 Okafor, Philip 1966-01-17"
    type_ev = next(e for e in evs if e["kind"] == "type")
    assert "structured_identity" not in type_ev


def test_recorder_pixel_backend_omits_structured_identity(tmp_path) -> None:
    rec = Recorder(_PixelBackendFull(), tmp_path / "rec2")
    rec.click(10, 20)
    rec_dir = rec.finish()
    click_ev = next(e for e in _events(rec_dir) if e["kind"] == "click")
    assert "structured_identity" not in click_ev


class _PixelBackendFull(_PixelBackend):
    def __init__(self):
        super().__init__()
        self._f = _png((1280, 800))

    @property
    def viewport(self):
        return (1280, 800)


# ---------------------------------------------------------------------------
# Compiler plumbs structured identity from the event onto the anchor
# ---------------------------------------------------------------------------

from openadapt_flow.compiler import compile_recording  # noqa: E402


def _write_frame(rec: _Path, i: int, suffix: str, size=(400, 300)) -> None:
    (rec / "frames").mkdir(parents=True, exist_ok=True)
    buf = _io.BytesIO()
    _Image.new("RGB", size, (250, 250, 250)).save(buf, format="PNG")
    (rec / "frames" / f"{i:04d}_{suffix}.png").write_bytes(buf.getvalue())


def test_compiler_stores_structured_identity_and_arms(tmp_path) -> None:
    rec = tmp_path / "rec"
    rec.mkdir()
    _write_frame(rec, 0, "before")
    _write_frame(rec, 0, "after")
    struct = "MG4408 Okafor, Philip 1966-01-17 M Active"
    (rec / "events.jsonl").write_text(
        _json.dumps(
            {
                "i": 0,
                "kind": "click",
                "x": 200,
                "y": 150,
                "t": 1.0,
                "structured_identity": struct,
            }
        )
        + "\n"
    )
    (rec / "meta.json").write_text(
        _json.dumps(
            {
                "id": "r1",
                "created_at": "2026-07-12T00:00:00+00:00",
                "viewport": [400, 300],
                "app_url": "http://x/",
                "params": {},
            }
        )
    )
    wf = compile_recording(rec, tmp_path / "bundle", name="wf")
    step = wf.steps[0]
    # PHI-free artifact (audit REM-2): the structured identity is stored as a
    # salted hash on the identity_template, not as plaintext. No name/MRN/DOB
    # in the bundle, but the exact-match structured tier still verifies.
    assert step.anchor.structured_identity is None
    tmpl = step.anchor.identity_template
    assert tmpl is not None and tmpl.structured is not None
    assert struct not in tmpl.model_dump_json()
    from openadapt_flow.runtime.identity_template import verify_structured_template

    assert verify_structured_template(tmpl, struct).status == "verified"
    assert (
        verify_structured_template(
            tmpl, "MG4408 Okafor, Philip 1966-01-18 M Active"
        ).status
        == "mismatch"
    )
    # Armed on the structured tier even though this blank frame OCRs to no
    # usable context band.
    assert step.identity_armed is True
