"""The 9th wrong-patient reopening — OCR verify-path conservative on ANY
collapsible-glyph identifier, INCLUDING purely-numeric MRNs. Real render + real
RapidOCR + real ``Replayer._verify_identity``.

The 8th fix (PR #32) made the OCR tier ABSTAIN on a glyph-confusable
identifier, but its predicate keyed on a letter+digit MIX
(``_is_glyph_vulnerable_identifier`` required BOTH an alpha and a digit char).
That MISSED the most common real MRN shape: a PURELY NUMERIC one. Two DIFFERENT
patients sharing NAME and DOB, whose MRNs differ only by an O/0 or l/1 glyph in
an all-digit body —

    recorded target : Smith, John · 01/15/1980 · MRN 100512   (digit ones/zeros)
    live wrong row  : Smith, John · 01/15/1980 · MRN 1OO512   (letter O's — a
                                                               DIFFERENT patient)

— OCR to the byte-identical band string ``100512``. The 8th predicate never
flagged the recorded ``100512`` (no letter), so the name+DOB matched, the glyph
budget stayed 0, and the OCR tier VERIFIED the wrong patient. Also reproduced
with ``400761``/``4OO761`` and ``417063``/``4l7063``.

The fix drops the alphanumeric-mix requirement: ANY identifier-shaped token
(a bare alnum run ≥ 3 chars carrying a digit — numeric, alphanumeric, any
casing) bearing a confusable glyph {0,1,O,l,I} makes the OCR tier ABSTAIN.
A clean identifier bearing NONE of those glyphs (``RC79284``) still verifies;
a different-NAME sibling still MISMATCHES. Split identifiers are covered too:
a confusable glyph in any numeric/alnum fragment triggers abstain.

Every real-render test drives the pixel-only substrate path of the REAL
``Replayer._verify_identity`` (no ``structured_text_at``, no ``identifier_crop``
→ structured/pixel/vlm tiers abstain, OCR tier decides), so the verdict is the
production one. Browser + OCR, so guarded by ``importorskip``.
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

_ROW_K = 2  # the patient row under test (two filler rows precede it)


def _filler(i: int) -> Row:
    return Row(
        f"Filler{i}, Pat", "1971-02-0%d" % (i % 9 + 1), f"ZZ{1000 + i}", "M", "Active"
    )


def _render(
    mrn: str,
    cond: RenderCondition,
    *,
    name: str = "Smith, John",
    dob: str = "01/15/1980",
):
    rows = [_filler(0), _filler(1), Row(name, dob, mrn, "M", "Active"), _filler(3)]
    table = DenseTable(rows=rows, pairs=[], n_rows=len(rows))
    return render_frame(table, cond, top_offset_px=0)


def _band_ocr(frame, k: int) -> str:
    _, _, _, row_region = frame.points[k]
    return " ".join(ln.text for ln in vision.ocr(frame.png, region=row_region))


def _processed_context(frame, k: int) -> str:
    """The band the COMPILER stores: row band minus the clicked (Open) cell
    and volatile (timestamp) lines, filtered exactly as replay filters the
    observed band."""
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
    """A pixel-only substrate: no ``structured_text_at`` (structured tier
    UNAVAILABLE) — exactly a Citrix/RDP/VDI or broken-a11y target."""

    def __init__(self, viewport, live_png: bytes) -> None:
        self.viewport = viewport
        self._live_png = live_png

    def screenshot(self) -> bytes:
        return self._live_png


def _verdict(
    rec_mrn: str,
    live_mrn: str,
    cond: RenderCondition,
    *,
    rec_name: str = "Smith, John",
    live_name: str = "Smith, John",
    dob: str = "01/15/1980",
):
    """Drive the REAL Replayer._verify_identity for a recorded target row vs a
    live (target or sibling) row rendered under ``cond``."""
    rec = _render(rec_mrn, cond, name=rec_name, dob=dob)
    live = _render(live_mrn, cond, name=live_name, dob=dob)

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
    wf = Workflow(name="repro_9th", params={}, steps=[step])
    res = Resolution(
        rung="ocr",
        point=(live_open_pt[0], live_open_pt[1]),
        confidence=0.9,
        elapsed_ms=1.0,
    )
    replayer = Replayer(_PixelOnlyBackend(rec.viewport, live.png), vision=vision)
    check = replayer._verify_identity(step, res, live.png, {}, wf, bundle_dir=None)
    return rec_band, live_band, context_text, check


# --- Attack vectors: numeric O/0 and l/1 homonyms across fonts/sizes ---------
# Each is a same-name/same-DOB DIFFERENT patient one glyph apart in a PURELY
# NUMERIC MRN. All must HALT (never "verified"): ABSTAIN when OCR collapses the
# glyph (the common case), MISMATCH when OCR happens to distinguish it.
_NUMERIC_HOMONYMS = [
    ("100512", "1OO512", "Arial", 15),
    ("100512", "1OO512", "Times New Roman", 13),
    ("100512", "1OO512", "Courier New", 12),
    ("100512", "1OO512", "Georgia", 15),
    ("100512", "1OO512", "Verdana", 11),
    ("400761", "4OO761", "Arial", 12),
    ("400761", "4OO761", "Verdana", 14),
    ("417063", "4l7063", "Arial", 13),
    ("417063", "4l7063", "Georgia", 10),
    ("501900", "5O19OO", "Arial", 15),
    ("110234", "ll0234", "Times New Roman", 15),
]


@pytest.mark.parametrize("rec_mrn,sib_mrn,font,px", _NUMERIC_HOMONYMS)
def test_numeric_mrn_homonym_never_verifies_real_stack(rec_mrn, sib_mrn, font, px):
    """THE 9th REOPENING. A purely-numeric MRN homonym must NOT verify the wrong
    patient on the real replayer OCR tier — across fonts and sizes."""
    cond = RenderCondition(f"{font}_{px}", font, px, 2, 6)
    _, _, context_text, check = _verdict(rec_mrn, sib_mrn, cond)
    # the discriminative name is really in the recorded band (name-carry path)
    assert "smith" in I.squash(context_text)
    assert check.status != "verified"  # the safety invariant


def test_numeric_100512_collapses_and_abstains_arial():
    """The impossibility premise on the canonical numeric case: 100512 and a
    DIFFERENT patient's 1OO512 (letter O's) OCR to a BYTE-IDENTICAL band, and
    the OCR tier ABSTAINS (the honest 'OCR cannot certify' verdict)."""
    cond = RenderCondition("arial15", "Arial", 15, 2, 6)
    rec_band, live_band, context_text, check = _verdict("100512", "1OO512", cond)
    assert I.squash(rec_band) == I.squash(live_band)  # byte-identical OCR
    assert "100512" in I.squash(rec_band)  # letter-O read as digit 0
    assert "smith" in I.squash(context_text)
    assert check.status == "abstain"
    assert check.mode == "context"


def test_numeric_417063_l1_collapse_abstains():
    """The l/1 numeric analogue: 417063 vs 4l7063 (letter l)."""
    cond = RenderCondition("arial13", "Arial", 13, 2, 6)
    rec_band, live_band, _, check = _verdict("417063", "4l7063", cond)
    assert I.squash(rec_band) == I.squash(live_band)
    assert check.status == "abstain"


def test_alphanumeric_regression_ac50061_still_abstains():
    """No regression on the 8th-fix case: AC50061 vs AC5OO61 must still HALT."""
    cond = RenderCondition("arial15", "Arial", 15, 2, 6)
    rec_band, live_band, _, check = _verdict("AC50061", "AC5OO61", cond)
    assert I.squash(rec_band) == I.squash(live_band)
    assert check.status != "verified"


def test_lowercase_mixed_case_numeric_homonym_never_verifies():
    """Lowercase/mixed identifiers are covered: the predicate squashes to
    lowercase, so a mixed-case MRN homonym halts exactly like an upper one."""
    cond = RenderCondition("arial15", "Arial", 15, 2, 6)
    # ab10023 vs ab1OO23 (letter O's); alnum, lowercase recorded
    _, _, _, check = _verdict("ab10023", "ab1OO23", cond)
    assert check.status != "verified"


# --- Clean targets STILL VERIFY (no catastrophic over-halt) ------------------


def test_clean_nonconfusable_mrn_still_verifies():
    """A clean name+DOB with an identifier containing NONE of {0,1,O,l,I}
    (MRN RC79284) must still VERIFY on the correct row — the conservative rule
    does not over-halt the genuinely-clean band."""
    cond = RenderCondition("arial15", "Arial", 15, 2, 6)
    _, _, context_text, check = _verdict("RC79284", "RC79284", cond)
    assert "smith" in I.squash(context_text)
    assert check.status == "verified"


def test_different_name_sibling_mismatches():
    """A different-NAME sibling (even sharing DOB and a clean MRN) is an
    AFFIRMATIVE mismatch, not an abstain — the name budgets still fire."""
    cond = RenderCondition("arial15", "Arial", 15, 2, 6)
    _, _, _, check = _verdict("RC79284", "RC79284", cond, live_name="Jones, John")
    assert check.status == "mismatch"


# --- SPLIT identifiers: a confusable glyph in ANY fragment triggers abstain --
# OCR-forced splits are non-deterministic to render, so the split path is
# proven directly on the REAL matching stack (band_match / verify_target_identity
# — the same functions the replayer OCR tier calls), with the recorded
# identifier stored as fragments (a record-time OCR split) and the live band
# reading it whole (or vice-versa).


class TestSplitIdentifierAbstains:
    def test_numeric_split_recorded_fragments_abstains(self):
        # recorded MRN split into '100' '512'; live reads it whole '100512'.
        rec = "Smith, John 01/15/1980 100 512 M Active"
        live = "Smith, John 01/15/1980 100512 M Active"
        assert I.verify_target_identity(rec, live).status == "abstain"

    def test_alnum_split_recorded_fragments_abstains(self):
        # 'AC5' '0061' recorded, whole 'AC50061' live — the numeric fragment
        # '0061' carries the confusable glyph and must trigger abstain.
        rec = "Smith, John 01/15/1980 AC5 0061 M Active"
        live = "Smith, John 01/15/1980 AC50061 M Active"
        assert I.verify_target_identity(rec, live).status == "abstain"

    def test_live_split_of_recorded_whole_abstains(self):
        # the reverse split direction (recorded whole, live fragmented).
        rec = "Smith, John 01/15/1980 100512 M Active"
        live = "Smith, John 01/15/1980 100 512 M Active"
        assert I.verify_target_identity(rec, live).status == "abstain"

    def test_split_fragment_carries_confusable_only_in_numeric_part(self):
        # a name fragment adjacent to the numeric MRN fragment must NOT be
        # mistaken for the identifier; only the '0061'-style fragment flags.
        assert I._is_glyph_vulnerable_identifier("0061") is True
        assert I._is_glyph_vulnerable_identifier("ac5") is False  # 5 not conf.
        assert I._is_glyph_vulnerable_identifier("evelyn") is False


# --- Predicate-level pins (numeric / split fragment / lowercase / clean) ------


class TestGlyphVulnerablePredicate:
    def test_purely_numeric_with_confusable_is_flagged(self):
        for tok in ("100512", "400761", "417063", "0061", "1980", "748291"):
            assert I._is_glyph_vulnerable_identifier(tok) is True, tok

    def test_alphanumeric_with_confusable_is_flagged(self):
        for tok in ("ac50061", "ac5oo61", "mg4408", "cox3834"):
            assert I._is_glyph_vulnerable_identifier(tok) is True, tok

    def test_clean_identifier_not_flagged(self):
        # no 0/1/O/l/I anywhere -> not glyph-vulnerable -> verifies.
        for tok in ("rc79284", "zz4832", "mg4728", "abc957"):
            assert I._is_glyph_vulnerable_identifier(tok) is False, tok

    def test_names_and_dates_not_flagged(self):
        # a name (pure alpha, even bearing o/l/i) and a separator-bearing date
        # are NOT identifier-shaped.
        for tok in ("smith", "john", "oliver", "01/15/1980", "1985-03-12"):
            assert I._is_identifier_shaped(tok) in (
                False,
            ) or not I._is_glyph_vulnerable_identifier(tok), tok
        assert I._is_glyph_vulnerable_identifier("smith") is False
        assert I._is_glyph_vulnerable_identifier("01/15/1980") is False

    def test_short_runs_not_flagged(self):
        for tok in ("m", "f", "10", "z1"):
            assert I._is_glyph_vulnerable_identifier(tok) is False, tok

    def test_no_alphanumeric_mix_requirement(self):
        # the crux of the 9th reopening: a purely-numeric token (no letter) is
        # now flagged, where the 8th predicate's letter+digit mix missed it.
        assert I._is_glyph_vulnerable_identifier("100512") is True
        assert not any(c.isalpha() for c in "100512")
