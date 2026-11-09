"""Wrong-patient safety gallery — generated visual proof of the identity defense.

This is a DETERMINISTIC generator. It takes a curated set of adversarial
patient-identifier classes, RENDERS each recorded row and its live/sibling row
with the SAME fixture the identity studies use
(:func:`openadapt_flow.validation.dense_surface.render_table_html`), reads them
with the REAL OCR (:func:`openadapt_flow.vision.ocr.ocr`), and judges each pair
with the REAL production identity check
(:func:`openadapt_flow.runtime.identity.verify_target_identity`). Nothing here
re-implements rendering, OCR, or the gate — it only orchestrates them and lays
the evidence out.

**ZERO model calls.** This is the deterministic OCR/identity path; no Anthropic
call, no VLM, no network. Regenerate both artifacts with::

    python -m benchmark.safety_gallery.generate

It emits, into ``benchmark/safety_gallery/``:

- ``gallery.html`` — a self-contained, theme-aware page (inline CSS, base64
  images, no external assets) that shows, per case: the two rows as they paint
  on screen, a magnified crop of the identifier cell, the byte-level OCR output
  of each row side by side (proving a true collapse reads IDENTICALLY), the
  gate's verdict (VERIFIED / MISMATCH / ABSTAIN / UNREADABLE) with a
  plain-English one-liner, and a SAFE/UNSAFE marker. It ends with an honest
  "What still slips" section drawn from ``docs/LIMITS.md``.
- ``results.json`` — the machine-checkable record (each case's OCR strings,
  verdict, and safe/unsafe flag) so correctness is verifiable without eyeballing
  the page.

The cases (the real glyph classes plus the separator class from the 10th
wrong-patient reopening, plus two controls that prove the gate is not trivially
abstaining):

    O0_alphanumeric   MG4408  vs MG44O8   -- danger, must NOT verify
    l1_alphanumeric   MG4118  vs MG41l8   -- danger, must NOT verify
    numeric           100512  vs 1OO512   -- danger, must NOT verify
    separator         MG-4408 vs MG-44O8  -- danger, must NOT verify (10th)
    sibling           MG5439  vs MG7263   -- danger (same name+DOB), must NOT verify
    clean_control     RC79284 vs RC79284  -- control, MUST verify
    different_patient RC44823 vs RC77235  -- control, MUST mismatch
"""

from __future__ import annotations

import argparse
import base64
import html
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Reuse the dense-surface fixture's RENDERING and the real identity/OCR path
# unchanged (read them, call them, never re-implement).
from openadapt_flow.validation.dense_surface import (
    DenseTable,
    Row,
    _filler_row,
    render_table_html,
)
from openadapt_flow.runtime.identity import squash, verify_target_identity

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Case fixture
# ---------------------------------------------------------------------------

DANGER = "danger"
CONTROL_VERIFY = "control_verify"
CONTROL_MISMATCH = "control_mismatch"


@dataclass
class CaseSpec:
    """One gallery case: a recorded target row and the live row seen at replay.

    ``kind`` fixes what the SAFE / CORRECT outcome is:

    - ``danger`` — a wrong-patient trap; the gate is SAFE iff it does NOT
      VERIFY (mismatch / abstain / unreadable all halt the run).
    - ``control_verify`` — the correct patient re-read; CORRECT iff VERIFIED
      (proves the gate is not trivially abstaining on everything).
    - ``control_mismatch`` — a plainly different patient; CORRECT iff MISMATCH
      (proves the gate is not trivially verifying).
    """

    id: str
    kind: str
    glyph_class: str
    title: str
    summary: str
    recorded: Row
    live: Row


def _row(name: str, dob: str, mrn: str, sex: str = "M", status: str = "Active") -> Row:
    return Row(name, dob, mrn, sex, status)


GALLERY_CASES: list[CaseSpec] = [
    CaseSpec(
        id="O0_alphanumeric",
        kind=DANGER,
        glyph_class="Letter O vs digit 0",
        title="A patient ID where a letter O stands in for a zero",
        summary=(
            "Two different patients who happen to share a name and date of birth. "
            "Their patient IDs look identical on screen — MG4408 and MG44O8 — "
            "because one uses the digit 0 where the other uses the letter O. Reading "
            "the screen, the computer sees the same text for both."
        ),
        recorded=_row("Sorensen, Philip", "1975-03-12", "MG4408"),
        live=_row("Sorensen, Philip", "1975-03-12", "MG44O8"),
    ),
    CaseSpec(
        id="l1_alphanumeric",
        kind=DANGER,
        glyph_class="Lowercase l vs digit 1",
        title="A patient ID where a lowercase L stands in for a one",
        summary=(
            "Same name, same date of birth, two different people. One ID is MG4118, "
            "the other MG41l8 — a digit 1 versus a lowercase L. On screen the two "
            "IDs are indistinguishable."
        ),
        recorded=_row("Okafor, Daniel", "1968-11-04", "MG4118"),
        live=_row("Okafor, Daniel", "1968-11-04", "MG41l8"),
    ),
    CaseSpec(
        id="numeric",
        kind=DANGER,
        glyph_class="All-digits ID, letter O for zero",
        title="An all-numbers ID with letter O's swapped for zeros",
        summary=(
            "Some patient IDs are all numbers, with no letters to anchor them — "
            "and they collapse too. 100512 (with digit zeros) and 1OO512 (with letter "
            "O's) read as the exact same text off the screen."
        ),
        recorded=_row("Delgado, Maria", "1982-07-22", "100512", sex="F"),
        live=_row("Delgado, Maria", "1982-07-22", "1OO512", sex="F"),
    ),
    CaseSpec(
        id="separator",
        kind=DANGER,
        glyph_class="Dashed ID, letter O for zero",
        title="A dashed ID that an earlier version of the check let through",
        summary=(
            "Patient IDs with a dash in them used to slip past an earlier version of "
            "this check. MG-4408 versus MG-44O8 — again a digit 0 versus a letter "
            "O. The system now ignores dashes and other separators before comparing, "
            "so this no longer gets through."
        ),
        recorded=_row("Bianchi, Robert", "1959-02-18", "MG-4408"),
        live=_row("Bianchi, Robert", "1959-02-18", "MG-44O8"),
    ),
    CaseSpec(
        id="sibling",
        kind=DANGER,
        glyph_class="Two people, same name and birth date",
        title="Two different patients who share a name and date of birth",
        summary=(
            "Here the computer CAN read the two IDs apart — MG5439 versus MG7263 "
            "— but the two patients share a name and date of birth, so matching on "
            "name and birth date alone would treat them as the same person. Comparing "
            "the patient ID catches that they are different people."
        ),
        recorded=_row("Halloran, Susan", "1975-03-12", "MG5439", sex="F"),
        live=_row("Halloran, Susan", "1975-03-12", "MG7263", sex="F"),
    ),
    CaseSpec(
        id="clean_control",
        kind=CONTROL_VERIFY,
        glyph_class="Control — the genuinely correct patient",
        title="The correct patient, shown again (an ID with no look-alike characters)",
        summary=(
            "The genuinely correct patient, shown again at replay. Their ID (RC79284) "
            "contains none of the look-alike characters, so there is nothing "
            "ambiguous. The system should proceed here — and does. This proves it "
            "is not simply refusing everything."
        ),
        recorded=_row("Montgomery, James", "1990-09-30", "RC79284"),
        live=_row("Montgomery, James", "1990-09-30", "RC79284"),
    ),
    CaseSpec(
        id="different_patient",
        kind=CONTROL_MISMATCH,
        glyph_class="Control — a plainly different patient",
        title="A completely different patient",
        summary=(
            "A completely different patient is sitting where the recorded one was "
            "(Castellano, Angela was recorded; Nakamura, Thomas appears at replay). "
            "The system should catch that these are different people — and does. "
            "This proves it is not simply approving everything."
        ),
        recorded=_row("Castellano, Angela", "1977-05-14", "RC44823", sex="F"),
        live=_row("Nakamura, Thomas", "1963-08-08", "RC77235"),
    ),
]


# ---------------------------------------------------------------------------
# Safety classification (pure, unit-testable without a browser)
# ---------------------------------------------------------------------------

# The four verdicts verify_target_identity can return. Only "verified" lets the
# run click; every other verdict HALTS on a pure-pixel substrate.
HALTING_VERDICTS = frozenset({"mismatch", "abstain", "unreadable"})


def is_safe(kind: str, status: str) -> bool:
    """Whether ``status`` is the SAFE / CORRECT outcome for a case ``kind``.

    A dangerous case is SAFE iff the gate does not VERIFY (any halt is safe).
    A verify-control is correct iff it VERIFIES; a mismatch-control iff it
    MISMATCHES.
    """
    if kind == DANGER:
        return status != "verified"
    if kind == CONTROL_VERIFY:
        return status == "verified"
    if kind == CONTROL_MISMATCH:
        return status == "mismatch"
    raise ValueError(f"unknown case kind: {kind!r}")


def verdict_oneliner(kind: str, status: str) -> str:
    """A plain-English reading of a verdict for the gallery."""
    if status == "abstain":
        return (
            "halts — OCR reads the two identifiers identically, so it cannot "
            "rule out a look-alike patient and refuses to click."
        )
    if status == "mismatch":
        if kind == CONTROL_MISMATCH:
            return "halts — the live row is affirmatively a different patient."
        return (
            "halts — the resolved row's identifier differs from the recorded "
            "target: a different patient."
        )
    if status == "unreadable":
        return "halts — no usable identity text could be read from the live band."
    if status == "verified":
        if kind == CONTROL_VERIFY:
            return (
                "proceeds — a clear name, date of birth, and an unambiguous ID all "
                "match the patient the workflow was recorded on."
            )
        return "proceeds — the check accepted this row."
    return status


# ---------------------------------------------------------------------------
# Plain-language labels (copy only; the underlying verdict is unchanged)
# ---------------------------------------------------------------------------

# Each verdict maps to a plain-language headline plus the original technical
# term (kept small, in parentheses, so the record stays traceable).
_VERDICT_PLAIN = {
    "abstain": (
        "HALTED — refuses to click (can't safely tell the patients apart)",
        "abstain",
    ),
    "mismatch": ("STOPPED — caught the mismatch", "mismatch"),
    "unreadable": ("HALTED — could not read the ID off the screen", "unreadable"),
    "verified": ("PROCEEDS — safe to act", "verified"),
}

# Human names for the confusable characters, for the per-card difference callout.
_GLYPH_NAMES = {
    "O": "the letter O", "o": "a lowercase o", "0": "the digit 0",
    "l": "a lowercase L", "I": "a capital I", "1": "the digit 1",
    "|": "a pipe", "!": "an exclamation mark",
}


def _glyph_name(ch: str) -> str:
    return _GLYPH_NAMES.get(ch, f"the character {ch!r}")


def glyph_diff(recorded: str, live: str) -> list[tuple[int, str, str]]:
    """Positions (and characters) where two equal-length IDs differ.

    Used only to point the reader at the one look-alike character that makes two
    different patients read the same; it does not feed the gate.
    """
    return [
        (i, a, b) for i, (a, b) in enumerate(zip(recorded, live)) if a != b
    ]


def _mark_id(mrn: str, positions: set[int]) -> str:
    """The ID as HTML with the differing character(s) highlighted."""
    return "".join(
        f'<mark class="gd">{_e(ch)}</mark>' if i in positions else _e(ch)
        for i, ch in enumerate(mrn)
    )


# ---------------------------------------------------------------------------
# Rendering (reuses render_table_html unchanged; adds only crop extraction)
# ---------------------------------------------------------------------------

VIEWPORT_W = 1120
DSF = 2


def _values_table(rows: dict[str, Row], seed: int = 7) -> tuple[DenseTable, dict[str, int]]:
    """Place each labelled row at a stable index in a realistic dense table
    (each preceded by one deterministic filler so backgrounds are consistent),
    mirroring the pixel-identity probe's stable-index trick. Returns the table
    and a ``key -> row index`` map."""
    import random

    rng = random.Random(seed)
    table_rows: list[Row] = [_filler_row(rng), _filler_row(rng)]
    index: dict[str, int] = {}
    for key, row in rows.items():
        table_rows.append(_filler_row(rng))
        index[key] = len(table_rows)
        table_rows.append(row)
    table_rows.append(_filler_row(rng))
    return DenseTable(rows=table_rows, pairs=[], n_rows=len(table_rows)), index


@dataclass
class RowCrops:
    row_png: bytes   # full-row crop (what paints on screen)
    mrn_png: bytes   # the identifier cell only (magnified in the gallery)


def render_row_crops(
    rows: dict[str, Row], *, top_offset_px: int, font_family: str = "Arial",
    font_px: int = 15,
) -> dict[str, RowCrops]:
    """Render ``rows`` in a dense table and return the full-row and MRN-cell PNG
    crops for each. Reuses ``render_table_html`` for the pixels; the only
    additions are the per-row / per-cell crop extraction."""
    import cv2
    import numpy as np
    from playwright.sync_api import sync_playwright

    table, index = _values_table(rows)
    html_doc = render_table_html(
        table, font_family=font_family, font_px=font_px, row_pad_px=6,
        top_offset_px=top_offset_px,
    )
    out: dict[str, RowCrops] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": VIEWPORT_W, "height": 1600}, device_scale_factor=DSF,
        )
        page.set_content(html_doc, wait_until="networkidle")
        full_h = page.evaluate("document.body.scrollHeight")
        page.set_viewport_size({"width": VIEWPORT_W, "height": int(full_h) + 20})
        png = page.screenshot(full_page=True)
        frame = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        fh, fw = frame.shape[:2]

        def _crop(selector: str) -> bytes:
            bb = page.eval_on_selector(
                selector,
                "el => { const r = el.getBoundingClientRect();"
                " return [r.x, r.y, r.width, r.height]; }",
            )
            x = max(0, int(bb[0] * DSF))
            y = max(0, int(bb[1] * DSF))
            x2 = min(fw, x + int(bb[2] * DSF))
            y2 = min(fh, y + int(bb[3] * DSF))
            sub = frame[y:y2, x:x2].copy()
            ok, buf = cv2.imencode(".png", sub)
            if not ok:
                raise RuntimeError(f"failed to encode crop for {selector}")
            return buf.tobytes()

        for key, i in index.items():
            out[key] = RowCrops(
                row_png=_crop(f'[data-row="{i}"]'),
                mrn_png=_crop(f'[data-row="{i}"] .mrn'),
            )
        browser.close()
    return out


def ocr_text(png: bytes) -> str:
    """The REAL OCR of a crop, joined left-to-right like the identity band."""
    from openadapt_flow.vision.ocr import ocr

    lines = ocr(png)
    return " ".join(line.text for line in lines)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    spec: CaseSpec
    recorded_crops: RowCrops
    live_crops: RowCrops
    ocr_recorded: str
    ocr_live: str
    status: str
    coverage: float
    safe: bool
    ocr_collapsed: bool
    oneliner: str = field(default="")

    def to_json(self) -> dict:
        return {
            "id": self.spec.id,
            "kind": self.spec.kind,
            "glyph_class": self.spec.glyph_class,
            "recorded_mrn": self.spec.recorded.mrn,
            "live_mrn": self.spec.live.mrn,
            "recorded_name": self.spec.recorded.name,
            "live_name": self.spec.live.name,
            "ocr_recorded": squash(self.ocr_recorded),
            "ocr_live": squash(self.ocr_live),
            "ocr_collapsed": self.ocr_collapsed,
            "verdict": self.status,
            "coverage": self.coverage,
            "safe": self.safe,
        }


def evaluate(cases: Optional[list[CaseSpec]] = None) -> list[CaseResult]:
    """Render, OCR, and judge every case with the real identity path.

    Requires Playwright + a browser + the OCR stack (this is the honest,
    end-to-end measurement — nothing is stubbed)."""
    cases = cases or GALLERY_CASES
    recorded_rows = {c.id: c.recorded for c in cases}
    live_rows = {f"{c.id}__live": c.live for c in cases}

    # Record crisp (offset 0); replay slightly shifted (offset 7) so the two
    # renders rasterize with genuine cross-render OCR jitter, as a real revisit
    # would — exactly the pixel-identity probe's STABLE_REF / STABLE_RERENDER.
    rec_crops = render_row_crops(recorded_rows, top_offset_px=0)
    live_crops = render_row_crops(live_rows, top_offset_px=7)

    results: list[CaseResult] = []
    for c in cases:
        rc = rec_crops[c.id]
        lc = live_crops[f"{c.id}__live"]
        ocr_rec = ocr_text(rc.row_png)
        ocr_liv = ocr_text(lc.row_png)
        check = verify_target_identity(ocr_rec, ocr_liv)
        safe = is_safe(c.kind, check.status)
        results.append(
            CaseResult(
                spec=c,
                recorded_crops=rc,
                live_crops=lc,
                ocr_recorded=ocr_rec,
                ocr_live=ocr_liv,
                status=check.status,
                coverage=round(float(check.coverage), 4),
                safe=safe,
                ocr_collapsed=(squash(ocr_rec) == squash(ocr_liv) and bool(squash(ocr_rec))),
                oneliner=verdict_oneliner(c.kind, check.status),
            )
        )
    return results


def headline(results: list[CaseResult]) -> dict:
    danger = [r for r in results if r.spec.kind == DANGER]
    controls = [r for r in results if r.spec.kind != DANGER]
    return {
        "danger_total": len(danger),
        "danger_safe": sum(1 for r in danger if r.safe),
        "controls_total": len(controls),
        "controls_correct": sum(1 for r in controls if r.safe),
        "all_safe": all(r.safe for r in results),
        "unsafe_ids": [r.spec.id for r in results if not r.safe],
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_VERDICT_STYLE = {
    "abstain": ("ABSTAIN", "halt"),
    "mismatch": ("MISMATCH", "halt"),
    "unreadable": ("UNREADABLE", "halt"),
    "verified": ("VERIFIED", "verify"),
}


def _img(png: bytes, cls: str) -> str:
    b64 = base64.b64encode(png).decode("ascii")
    return f'<img class="{cls}" alt="" src="data:image/png;base64,{b64}">'


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def _column_labels(kind: str) -> tuple[str, str]:
    """Plain-language left/right column headings for a case ``kind``.

    Left is always the patient the workflow was recorded on; the right heading
    tells the reader what is actually standing there at replay.
    """
    left = "The patient you recorded"
    if kind == DANGER:
        return left, "A DIFFERENT patient — same-looking row"
    if kind == CONTROL_VERIFY:
        return left, "The same patient at replay"
    if kind == CONTROL_MISMATCH:
        return left, "A different patient"
    return left, "The row present at replay"


def _difference_callout(r: CaseResult) -> str:
    """A plain-language pointer at the one character that separates the two
    patients — for the dangerous look-alike cards only.

    For a same-name/same-DOB sibling (a genuinely different, readable ID) it
    says the IDs differ outright; controls get no callout.
    """
    if r.spec.kind != DANGER:
        return ""

    rec, liv = r.spec.recorded.mrn, r.spec.live.mrn

    # Sibling: the two IDs are genuinely different and the computer CAN read
    # them apart — not one look-alike character. Say so plainly.
    if not r.ocr_collapsed:
        return (
            '<div class="diff">'
            f"<p><strong>These are two different patients.</strong> Their IDs are "
            f"genuinely different and the computer can read them apart — "
            f'<code class="idmark">{_e(rec)}</code> vs '
            f'<code class="idmark">{_e(liv)}</code>. Matching on name and date of '
            f"birth alone would treat them as the same person; comparing the ID "
            f"catches the difference.</p></div>"
        )

    diffs = glyph_diff(rec, liv)
    positions = {i for i, _, _ in diffs}
    if not diffs:
        return ""

    # De-duplicate identical substitutions (e.g. the all-digits ID swaps O for
    # 0 twice) so the sentence names the confusion once.
    seen: list[tuple[str, str]] = []
    for _, a, b in diffs:
        if (a, b) not in seen:
            seen.append((a, b))
    pairs = ", ".join(
        f"<strong class=\"glyph\">{_e(a)}</strong> vs "
        f"<strong class=\"glyph\">{_e(b)}</strong> ({_glyph_name(a)} vs "
        f"{_glyph_name(b)})"
        for a, b in seen
    )
    count = (
        "a single character"
        if len(diffs) == 1
        else f"{len(diffs)} look-alike characters"
    )
    return (
        '<div class="diff">'
        f"<p><strong>These are two different patients.</strong> Their IDs differ "
        f"by {count} — {pairs} — which the computer cannot tell apart when it "
        f"reads the screen.</p>"
        '<div class="idcmp">'
        f'<span class="idcmp-lbl">recorded</span>'
        f'<code class="idmark">{_mark_id(rec, positions)}</code>'
        f'<span class="idcmp-lbl">replay</span>'
        f'<code class="idmark">{_mark_id(liv, positions)}</code>'
        "</div></div>"
    )


def _case_card(r: CaseResult) -> str:
    _, kind = _VERDICT_STYLE.get(r.status, (r.status.upper(), "halt"))
    plain_label, tech = _VERDICT_PLAIN.get(
        r.status, (r.status.upper(), r.status)
    )
    safe_cls = "safe" if r.safe else "unsafe"
    safe_txt = "SAFE" if r.safe else "UNSAFE — P0"
    safe_mark = "&#10003;" if r.safe else "&#10007;"
    left_label, right_label = _column_labels(r.spec.kind)
    collapse_badge = (
        '<span class="badge collapse">The computer reads both IDs as the SAME '
        "text</span>"
        if r.ocr_collapsed
        else '<span class="badge distinct">The computer reads the two IDs '
        "differently</span>"
    )
    return f"""
    <section class="card {safe_cls}">
      <header class="card-head">
        <div class="titles">
          <span class="gclass">{_e(r.spec.glyph_class)}</span>
          <h3>{_e(r.spec.title)}</h3>
        </div>
        <div class="mark {safe_cls}" title="the correct, expected outcome">
          <span class="tick">{safe_mark}</span>{safe_txt}</div>
      </header>
      <p class="summary">{_e(r.spec.summary)}</p>
      {_difference_callout(r)}
      <div class="cols">
        <div class="col">
          <div class="col-label">{_e(left_label)}</div>
          {_img(r.recorded_crops.row_png, "row")}
          <div class="mrn-wrap"><span class="mrn-tag">patient ID, magnified</span>
            {_img(r.recorded_crops.mrn_png, "mrn")}</div>
          <div class="ocr-label">What the computer sees (OCR)</div>
          <code class="ocr">{_e(squash(r.ocr_recorded))}</code>
        </div>
        <div class="col">
          <div class="col-label">{_e(right_label)}</div>
          {_img(r.live_crops.row_png, "row")}
          <div class="mrn-wrap"><span class="mrn-tag">patient ID, magnified</span>
            {_img(r.live_crops.mrn_png, "mrn")}</div>
          <div class="ocr-label">What the computer sees (OCR)</div>
          <code class="ocr">{_e(squash(r.ocr_live))}</code>
        </div>
      </div>
      <div class="collapse-row">{collapse_badge}</div>
      <div class="verdict {kind}" title="coverage {r.coverage:.2f}">
        <span class="vlabel">{_e(plain_label)}</span>
        <span class="vterm">({_e(tech)})</span>
        <span class="vtext">{_e(r.oneliner)}</span>
      </div>
    </section>
    """


# The honest-limits content is quoted / paraphrased from docs/LIMITS.md and the
# fault-model study so the gallery discloses, not sells.
LIMITS_ITEMS = [
    (
        "Identity covers only ARMED steps — and real bundles arm a minority of clicks.",
        "The gate runs only where a step carries recorded identity context. The most "
        "recent live OpenEMR check armed 4 of 12 click steps; the rest (login buttons, "
        "icon-only pencils, too-generic bands) compile with NO identity check at all. "
        "A wrong-entity click on an unarmed step is still silent. Coverage is now an "
        "auditable per-step metric, but disclosure does not close the gap.",
    ),
    (
        "Phantom success on transactional writes — postconditions read the SCREEN, "
        "not the system of record.",
        "A 2026-07-12 fault-model study (benchmark/fault_model) drove 90 replays through "
        "a real persistence boundary and found the vision postconditions silently "
        "mishandle 5 of 7 transactional fault classes: a duplicate submission or "
        "double-click writes a SECOND record behind a clean success; an optimistic-UI "
        "update the backend later rejects reports success over an empty database; a "
        "partial save drops a field; a stale/concurrent edit overwrites another user's "
        "change. None is render drift, so self-healing cannot catch them, and the screen "
        "showed success. Closing this needs verification against the record (an API/DB "
        "read) plus an at-most-once guard — neither is expressible in a vision-only replay.",
    ),
    (
        "The pure-pixel over-halt cost — refusing a collapsible MRN also refuses the "
        "CORRECT patient.",
        "Because the OCR tier ABSTAINS on ANY collapsible identifier, it also halts the "
        "right patient whenever the true row's own MRN carries an O/0 or l/1 — a measured "
        "43.6-49.3% false-abort on the frozen adversarial corpora (the safe, cheap "
        "direction). On browser (DOM) and native desktop (UIA/AX) the structured-text "
        "tier verifies these with no availability cost, because 0 and O are distinct "
        "characters in the tree; the abstain cost bites only on pure-pixel substrates "
        "(Citrix/RDP/VDI, broken a11y).",
    ),
    (
        "The irreducible floor: a font that renders O and 0 (or l and 1) "
        "pixel-identical.",
        "Where two glyphs rasterize to the same pixels, NO vision method — OCR, "
        "pixel-compare, or VLM — can separate them; the distinction does not exist in "
        "the raster. None was found among 14 common UI fonts (benchmark/pixel_identity), "
        "but it is a real, disclosed limit rather than a solved problem.",
    ),
]


def render_html(results: list[CaseResult]) -> str:
    hd = headline(results)
    all_safe = hd["all_safe"]
    banner_cls = "ok" if all_safe else "alarm"
    if all_safe:
        headline_txt = (
            f"{hd['danger_safe']}/{hd['danger_total']} dangerous look-alikes refused "
            f"&middot; {hd['controls_correct']}/{hd['controls_total']} safe cases "
            f"handled correctly"
        )
    else:
        headline_txt = (
            f"P0: {hd['danger_total'] - hd['danger_safe']} dangerous case(s) VERIFIED a "
            f"wrong patient &mdash; {', '.join(hd['unsafe_ids'])}"
        )
    cards = "\n".join(_case_card(r) for r in results)
    limits = "\n".join(
        f'<li><strong>{_e(t)}</strong><p>{_e(b)}</p></li>' for t, b in LIMITS_ITEMS
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wrong-Patient Safety &mdash; openadapt-flow</title>
<style>
{_CSS}
</style>
</head>
<body>
<main>
  <header class="page-head">
    <h1>Wrong-Patient Safety</h1>
    <p class="sub">The look-alike-patient problem, and how the system refuses to
      guess.</p>
    <div class="headline {banner_cls}">{headline_txt}</div>
  </header>

  <section class="explainer">
    <h2>What you're looking at</h2>
    <p class="stakes">Automating an EMR means a computer clicks a patient's row and
      writes to their chart. The catastrophic failure is picking the <strong>wrong
      patient</strong>. This page shows that failure mode &mdash; and proves the
      system stops before it happens.</p>

    <div class="ex-grid">
      <div class="ex-item">
        <h3>Why it's hard</h3>
        <p>On legacy, Citrix, and RDP EMRs the software reads the <strong>screen</strong>
          with OCR &mdash; pixels, not the database. Some patient IDs differ by a
          single look-alike character: a letter <code>O</code> vs the digit
          <code>0</code>, a lowercase <code>l</code> vs the digit <code>1</code>. OCR
          reads those two <strong>different</strong> patients as the <strong>same</strong>
          text &mdash; so the computer literally cannot tell them apart from the
          screen.</p>
      </div>
      <div class="ex-item">
        <h3>The defense</h3>
        <p>When an identifier is that ambiguous, openadapt-flow <strong>halts</strong>
          instead of guessing. Below, every dangerous look-alike &rarr; it stops; a
          genuinely clean patient &rarr; it proceeds normally; a different patient
          &rarr; it catches the mismatch. It stops exactly when it should, and acts
          when it's safe.</p>
      </div>
    </div>

    <div class="legend">
      <h3>How to read each card</h3>
      <ul>
        <li><strong>Left</strong> &mdash; the patient the workflow was recorded on.</li>
        <li><strong>Right</strong> &mdash; the row present at replay. For the
          dangerous cards this is a <strong>different</strong> patient whose row looks
          the same; for the clean control it's the same patient re-rendered.</li>
        <li><strong>The magnified strip</strong> &mdash; the patient ID, the only real
          difference between the two.</li>
        <li><strong>&ldquo;What the computer sees (OCR)&rdquo;</strong> &mdash; the text
          read from the pixels. When it's <strong>identical</strong> on both sides,
          that's the danger.</li>
        <li><strong>The verdict</strong> &mdash; what the system actually did.</li>
      </ul>
    </div>

    <p class="method">Every pair below is a real screenshot, read with the repo's own
      OCR and judged by the exact check that ships in production
      (<code>verify_target_identity</code>). No AI model, no network, fully
      reproducible: <code>python -m benchmark.safety_gallery.generate</code>.</p>
  </section>

  <div class="gallery">
    {cards}
  </div>

  <section class="limits">
    <h2>What this does NOT protect against</h2>
    <p>This page would be dishonest without the failures it does <em>not</em> fix.
      Pulled straight from <code>docs/LIMITS.md</code> and the fault-model study:</p>
    <ul>
      {limits}
    </ul>
    <p class="foot">The check turns a wrong-patient approval into a stop; it does not
      make the rest of the automation all-seeing. The limits above are open problems,
      disclosed on purpose.</p>
  </section>
</main>
</body>
</html>
"""


_CSS = """
:root{
  --bg:#f6f7f9; --fg:#161a1f; --muted:#5b6570; --card:#ffffff; --line:#e2e6ea;
  --safe:#137a3f; --safe-bg:#e7f5ec; --unsafe:#b3261e; --unsafe-bg:#fdeceb;
  --halt:#8a5a00; --halt-bg:#fbf3e2; --verify:#137a3f; --verify-bg:#e7f5ec;
  --code:#0b3d2e; --code-bg:#eef4f1; --accent:#1f3a5f;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#0f1720; --fg:#e6edf3; --muted:#9fb0c0; --card:#161d26; --line:#263039;
    --safe:#4ade80; --safe-bg:#12321f; --unsafe:#ff6b61; --unsafe-bg:#3a1512;
    --halt:#e7b84b; --halt-bg:#332a12; --verify:#4ade80; --verify-bg:#12321f;
    --code:#a5f3d0; --code-bg:#10231b; --accent:#8fb2e0;
  }
}
:root[data-theme="dark"]{
  --bg:#0f1720; --fg:#e6edf3; --muted:#9fb0c0; --card:#161d26; --line:#263039;
  --safe:#4ade80; --safe-bg:#12321f; --unsafe:#ff6b61; --unsafe-bg:#3a1512;
  --halt:#e7b84b; --halt-bg:#332a12; --verify:#4ade80; --verify-bg:#12321f;
  --code:#a5f3d0; --code-bg:#10231b; --accent:#8fb2e0;
}
:root[data-theme="light"]{
  --bg:#f6f7f9; --fg:#161a1f; --muted:#5b6570; --card:#ffffff; --line:#e2e6ea;
  --safe:#137a3f; --safe-bg:#e7f5ec; --unsafe:#b3261e; --unsafe-bg:#fdeceb;
  --halt:#8a5a00; --halt-bg:#fbf3e2; --verify:#137a3f; --verify-bg:#e7f5ec;
  --code:#0b3d2e; --code-bg:#eef4f1; --accent:#1f3a5f;
}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  line-height:1.5;}
main{max-width:1040px;margin:0 auto;padding:32px 20px 64px;}
.page-head h1{font-size:30px;margin:0 0 6px;letter-spacing:-0.02em;}
.sub{color:var(--muted);margin:0 0 18px;max-width:70ch;}
.method{color:var(--muted);font-size:14px;max-width:78ch;}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  background:var(--code-bg);color:var(--code);padding:1px 5px;border-radius:4px;
  font-size:0.92em;}
.headline{display:inline-block;font-weight:700;font-size:17px;padding:10px 16px;
  border-radius:8px;margin:6px 0 16px;}
.headline.ok{background:var(--safe-bg);color:var(--safe);border:1px solid var(--safe);}
.headline.alarm{background:var(--unsafe-bg);color:var(--unsafe);border:1px solid var(--unsafe);}
.explainer{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:20px 22px;margin:8px 0 6px;}
.explainer h2{font-size:20px;margin:0 0 10px;letter-spacing:-0.01em;}
.explainer h3{font-size:14px;margin:0 0 6px;letter-spacing:0.01em;}
.explainer .stakes{font-size:16px;margin:0 0 16px;max-width:82ch;}
.explainer p{max-width:82ch;}
.ex-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin:6px 0 4px;}
@media (max-width:680px){.ex-grid{grid-template-columns:1fr;}}
.ex-item p{color:var(--muted);font-size:14px;margin:0;}
.legend{margin-top:18px;border-top:1px solid var(--line);padding-top:14px;}
.legend ul{list-style:none;padding:0;margin:8px 0 0;display:flex;flex-direction:column;
  gap:6px;}
.legend li{font-size:13.5px;color:var(--muted);max-width:88ch;}
.legend li strong{color:var(--fg);}
.explainer .method{color:var(--muted);font-size:13px;margin:16px 0 0;
  border-top:1px solid var(--line);padding-top:12px;}
.diff{margin:2px 0 14px;padding:11px 14px;border-radius:8px;background:var(--code-bg);
  border:1px solid var(--line);border-left:4px solid var(--accent);}
.diff p{margin:0;font-size:14px;max-width:84ch;}
.diff .glyph{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:1.05em;
  padding:0 3px;border-radius:3px;background:var(--halt-bg);color:var(--halt);}
.idcmp{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;align-items:center;
  margin-top:10px;}
.idcmp-lbl{font-size:11px;text-transform:uppercase;letter-spacing:0.05em;
  color:var(--muted);}
.idmark{font-family:ui-monospace,Menlo,Consolas,monospace;letter-spacing:0.12em;
  font-size:15px;}
mark.gd{background:var(--halt-bg);color:var(--halt);font-weight:800;padding:0 2px;
  border-radius:3px;text-decoration:underline;}
.gallery{display:flex;flex-direction:column;gap:22px;margin-top:14px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:18px 18px 16px;box-shadow:0 1px 2px rgba(0,0,0,0.04);}
.card.safe{border-left:5px solid var(--safe);}
.card.unsafe{border-left:5px solid var(--unsafe);}
.card-head{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;}
.gclass{display:inline-block;font-size:12px;text-transform:uppercase;letter-spacing:0.04em;
  color:var(--accent);font-weight:700;}
.titles h3{margin:2px 0 0;font-size:19px;letter-spacing:-0.01em;}
.mark{font-weight:800;font-size:14px;white-space:nowrap;padding:6px 12px;border-radius:999px;
  display:flex;align-items:center;gap:6px;}
.mark.safe{background:var(--safe-bg);color:var(--safe);}
.mark.unsafe{background:var(--unsafe-bg);color:var(--unsafe);}
.mark .tick{font-size:16px;}
.summary{color:var(--muted);font-size:14px;margin:10px 0 14px;max-width:82ch;}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
@media (max-width:680px){.cols{grid-template-columns:1fr;}}
.col{min-width:0;}
.col-label{font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
  color:var(--muted);margin-bottom:6px;}
img.row{width:100%;height:auto;border:1px solid var(--line);border-radius:6px;display:block;
  background:#fff;}
.mrn-wrap{display:flex;align-items:center;gap:8px;margin:8px 0 2px;}
.mrn-tag{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;}
img.mrn{height:44px;width:auto;image-rendering:-webkit-optimize-contrast;
  border:1px solid var(--line);border-radius:5px;background:#fff;padding:2px 4px;}
.ocr-label{font-size:11px;color:var(--muted);margin:10px 0 3px;text-transform:uppercase;
  letter-spacing:0.05em;}
code.ocr{display:block;white-space:pre-wrap;word-break:break-all;padding:8px 10px;
  font-size:12.5px;line-height:1.45;}
.collapse-row{margin:14px 0 4px;}
.badge{display:inline-block;font-size:12px;font-weight:700;padding:5px 11px;border-radius:6px;}
.badge.collapse{background:var(--unsafe-bg);color:var(--unsafe);border:1px solid var(--unsafe);}
.badge.distinct{background:var(--code-bg);color:var(--muted);border:1px solid var(--line);}
.verdict{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:10px;
  padding:11px 14px;border-radius:8px;}
.verdict.halt{background:var(--halt-bg);}
.verdict.verify{background:var(--verify-bg);}
.vlabel{font-weight:800;font-size:14px;letter-spacing:0.03em;}
.verdict.halt .vlabel{color:var(--halt);}
.verdict.verify .vlabel{color:var(--verify);}
.vterm{font-size:12px;color:var(--muted);font-family:ui-monospace,Menlo,monospace;}
.vtext{flex:1;min-width:220px;font-size:14px;}
.limits{margin-top:40px;border-top:2px solid var(--line);padding-top:22px;}
.limits h2{font-size:22px;margin:0 0 8px;}
.limits > p{color:var(--muted);max-width:80ch;}
.limits ul{list-style:none;padding:0;margin:16px 0;display:flex;flex-direction:column;gap:14px;}
.limits li{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--halt);
  border-radius:8px;padding:12px 16px;}
.limits li strong{display:block;font-size:15px;margin-bottom:4px;}
.limits li p{margin:0;color:var(--muted);font-size:14px;max-width:88ch;}
.foot{font-size:13px;color:var(--muted);margin-top:18px;}
"""


# ---------------------------------------------------------------------------
# Build + CLI
# ---------------------------------------------------------------------------


def build(outdir: Path = HERE) -> dict:
    """Evaluate every case and write ``gallery.html`` + ``results.json``.

    Returns the headline dict (also used by the test as a machine check)."""
    outdir.mkdir(parents=True, exist_ok=True)
    results = evaluate()
    hd = headline(results)

    payload = {
        "generated_by": "benchmark.safety_gallery.generate",
        "model_calls": 0,
        "identity_check": "openadapt_flow.runtime.identity.verify_target_identity",
        "headline": hd,
        "cases": [r.to_json() for r in results],
    }
    (outdir / "results.json").write_text(json.dumps(payload, indent=2) + "\n")
    (outdir / "gallery.html").write_text(render_html(results))
    return hd


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(HERE),
                        help="output directory (default: this module's directory)")
    args = parser.parse_args(argv)

    hd = build(Path(args.out))
    print(
        f"safety gallery: {hd['danger_safe']}/{hd['danger_total']} dangerous cases safe, "
        f"{hd['controls_correct']}/{hd['controls_total']} controls correct"
    )
    if not hd["all_safe"]:
        print(f"  P0 UNSAFE CASES: {', '.join(hd['unsafe_ids'])}")
        return 1
    print(f"  wrote gallery.html + results.json to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
