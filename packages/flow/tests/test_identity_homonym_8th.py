"""The 8th wrong-patient reopening — end-to-end repro + fix, real render+OCR.

An adversarial review of PR #31 proved a LIVE, production-reachable wrong-
patient VERIFY on the OCR name+DOB tier. Two DIFFERENT patients share NAME and
DOB and differ only by a glyph-confusable MRN:

    recorded target : Smith, John · 01/15/1980 · MRN AC50061   (digit zeros)
    live wrong row  : Smith, John · 01/15/1980 · MRN AC5OO61   (letter O -- a
                                                                DIFFERENT patient)

RapidOCR collapses the letter O to a digit 0, so BOTH rows OCR to a
BYTE-IDENTICAL band string. The recorded band carries a discriminative name
(Smith/John), so #27's name+DOB-primary rule let the matched name "carry"
identity and SUPPRESSED the digit-side glyph budget -> status "verified" ->
the WRONG patient's chart is clicked.

This test drives the REAL replayer path (``Replayer._verify_identity`` ->
structured/pixel/vlm/OCR ladder) on a pixel-only substrate (no
``structured_text_at`` backend, no ``identifier_crop`` -> pixel/vlm tiers
abstain), with REAL browser rendering and REAL RapidOCR.

Pre-fix this VERIFIED the wrong patient (coverage 1.0). Post-fix the OCR tier
ABSTAINS (band rests on a glyph-confusable identifier OCR may have collapsed;
a same-name/same-DOB homonym cannot be ruled out), so an irreversible step
HALTS. Browser + OCR, so guarded by ``importorskip``.
"""

from __future__ import annotations

from datetime import date

import pytest

pytest.importorskip("playwright")
pytest.importorskip("rapidocr_onnxruntime")

import openadapt_flow.vision as vision  # noqa: E402
from openadapt_flow.ir import Anchor, Resolution, Step, Workflow  # noqa: E402
from openadapt_flow.runtime import identity as I  # noqa: E402
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402
from openadapt_flow.validation.dense_surface import (  # noqa: E402
    DenseTable,
    Row,
    RenderCondition,
    render_frame,
)

_COND = RenderCondition("record", "Arial", 15, 2, 6)
_ROW_K = 2  # the patient row under test (two filler rows precede it)


def _filler(i: int) -> Row:
    return Row(
        f"Filler{i}, Pat", "1971-02-0%d" % (i % 9 + 1), f"ZZ{1000 + i}", "M", "Active"
    )


def _render(mrn: str):
    rows = [
        _filler(0),
        _filler(1),
        Row("Smith, John", "01/15/1980", mrn, "M", "Active"),
        _filler(3),
    ]
    table = DenseTable(rows=rows, pairs=[], n_rows=len(rows))
    return render_frame(table, _COND, top_offset_px=0)


def _band_ocr(frame, k: int) -> str:
    _, _, _, row_region = frame.points[k]
    return " ".join(ln.text for ln in vision.ocr(frame.png, region=row_region))


def _processed_context(frame, k: int) -> str:
    """The band the COMPILER stores: row band minus the clicked (Open) cell
    and volatile (timestamp) lines, filtered exactly as replay filters the
    observed band -- so a correct re-resolution matches at coverage ~1.0."""
    _, open_pt, _, _ = frame.points[k]
    band = I.band_region((open_pt[0], open_pt[1]), 24, frame.viewport)
    exclude = (open_pt[0] - 30, open_pt[1] - 12, 60, 24)
    lines = [
        ln
        for ln in vision.ocr(frame.png, region=band)
        if ln.text.strip()
        and not I.regions_intersect(ln.region, exclude)
        and not I.is_volatile_line(ln.text, reference_date=date.today())
    ]
    lines = I.lines_near_point(lines, open_pt[1])
    return " ".join(ln.text.strip() for ln in lines)


class _PixelOnlyBackend:
    """A pixel-only substrate: no ``structured_text_at`` (so the structured
    tier is UNAVAILABLE) -- exactly a Citrix/RDP/VDI or broken-a11y target."""

    def __init__(self, viewport, live_png: bytes) -> None:
        self.viewport = viewport
        self._live_png = live_png

    def screenshot(self) -> bytes:
        return self._live_png


def _verdict_on_wrong_patient():
    rec = _render("AC50061")  # recorded target (digit zeros)
    live = _render("AC5OO61")  # live SIBLING row (letter O) -- different patient

    rec_band = _band_ocr(rec, _ROW_K)
    live_band = _band_ocr(live, _ROW_K)
    context_text = _processed_context(rec, _ROW_K)

    _, open_pt, _, _ = rec.points[_ROW_K]
    _, live_open_pt, _, _ = live.points[_ROW_K]
    anchor = Anchor(
        template="t.png",
        region=(open_pt[0] - 30, open_pt[1] - 12, 60, 24),
        click_point=(open_pt[0], open_pt[1]),
        context_text=context_text,
        identifier_crop=None,  # pixel-only: no crop -> pixel & vlm tiers abstain
    )
    step = Step(
        id="open_patient",
        intent="open patient chart",
        action="click",
        anchor=anchor,
        risk="irreversible",
    )
    wf = Workflow(name="repro_8th", params={}, steps=[step])
    res = Resolution(
        rung="ocr",
        point=(live_open_pt[0], live_open_pt[1]),
        confidence=0.9,
        elapsed_ms=1.0,
    )
    replayer = Replayer(_PixelOnlyBackend(rec.viewport, live.png), vision=vision)
    check = replayer._verify_identity(step, res, live.png, {}, wf, bundle_dir=None)
    return rec_band, live_band, context_text, check


def test_letter_O_sibling_ocr_collapses_to_digit_band():
    """The impossibility premise: the two DIFFERENT patients' rows OCR to a
    BYTE-IDENTICAL band -- no function downstream of OCR can separate them."""
    rec_band, live_band, _, _ = _verdict_on_wrong_patient()
    assert I.squash(rec_band) == I.squash(live_band)
    assert "ac50061" in I.squash(rec_band)  # letter-O sibling read as digit 0


def test_wrong_patient_does_not_verify_via_real_replayer_ocr_tier():
    """THE 8th REOPENING, closed. Pre-fix the OCR tier VERIFIED this wrong
    patient at coverage 1.0 (name+DOB carried, digit-glyph budget suppressed).
    Post-fix it ABSTAINS -- OCR cannot certify a collapsible MRN -- so the
    wrong patient is NOT verified. The ladder then HALTs (irreversible)."""
    _, _, context_text, check = _verdict_on_wrong_patient()
    # the discriminative name really is in the recorded band (so the pre-fix
    # name-carry path was exercised)
    assert "smith" in I.squash(context_text)
    assert check.status != "verified"  # the safety invariant
    assert check.status == "abstain"  # the honest verdict (not a
    #                                            false "mismatch/different")
    assert check.mode == "context"
