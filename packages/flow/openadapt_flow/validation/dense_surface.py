"""Dense sibling-surface false-abort / false-accept study of the identity band.

The bounded identity evidence (``docs/validation/IDENTITY_EVIDENCE.md``: false accept
0.000%, false abort 26.17%) is measured on SYNTHETIC corpora — string pairs
with hand-injected OCR noise — and, at the product level, on CLEAN OpenEMR
identity banners. Neither is the surface where a wrong-patient write actually
does damage: a DENSE, SIBLING-HEAVY record list, where OCR noise is worst
(small dense rows, adjacent-row bleed) and near-duplicate names sit one row
apart.

This module measures the identity matcher on that surface for real, by
RENDERING a dense clinical record table, SCREENSHOTTING it, running the
repo's own OCR (``openadapt_flow.vision.ocr``), extracting the identity band
EXACTLY as the compiler records it and the replayer verifies it
(``context_from_lines`` at record time, ``band_region`` +
``lines_near_point`` + the 2x-upscale retry at replay time), and running
``verify_target_identity``. Nothing here fabricates band strings — every
string comes out of RapidOCR reading a rendered PNG.

Two per-click rates, on the dense surface:

- **false abort** — a PRESENT, CORRECT target row refused. The resolver
  landed on the right row; identity should verify, but dense-row OCR noise
  (dropped/garbled cells, adjacent-row bleed) drops band coverage below the
  operating point and the run safe-halts. Cost: one hybrid fallback
  (~$0.10) or a human retry.
- **false accept** — a seeded SIBLING row VERIFIED as the target. The
  resolver landed on the wrong-but-positionally-plausible sibling row;
  identity should mismatch, but does not. Cost: a wrong-patient write —
  catastrophic, and NOT caught by downstream note verification (the note is
  saved, in the wrong chart). Must stay 0; every one that slips is reported
  with its exact rendered rows and band strings.

Siblings are realistic DIFFERENT patients (distinct MRN — the unique key),
differing from the target in exactly one collision dimension. We do NOT rig
identical-MRN siblings (impossible for real distinct patients); a false
accept here therefore requires dense OCR to have DROPPED or CONFLATED the
discriminating cell on its own — which is precisely the emergent risk this
study exists to measure.

Run:
    python -m openadapt_flow.validation.dense_surface --out benchmark/dense_surface

Outputs (under ``--out``): ``dense_surface.json`` (raw per-trial records and
aggregates) and the rendered target/sibling screenshots for audit. The
narrative deliverable ``DENSE_SURFACE.md`` is written by the same run.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.ir import Point, Region

# ---------------------------------------------------------------------------
# Collision-class fixture
# ---------------------------------------------------------------------------


@dataclass
class Row:
    # A record-list row. ``sex``/``status`` are the low-entropy shared columns
    # of a real EMR list; ``name``/``dob``/``mrn`` carry identity.
    name: str
    dob: str
    mrn: str
    sex: str
    status: str
    # Set at render time from the live DOM bounding boxes.
    name_point: Optional[Point] = None
    open_point: Optional[Point] = None
    y_center: Optional[int] = None
    row_region: Optional[Region] = None


@dataclass
class CollisionPair:
    """A target row and the adjacent sibling that collides with it."""

    collision_class: str
    target: Row
    sibling: Row
    # How the sibling differs, in plain words (for the report).
    note: str


_SURNAMES = [
    "Harrington",
    "Okafor",
    "Delgado",
    "Bianchi",
    "Halloran",
    "Montgomery",
    "Castellano",
    "Fitzgerald",
    "Abernathy",
    "Whitfield",
    "Lindqvist",
    "Nakamura",
    "Petrov",
    "Ferreira",
    "Kowalski",
    "Underwood",
]
_FIRSTS = [
    "James",
    "Maria",
    "Philip",
    "Karen",
    "Daniel",
    "Susan",
    "Robert",
    "Angela",
    "Thomas",
    "Patricia",
    "Edward",
    "Nancy",
    "Gregory",
    "Diane",
]
_STATUSES = ["Active", "Active", "Active", "Inactive", "Pending"]


def _mrn(rng: random.Random, prefix: str = "MG") -> str:
    return f"{prefix}{rng.randint(100000, 999999)}"


def _dob(rng: random.Random) -> str:
    y = rng.randint(1940, 2005)
    m = rng.randint(1, 12)
    d = rng.randint(1, 28)
    return f"{y:04d}-{m:02d}-{d:02d}"


def _transpose_two(mrn: str, rng: random.Random) -> str:
    """Transpose two adjacent digits of an MRN (a real different identifier)."""
    digits = [i for i, c in enumerate(mrn) if c.isdigit()]
    # pick an i where i and i+1 are both digits and differ
    cands = [i for i in digits if i + 1 in digits and mrn[i] != mrn[i + 1]]
    if not cands:
        return mrn[:-2] + mrn[-1] + mrn[-2]
    i = rng.choice(cands)
    return mrn[:i] + mrn[i + 1] + mrn[i] + mrn[i + 2 :]


def _dob_off_by_one(dob: str) -> str:
    y, m, d = dob.split("-")
    dd = int(d)
    dd = dd + 1 if dd < 28 else dd - 1
    return f"{y}-{m}-{dd:02d}"


# Name-collision pairs: same phonetic/visual name, DIFFERENT real people.
# Each entry is (class, target_name, sibling_name, note).
_NAME_COLLISIONS = [
    (
        "near_surname",
        "Sorensen, Philip",
        "Sorenson, Philip",
        "surname e/o swap (Sorensen vs Sorenson) — a/o is NOT an OCR class",
    ),
    (
        "nguyen_variant",
        "Nguyen, Anh",
        "Ngyuen, Anh",
        "Nguyen transposition (Nguyen vs Ngyuen)",
    ),
    (
        "generational_suffix",
        "Belford, Philip",
        "Belford, Philip Jr",
        "generational suffix present on the sibling only",
    ),
    (
        "same_surname_diff_first",
        "Okafor, James",
        "Okafor, Janet",
        "shared surname, different first name (James vs Janet)",
    ),
    (
        "letterletter_name",
        "Nesbitt, Neil",
        "Nesbitt, Nell",
        "l/i letter-letter confusion (Neil vs Nell) — the suspect class",
    ),
]


def build_collision_pairs(seed: int) -> list[CollisionPair]:
    """Construct the collision pairs. Each sibling is a realistic DIFFERENT
    patient (distinct MRN) differing from the target in exactly the collision
    dimension; the confusable/transposed classes put the difference in the
    MRN itself."""
    rng = random.Random(seed)
    pairs: list[CollisionPair] = []

    # -- name collisions: distinct MRN (real different patients), same DOB --
    for cls, tname, sname, note in _NAME_COLLISIONS:
        dob = _dob(rng)
        status = rng.choice(_STATUSES)
        t = Row(tname, dob, _mrn(rng), rng.choice("MF"), status)
        s = Row(sname, dob, _mrn(rng), rng.choice("MF"), status)
        pairs.append(CollisionPair(cls, t, s, note))

    # -- same name, DOB off by one (distinct MRN) --
    for _ in range(1):
        dob = _dob(rng)
        surname = rng.choice(_SURNAMES)
        first = rng.choice(_FIRSTS)
        name = f"{surname}, {first}"
        t = Row(name, dob, _mrn(rng), rng.choice("MF"), rng.choice(_STATUSES))
        s = Row(name, _dob_off_by_one(dob), _mrn(rng), t.sex, t.status)
        pairs.append(
            CollisionPair(
                "same_name_diff_dob", t, s, "identical name, DOB off by one day"
            )
        )

    # -- MRN transposition: same name+DOB, MRN two adjacent digits swapped --
    for _ in range(1):
        dob = _dob(rng)
        name = f"{rng.choice(_SURNAMES)}, {rng.choice(_FIRSTS)}"
        mrn = _mrn(rng)
        t = Row(name, dob, mrn, rng.choice("MF"), rng.choice(_STATUSES))
        s = Row(name, dob, _transpose_two(mrn, rng), t.sex, t.status)
        pairs.append(
            CollisionPair(
                "mrn_transposition",
                t,
                s,
                f"identical name+DOB, MRN digits transposed ({mrn} vs {s.mrn})",
            )
        )

    # -- identifier letter/digit confusion (l/1, O/0): same name+DOB --
    for label in ("id_confusion_l1", "id_confusion_O0"):
        dob = _dob(rng)
        name = f"{rng.choice(_SURNAMES)}, {rng.choice(_FIRSTS)}"
        if label == "id_confusion_l1":
            mrn = f"PL1{rng.randint(1000, 9999)}"
            smrn = mrn.replace("1", "l", 1)
        else:
            mrn = f"C0X{rng.randint(1000, 9999)}"
            smrn = mrn.replace("0", "O", 1)
        t = Row(name, dob, mrn, rng.choice("MF"), rng.choice(_STATUSES))
        s = Row(name, dob, smrn, t.sex, t.status)
        pairs.append(
            CollisionPair(
                label,
                t,
                s,
                f"identical name+DOB, MRN one OCR-confusable char apart "
                f"({mrn} vs {smrn})",
            )
        )

    # -- PURELY NUMERIC MRN confusion (9th reopening): same name+DOB, an
    # all-digit MRN one O/0 or l/1 glyph apart. The earlier corpus was all
    # ALPHA-PREFIXED (PL1.../C0X...), which hid the numeric hole -- a numeric
    # MRN was flagged by NEITHER the letter-only (#26) NOR the alphanumeric-mix
    # (#27/#32) predicate, so a numeric homonym VERIFIED the wrong patient.
    for label in ("id_numeric_O0", "id_numeric_l1"):
        dob = _dob(rng)
        name = f"{rng.choice(_SURNAMES)}, {rng.choice(_FIRSTS)}"
        if label == "id_numeric_O0":
            # a 6-digit MRN with 0s the sibling renders as letter O.
            mrn = f"{rng.randint(1, 5)}00{rng.randint(100, 999)}"
            smrn = mrn.replace("0", "O")
        else:
            # a 6-digit MRN with 1s the sibling renders as letter l.
            mrn = f"{rng.randint(2, 5)}1{rng.randint(200, 999)}1"
            smrn = mrn.replace("1", "l")
        t = Row(name, dob, mrn, rng.choice("MF"), rng.choice(_STATUSES))
        s = Row(name, dob, smrn, t.sex, t.status)
        pairs.append(
            CollisionPair(
                label,
                t,
                s,
                f"identical name+DOB, PURELY NUMERIC MRN one OCR-confusable glyph "
                f"apart ({mrn} vs {smrn}) -- the 9th-reopening numeric hole",
            )
        )

    # -- SPLIT numeric identifier (9th reopening): the target's numeric MRN, and
    # a sibling one glyph apart, but the recorded/observed MRN is stored with an
    # embedded space so OCR (or the fixture) presents it as FRAGMENTS. A
    # confusable glyph in any numeric fragment must still trigger abstain.
    for label in ("id_split_numeric_O0",):
        dob = _dob(rng)
        name = f"{rng.choice(_SURNAMES)}, {rng.choice(_FIRSTS)}"
        head = f"{rng.randint(1, 9)}0{rng.randint(1, 9)}"
        tail = f"0{rng.randint(10, 99)}"
        mrn = f"{head} {tail}"  # split into two numeric fragments
        smrn = mrn.replace("0", "O")  # sibling: letter O in both fragments
        t = Row(name, dob, mrn, rng.choice("MF"), rng.choice(_STATUSES))
        s = Row(name, dob, smrn, t.sex, t.status)
        pairs.append(
            CollisionPair(
                label,
                t,
                s,
                f"identical name+DOB, SPLIT numeric MRN ({mrn!r} vs {smrn!r}) -- a "
                f"confusable glyph in a numeric FRAGMENT must still abstain",
            )
        )

    return pairs


def _filler_row(rng: random.Random) -> Row:
    return Row(
        f"{rng.choice(_SURNAMES)}, {rng.choice(_FIRSTS)}",
        _dob(rng),
        _mrn(rng, prefix=rng.choice(["MG", "RC", "PT"])),
        rng.choice("MF"),
        rng.choice(_STATUSES),
    )


@dataclass
class DenseTable:
    """A dense record list: many filler rows with target/sibling pairs placed
    adjacently. ``rows`` is the full ordered row list; ``pairs`` indexes into
    it by identity."""

    rows: list[Row]
    pairs: list[CollisionPair]
    n_rows: int


def build_dense_table(seed: int, n_rows: int = 40) -> DenseTable:
    """Interleave collision pairs (target directly above sibling) among
    filler rows to reach ``n_rows`` dense rows."""
    rng = random.Random(seed * 7919 + 1)
    pairs = build_collision_pairs(seed)
    rows: list[Row] = []
    # Space the pairs through the table so each has real neighbours above and
    # below (adjacent-row bleed comes from BOTH neighbours).
    lead_filler = 3
    for _ in range(lead_filler):
        rows.append(_filler_row(rng))
    gap = max(2, (n_rows - lead_filler - 2 * len(pairs)) // max(1, len(pairs)))
    for pair in pairs:
        rows.append(pair.target)
        rows.append(pair.sibling)
        for _ in range(gap):
            rows.append(_filler_row(rng))
    while len(rows) < n_rows:
        rows.append(_filler_row(rng))
    # Never truncate below the natural length — that would drop collision
    # pairs. ``n_rows`` is a floor (a density target), not a hard cap.
    return DenseTable(rows=rows, pairs=pairs, n_rows=len(rows))


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def render_table_html(
    table: DenseTable,
    *,
    font_family: str,
    font_px: int,
    row_pad_px: int,
    top_offset_px: int,
) -> str:
    """A realistic dense clinical record-list page. ``top_offset_px`` shifts
    the whole table down a few px so a 're-visit' render rasterizes slightly
    differently (genuine cross-render OCR jitter even at the same scale)."""
    css = f"""
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family:{font_family}; background:#fff; color:#111; }}
    #hdr {{ background:#1f3a5f; color:#fff; padding:10px 16px; font-size:16px; }}
    #toolbar {{ padding:6px 16px; border-bottom:1px solid #ccc; font-size:13px;
               color:#333; }}
    table {{ border-collapse:collapse; width:100%; font-size:{font_px}px; }}
    thead th {{ text-align:left; padding:6px 14px; background:#eef2f7;
                border-bottom:2px solid #b8c4d4; color:#233; font-weight:600; }}
    tbody td {{ padding:{row_pad_px}px 14px; border-bottom:1px solid #e2e6ea;
                white-space:nowrap; }}
    tbody tr:nth-child(even) {{ background:#f7f9fb; }}
    .mrn {{ font-variant-numeric: tabular-nums; color:#333; }}
    .open {{ padding:2px 10px; border:1px solid #7a8aa0; border-radius:3px;
             background:#f0f3f7; font-size:{max(11, font_px - 2)}px; cursor:pointer; }}
    #spacer {{ height:{top_offset_px}px; }}
    """
    head = (
        "<thead><tr>"
        "<th>MRN</th><th>Patient Name</th><th>DOB</th><th>Sex</th>"
        "<th>Status</th><th>Last Seen</th><th></th>"
        "</tr></thead>"
    )
    body_rows = []
    for i, r in enumerate(table.rows):
        body_rows.append(
            f'<tr data-row="{i}">'
            f'<td class="mrn">{r.mrn}</td>'
            f'<td class="pname" data-name="{i}">{r.name}</td>'
            f"<td>{r.dob}</td>"
            f"<td>{r.sex}</td>"
            f"<td>{r.status}</td>"
            f"<td>2026-05-{(i % 27) + 1:02d}</td>"
            f'<td><button class="open" data-open="{i}">Open</button></td>'
            f"</tr>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{css}</style></head><body>"
        "<div id='hdr'>MockMed &mdash; Patient Records (demo, all data fake)</div>"
        "<div id='toolbar'>Search results &mdash; "
        f"{table.n_rows} patients</div>"
        "<div id='spacer'></div>"
        f"<table>{head}<tbody>{''.join(body_rows)}</tbody></table>"
        "</body></html>"
    )


@dataclass
class RenderCondition:
    """A named render condition. ``device_scale_factor`` is the pixel density
    (the OCR-resolution knob); font/px vary the surface, too."""

    name: str
    font_family: str
    font_px: int
    device_scale_factor: int
    row_pad_px: int


# Record is always crisp (that is how you would record a clean bundle);
# the RISK variable is the replay-time surface, so the conditions vary the
# replay render. The first is the near-zero-jitter control.
RECORD_CONDITION = RenderCondition("record", "Arial", 15, 2, 6)

REPLAY_CONDITIONS = [
    RenderCondition("hi_res_arial", "Arial", 15, 2, 6),
    RenderCondition("native_arial", "Arial", 15, 1, 6),
    RenderCondition("small_dense", "Arial", 12, 1, 3),
    RenderCondition("serif_drift", "Georgia", 15, 1, 6),
]


# ---------------------------------------------------------------------------
# Rendering + click-point extraction via Playwright
# ---------------------------------------------------------------------------


@dataclass
class RenderedFrame:
    png: bytes
    viewport: tuple[int, int]
    # row index -> (name_point, open_point, y_center, row_region) in SCREEN px
    points: dict[int, tuple[Point, Point, int, Region]]
    # row index -> (name_point_struct, open_point_struct): the DOM row text
    # under each click point EXCLUDING the clicked cell, exactly what
    # backend.structured_text_at returns for click_name / click_action.
    # Independent of render resolution/font -- the DOM carries the REAL
    # characters (digit 0 vs letter O), which closes the OCR glyph-collapse
    # class.
    structured: dict[int, tuple[Optional[str], Optional[str]]]


def render_frame(
    table: DenseTable,
    cond: RenderCondition,
    *,
    top_offset_px: int,
    viewport_w: int = 1120,
) -> RenderedFrame:
    """Render the table under ``cond`` and return the PNG plus per-row click
    points (name-cell centre and Open-button centre) in SCREEN pixels.

    Screen pixels = CSS pixels * device_scale_factor, which is exactly the
    coordinate space of the screenshot the OCR and the identity band operate
    on."""
    from playwright.sync_api import sync_playwright

    html = render_table_html(
        table,
        font_family=cond.font_family,
        font_px=cond.font_px,
        row_pad_px=cond.row_pad_px,
        top_offset_px=top_offset_px,
    )
    dsf = cond.device_scale_factor
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        # Tall viewport so the whole dense table is on one screenshot (no
        # scroll) — every target/sibling row is present and readable.
        page = browser.new_page(
            viewport={"width": viewport_w, "height": 1600},
            device_scale_factor=dsf,
        )
        page.set_content(html, wait_until="networkidle")
        # Ensure full table height captured.
        full_h = page.evaluate("document.body.scrollHeight")
        page.set_viewport_size({"width": viewport_w, "height": int(full_h) + 20})
        png = page.screenshot(full_page=True)
        vw = viewport_w * dsf
        vh = (int(full_h) + 20) * dsf

        points: dict[int, tuple[Point, Point, int, Region]] = {}
        for i in range(len(table.rows)):
            name_bb = page.eval_on_selector(
                f'[data-name="{i}"]',
                "el => { const r = el.getBoundingClientRect();"
                " return [r.x, r.y, r.width, r.height]; }",
            )
            open_bb = page.eval_on_selector(
                f'[data-open="{i}"]',
                "el => { const r = el.getBoundingClientRect();"
                " return [r.x, r.y, r.width, r.height]; }",
            )
            row_bb = page.eval_on_selector(
                f'[data-row="{i}"]',
                "el => { const r = el.getBoundingClientRect();"
                " return [r.x, r.y, r.width, r.height]; }",
            )
            name_point = (
                int((name_bb[0] + name_bb[2] / 2) * dsf),
                int((name_bb[1] + name_bb[3] / 2) * dsf),
            )
            open_point = (
                int((open_bb[0] + open_bb[2] / 2) * dsf),
                int((open_bb[1] + open_bb[3] / 2) * dsf),
            )
            y_center = int((row_bb[1] + row_bb[3] / 2) * dsf)
            row_region = (
                int(row_bb[0] * dsf),
                int(row_bb[1] * dsf),
                int(row_bb[2] * dsf),
                int(row_bb[3] * dsf),
            )
            points[i] = (name_point, open_point, y_center, row_region)

        # Structured identity text per row, via the SAME DOM query the product
        # backend runs (elementFromPoint -> enclosing row -> clone -> drop the
        # clicked cell -> textContent). The viewport was sized to the full
        # table height above, so page coords == viewport coords (no scroll).
        # Queried at BOTH click points per row so each config excludes the
        # cell it actually clicks (click_name excludes the NAME cell; the
        # excluded cell's label is mutable evidence the ladder heals through),
        # faithful to backend.structured_text_at.
        structured: dict[int, tuple[Optional[str], Optional[str]]] = {}
        js = (
            "([px, py]) => {"
            " const el = document.elementFromPoint(px, py);"
            " if (!el) return null;"
            ' const row = el.closest(\'tr, [role="row"], li,'
            ' [role="listitem"]\');'
            " if (!row) return null;"
            ' const own = el.closest(\'td, th, [role="cell"],'
            ' [role="gridcell"]\') || el;'
            " own.setAttribute('data-oaflow-own', '1');"
            " let body = '';"
            " try {"
            "   const clone = row.cloneNode(true);"
            "   const m = clone.querySelector('[data-oaflow-own=\"1\"]');"
            "   if (m) m.remove();"
            "   body = clone.textContent || '';"
            " } finally { own.removeAttribute('data-oaflow-own'); }"
            " const parts = [];"
            " const aria = row.getAttribute ?"
            " row.getAttribute('aria-label') : null;"
            " if (aria) parts.push(aria);"
            " if (body) parts.push(body);"
            " const joined = parts.join(' ').replace(/\\s+/g, ' ').trim();"
            " return joined || null; }"
        )

        def _struct_at(css_x: float, css_y: float) -> Optional[str]:
            return page.evaluate(js, [css_x, css_y])

        for i in range(len(table.rows)):
            name_bb = page.eval_on_selector(
                f'[data-name="{i}"]',
                "el => { const r = el.getBoundingClientRect();"
                " return [r.x, r.y, r.width, r.height]; }",
            )
            open_bb = page.eval_on_selector(
                f'[data-open="{i}"]',
                "el => { const r = el.getBoundingClientRect();"
                " return [r.x, r.y, r.width, r.height]; }",
            )
            name_struct = _struct_at(
                name_bb[0] + name_bb[2] / 2, name_bb[1] + name_bb[3] / 2
            )
            open_struct = _struct_at(
                open_bb[0] + open_bb[2] / 2, open_bb[1] + open_bb[3] / 2
            )
            structured[i] = (name_struct, open_struct)
        browser.close()
    return RenderedFrame(
        png=png, viewport=(vw, vh), points=points, structured=structured
    )


# ---------------------------------------------------------------------------
# Faithful record + replay band extraction
# ---------------------------------------------------------------------------
#
# These mirror, line for line, what the compiler stores and what the replayer
# reads. They import the real functions; nothing is re-implemented.

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from openadapt_flow.compiler.compile import (  # noqa: E402
    MIN_OCR_CONFIDENCE,
    _discriminative_crop_region,
)
from openadapt_flow.runtime import identity as identity_mod  # noqa: E402
from openadapt_flow.runtime.identity import (  # noqa: E402
    band_region,
    context_from_lines,
    verify_structured_identity,
    verify_target_identity,
)
from openadapt_flow.vision.ocr import ocr  # noqa: E402

_TODAY = date.today()


def record_context(
    frame: RenderedFrame, click: Point, frame_lines: Optional[list] = None
) -> tuple[Optional[str], Region]:
    """Record-time band, mirroring ``compiler.compile`` for a click step.

    Returns ``(context_text, crop_region)`` — the anchor's stored identity
    band and the template crop region (the band height and the replay-time
    exclude both derive from this crop). ``frame_lines`` may be a precomputed
    full-frame OCR (the same object the compiler caches) to avoid re-OCRing
    the record frame per click config."""
    frame_bgr = cv2.imdecode(np.frombuffer(frame.png, np.uint8), cv2.IMREAD_COLOR)
    crop_region = _discriminative_crop_region(frame_bgr, click)
    if frame_lines is None:
        frame_lines = ocr(frame.png)
    context_text = context_from_lines(
        frame_lines,
        exclude_region=crop_region,
        band=band_region(click, crop_region[3], frame.viewport),
        point=click,
        min_confidence=MIN_OCR_CONFIDENCE,
        reference_date=_TODAY,
    )
    return context_text, crop_region


@dataclass
class ReplayObservation:
    """What replay-time verification saw at a resolved point."""

    check: Any  # IdentityCheck (production: row-filtered)
    observed: str  # band text after the row filter
    observed_no_rowfilter: str  # band text BEFORE the row filter
    status_no_rowfilter: str  # verdict WITHOUT the row filter
    band_lines: list[tuple[str, Region]]  # raw band lines (post exclude/vol)
    row_filtered_lines: list[tuple[str, Region]]  # lines kept by lines_near_point
    used_upscale: bool


def _band_attempt(
    png: bytes,
    region: Optional[Region],
    point_y: int,
    exclude_region: Region,
    context_text: str,
) -> tuple[Any, str, Any, str, list[tuple[str, Region]], list[str]]:
    """One OCR pass of the band, mirroring ``Replayer._verify_identity.attempt``.

    Returns the ROW-FILTERED verdict (production) AND the no-row-filter
    verdict (counterfactual, for the adjacent-row-bleed analysis), plus the
    raw band lines with regions and the row-filtered texts.
    """
    raw = [
        line
        for line in ocr(png, region=region)
        if line.text.strip()
        and not identity_mod.regions_intersect(line.region, exclude_region)
        and not identity_mod.is_volatile_line(line.text, reference_date=_TODAY)
    ]
    near = identity_mod.lines_near_point(raw, point_y)
    observed = " ".join(line.text.strip() for line in near)
    observed_nf = " ".join(line.text.strip() for line in raw)
    check = verify_target_identity(context_text, observed, params={}, param_examples={})
    check_nf = verify_target_identity(
        context_text, observed_nf, params={}, param_examples={}
    )
    return (
        check,
        observed,
        check_nf,
        observed_nf,
        [(ln.text.strip(), ln.region) for ln in raw],
        [(ln.text.strip(), ln.region) for ln in near],
    )


def replay_observe(
    frame: RenderedFrame,
    resolved_point: Point,
    recorded_click: Point,
    crop_region: Region,
    context_text: str,
) -> ReplayObservation:
    """Replay-time observation at ``resolved_point``, mirroring
    ``Replayer._verify_identity`` including the 2x-upscale retry.

    ``recorded_click``/``crop_region`` are the anchor's record-time click and
    template crop; the exclude region is the crop translated to the resolved
    point (same offset it had from the recorded click), exactly as the
    replayer computes it."""
    band = band_region(resolved_point, crop_region[3], frame.viewport)
    exclude = (
        resolved_point[0] + (crop_region[0] - recorded_click[0]),
        resolved_point[1] + (crop_region[1] - recorded_click[1]),
        crop_region[2],
        crop_region[3],
    )
    check, observed, check_nf, obs_nf, band_lines, near_lines = _band_attempt(
        frame.png,
        band,
        resolved_point[1],
        exclude,
        context_text,
    )
    used_upscale = False
    if check.status != "verified":
        upscaled = identity_mod.upscale_crop(frame.png, band)
        if upscaled is not None:
            (retry, r_obs, retry_nf, r_obs_nf, r_lines, r_near) = _band_attempt(
                upscaled,
                None,
                (resolved_point[1] - band[1]) * 2,
                (
                    (exclude[0] - band[0]) * 2,
                    (exclude[1] - band[1]) * 2,
                    exclude[2] * 2,
                    exclude[3] * 2,
                ),
                context_text,
            )
            rank = {"unreadable": 0, "abstain": 1, "mismatch": 2, "verified": 3}
            if (rank[retry.status], retry.coverage) > (
                rank[check.status],
                check.coverage,
            ):
                # The upscaled retry runs in a 2x coordinate space; map its
                # line regions back to the base frame so geometric row
                # assignment (bleed) stays in one coordinate system.
                def _downscale(lines):
                    out = []
                    for txt, (rx, ry, rw, rh) in lines:
                        out.append(
                            (
                                txt,
                                (
                                    band[0] + rx // 2,
                                    band[1] + ry // 2,
                                    rw // 2,
                                    rh // 2,
                                ),
                            )
                        )
                    return out

                check, observed, check_nf, obs_nf = (retry, r_obs, retry_nf, r_obs_nf)
                band_lines = _downscale(r_lines)
                near_lines = _downscale(r_near)
                used_upscale = True
    return ReplayObservation(
        check=check,
        observed=observed,
        observed_no_rowfilter=obs_nf,
        status_no_rowfilter=check_nf.status,
        band_lines=band_lines,
        row_filtered_lines=near_lines,
        used_upscale=used_upscale,
    )


# ---------------------------------------------------------------------------
# Trial runner
# ---------------------------------------------------------------------------

from openadapt_flow.runtime.identity import longest_run, squash  # noqa: E402


def _nearest_row_index(region: Region, points: dict) -> int:
    """Row whose y-center is closest to a band line's y-center."""
    _, y, _, h = region
    cy = y + h // 2
    return min(points, key=lambda i: abs(points[i][2] - cy))


def _surname_readable(surname: str, band_text: str) -> bool:
    s = squash(surname)
    return len(s) >= 3 and longest_run(s, squash(band_text)) >= len(s) - 1


CLICK_CONFIGS = ("click_name", "click_action")


def run_trials(
    seeds: list[int],
    *,
    n_rows: int,
    replay_conditions: Optional[list[RenderCondition]] = None,
    progress: bool = False,
) -> dict[str, Any]:
    """Render the dense surface for each seed, record each target, and measure
    per-click false-abort (resolve on the true row) and false-accept (resolve
    on the adjacent sibling) across click configs and replay conditions.

    Returns a dict with ``trials`` (per-trial records) and ``meta``.
    """
    replay_conditions = replay_conditions or REPLAY_CONDITIONS
    trials: list[dict[str, Any]] = []
    for seed in seeds:
        table = build_dense_table(seed, n_rows=n_rows)
        rec = render_frame(table, RECORD_CONDITION, top_offset_px=0)
        rec_lines = ocr(rec.png)  # cached full-frame record OCR
        # Pre-render every replay condition once (a small vertical offset per
        # condition so each is a genuine re-render, not a byte-identical copy).
        replays = {
            cond.name: render_frame(table, cond, top_offset_px=4 + k * 3)
            for k, cond in enumerate(replay_conditions)
        }
        idx = {id(r): i for i, r in enumerate(table.rows)}
        for pair in table.pairs:
            ti = idx[id(pair.target)]
            si = idx[id(pair.sibling)]
            surname = pair.target.name.split(",")[0]
            for config in CLICK_CONFIGS:
                pi = 0 if config == "click_name" else 1  # name_point/open_point
                rec_click = rec.points[ti][pi]
                context_text, crop = record_context(rec, rec_click, rec_lines)
                armed = context_text is not None
                for cond in replay_conditions:
                    rep = replays[cond.name]
                    # False abort: resolver lands on the correct row.
                    fa_point = rep.points[ti][pi]
                    fa = replay_observe(
                        rep, fa_point, rec_click, crop, context_text or ""
                    )
                    # False accept: resolver lands on the sibling row.
                    ac_point = rep.points[si][pi]
                    ac = replay_observe(
                        rep, ac_point, rec_click, crop, context_text or ""
                    )
                    # --- Structured-text (DOM) identity path ---------------
                    # The headline: identity verified against the DOM row text
                    # (backend.structured_text_at), NOT OCR. Recorded on the
                    # target row, compared to the live DOM text at the resolved
                    # row. O and 0 are distinct in the DOM, so the digit-flanked
                    # glyph-collapse cannot occur; and it is invariant across
                    # replay font/resolution (no OCR availability cost).
                    # pi selects the click config's own point (0=name,
                    # 1=open); each excludes the cell it clicks, faithful to
                    # backend.structured_text_at at the resolved point.
                    struct_rec = (rec.structured.get(ti) or (None, None))[pi]
                    struct_true = (rep.structured.get(ti) or (None, None))[pi]
                    struct_sib = (rep.structured.get(si) or (None, None))[pi]
                    struct_armed = struct_rec is not None
                    sv_true = verify_structured_identity(struct_rec, struct_true)
                    sv_sib = verify_structured_identity(struct_rec, struct_sib)
                    struct_fa_status = (
                        sv_true.status if sv_true is not None else "unavailable"
                    )
                    struct_acc_status = (
                        sv_sib.status if sv_sib is not None else "unavailable"
                    )
                    # Adjacent-row bleed (measured on the false-abort band),
                    # assigned GEOMETRICALLY (nearest row by y-center) so a
                    # neighbour's 'M'/'Active' token — same value as the
                    # target's — is not miscounted as a target token. A
                    # neighbour token SURVIVES the row filter only when a
                    # row-filtered LINE is itself geometrically in a
                    # neighbour row.
                    bleed_neighbors = [
                        t
                        for t, reg in fa.band_lines
                        if _nearest_row_index(reg, rep.points) != ti
                    ]
                    bleed_survived = any(
                        _nearest_row_index(reg, rep.points) != ti
                        for _t, reg in fa.row_filtered_lines
                    )
                    trials.append(
                        {
                            "seed": seed,
                            "collision_class": pair.collision_class,
                            "note": pair.note,
                            "click_config": config,
                            "replay_condition": cond.name,
                            "target_name": pair.target.name,
                            "sibling_name": pair.sibling.name,
                            "target_mrn": pair.target.mrn,
                            "sibling_mrn": pair.sibling.mrn,
                            "target_dob": pair.target.dob,
                            "sibling_dob": pair.sibling.dob,
                            "armed": armed,
                            "context_text": context_text,
                            # False-abort trial
                            "fa_status": fa.check.status,
                            "fa_coverage": fa.check.coverage,
                            "fa_observed": fa.observed,
                            "fa_used_upscale": fa.used_upscale,
                            "fa_surname_readable": _surname_readable(
                                surname, fa.observed_no_rowfilter
                            ),
                            "fa_status_no_rowfilter": fa.status_no_rowfilter,
                            "is_false_abort": bool(
                                armed and fa.check.status != "verified"
                            ),
                            # False-accept trial
                            "acc_status": ac.check.status,
                            "acc_coverage": ac.check.coverage,
                            "acc_observed": ac.observed,
                            "acc_expected": context_text,
                            "acc_used_upscale": ac.used_upscale,
                            "is_false_accept": bool(
                                armed and ac.check.status == "verified"
                            ),
                            # Structured-text (DOM) identity path -- both verdicts
                            "structured_armed": struct_armed,
                            "structured_recorded": struct_rec,
                            "structured_true_live": struct_true,
                            "structured_sibling_live": struct_sib,
                            "structured_fa_status": struct_fa_status,
                            "structured_acc_status": struct_acc_status,
                            "is_structured_false_abort": bool(
                                struct_armed and struct_fa_status != "verified"
                            ),
                            "is_structured_false_accept": bool(
                                struct_armed and struct_acc_status == "verified"
                            ),
                            # Bleed
                            "bleed_neighbor_tokens": bleed_neighbors,
                            "bleed_present": bool(bleed_neighbors),
                            "bleed_survived_rowfilter": bool(bleed_survived),
                            "bleed_changed_fa_verdict": bool(
                                fa.status_no_rowfilter != fa.check.status
                            ),
                        }
                    )
                if progress:
                    print(f"  seed {seed} {pair.collision_class} {config} done")
    return {
        "trials": trials,
        "meta": {
            "seeds": seeds,
            "n_rows": n_rows,
            "replay_conditions": [c.name for c in replay_conditions],
            "record_condition": RECORD_CONDITION.name,
            "click_configs": list(CLICK_CONFIGS),
            "operating_point": {
                "coverage_threshold": identity_mod.COVERAGE_THRESHOLD,
                "uncovered_run_cap": identity_mod.UNCOVERED_RUN_CAP,
                "contradicted_chars_cap": identity_mod.CONTRADICTED_CHARS_CAP,
                "suspect_chars_cap": identity_mod.SUSPECT_CHARS_CAP,
                "unexplained_name_tokens_cap": identity_mod.UNEXPLAINED_NAME_TOKENS_CAP,
                "absent_name_token_cap": identity_mod.ABSENT_NAME_TOKEN_CAP,
            },
        },
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _rate(num: int, den: int) -> float:
    return (num / den) if den else 0.0


def aggregate(result: dict[str, Any]) -> dict[str, Any]:
    """Compute headline + per-breakdown false-abort / false-accept rates."""
    trials = result["trials"]
    armed = [t for t in trials if t["armed"]]

    def rates(rows: list[dict]) -> dict[str, Any]:
        fa = sum(t["is_false_abort"] for t in rows)
        acc = sum(t["is_false_accept"] for t in rows)
        unread = sum(
            t["is_false_abort"] and t["fa_status"] == "unreadable" for t in rows
        )
        mism = sum(t["is_false_abort"] and t["fa_status"] == "mismatch" for t in rows)
        # 8th reopening: a band resting on a glyph-confusable identifier now
        # ABSTAINS (the honest "OCR cannot certify" verdict) rather than
        # mismatch/verify -- its own false-abort bucket.
        abst = sum(t["is_false_abort"] and t["fa_status"] == "abstain" for t in rows)
        return {
            "n": len(rows),
            "false_abort": fa,
            "false_abort_rate": _rate(fa, len(rows)),
            "false_abort_mismatch": mism,
            "false_abort_unreadable": unread,
            "false_abort_abstain": abst,
            "false_accept": acc,
            "false_accept_rate": _rate(acc, len(rows)),
        }

    def group(key) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for t in armed:
            out.setdefault(key(t), []).append(t)
        return {k: rates(v) for k, v in sorted(out.items())}

    struct_armed = [t for t in trials if t.get("structured_armed")]

    def struct_rates(rows: list[dict]) -> dict[str, Any]:
        fa = sum(t.get("is_structured_false_abort", False) for t in rows)
        acc = sum(t.get("is_structured_false_accept", False) for t in rows)
        return {
            "n": len(rows),
            "false_abort": fa,
            "false_abort_rate": _rate(fa, len(rows)),
            "false_accept": acc,
            "false_accept_rate": _rate(acc, len(rows)),
        }

    def struct_group(key) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for t in struct_armed:
            out.setdefault(key(t), []).append(t)
        return {k: struct_rates(v) for k, v in sorted(out.items())}

    bleed_present = [t for t in trials if t["bleed_present"]]
    bleed_survived = [t for t in trials if t["bleed_survived_rowfilter"]]
    bleed_changed = [t for t in trials if t["bleed_changed_fa_verdict"]]
    false_accepts = [t for t in trials if t["is_false_accept"]]

    return {
        "headline": rates(armed),
        "unarmed_count": sum(1 for t in trials if not t["armed"]),
        "by_collision_class": group(lambda t: t["collision_class"]),
        "by_replay_condition": group(lambda t: t["replay_condition"]),
        "by_click_config": group(lambda t: t["click_config"]),
        "by_class_and_config": group(
            lambda t: f"{t['collision_class']}::{t['click_config']}"
        ),
        "bleed": {
            "trials": len(trials),
            "bleed_present": len(bleed_present),
            "bleed_present_rate": _rate(len(bleed_present), len(trials)),
            "bleed_survived_rowfilter": len(bleed_survived),
            "bleed_changed_fa_verdict": len(bleed_changed),
        },
        "false_accept_details": false_accepts,
        "structured_path": {
            "headline": struct_rates(struct_armed),
            "by_collision_class": struct_group(lambda t: t["collision_class"]),
            "by_click_config": struct_group(lambda t: t["click_config"]),
            "by_replay_condition": struct_group(lambda t: t["replay_condition"]),
        },
    }


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

SYNTHETIC_FALSE_ABORT = (
    0.4831  # docs/validation/IDENTITY_EVIDENCE.md, bounded synthetic evidence
)
SYNTHETIC_FALSE_ACCEPT = 0.0


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _struct_rate_table(title: str, groups: dict[str, dict]) -> list[str]:
    """Rate table for the structured-text path (no OCR mismatch/unreadable
    split -- the DOM compare is a binary verify/mismatch)."""
    out = [
        f"### {title}",
        "",
        "| group | n | false-abort (over-halt) | false-accept |",
        "| --- | --- | --- | --- |",
    ]
    for name, r in groups.items():
        out.append(
            f"| `{name}` | {r['n']} | {_pct(r['false_abort_rate'])} "
            f"({r['false_abort']}) | {_pct(r['false_accept_rate'])} "
            f"({r['false_accept']}) |"
        )
    out.append("")
    return out


def _rate_table(title: str, groups: dict[str, dict]) -> list[str]:
    out = [
        f"### {title}",
        "",
        "| group | n | false-abort | (mismatch / unreadable) | false-accept |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, r in groups.items():
        out.append(
            f"| `{name}` | {r['n']} | {_pct(r['false_abort_rate'])} "
            f"({r['false_abort']}) | {r['false_abort_mismatch']} / "
            f"{r['false_abort_unreadable']} | "
            f"{_pct(r['false_accept_rate'])} ({r['false_accept']}) |"
        )
    out.append("")
    return out


def render_markdown(result: dict[str, Any], agg: dict[str, Any]) -> str:
    h = agg["headline"]
    meta = result["meta"]
    fa_rate = h["false_abort_rate"]
    delta = fa_rate - SYNTHETIC_FALSE_ABORT
    higher = "HIGHER" if delta > 0 else "LOWER"
    lines: list[str] = []
    lines += [
        "# Dense sibling-surface false-abort / false-accept study",
        "",
        "The identity band matcher's headline numbers "
        "(`docs/validation/IDENTITY_EVIDENCE.md`: **false accept 0.000%, false "
        f"abort {_pct(SYNTHETIC_FALSE_ABORT)}**) are measured on SYNTHETIC "
        "corpora (string pairs with hand-injected OCR noise) and, at the "
        "product level, on CLEAN OpenEMR identity banners. This study "
        "measures the SAME matcher on the surface where a wrong-patient "
        "write actually does damage: a dense, sibling-heavy record LIST, "
        "rendered as HTML, screenshotted, and read by the repo's own OCR "
        "(RapidOCR). Every band string below came out of OCR reading a "
        "rendered PNG — nothing is a fabricated string.",
        "",
        "## Method (faithful to record + replay)",
        "",
        "- **Fixture**: a dense clinical record list "
        f"({meta['n_rows']} rows: MRN / Patient Name / DOB / Sex / Status / "
        "Last Seen / Open) with seeded collision siblings placed one row "
        "from their target. Rendered over "
        f"{len(meta['seeds'])} seeds "
        f"({', '.join(str(s) for s in meta['seeds'])}).",
        "- **Record** (crisp, `device_scale_factor=2`, Arial 15px): OCR the "
        "full frame and store the identity band exactly as "
        "`compiler.compile` does — `context_from_lines(...)` with the "
        "clicked cell's template crop EXCLUDED and volatile lines dropped.",
        "- **Replay**: at the resolved click point, extract the band exactly "
        "as `Replayer._verify_identity` does — `band_region`, translate the "
        "exclude crop to the resolved point, drop volatile lines, "
        "`lines_near_point` row refinement, then `verify_target_identity` "
        "with the same 2x-upscale retry. No Anthropic calls; identity + OCR "
        "only.",
        "- **Two click configs** per target: `click_name` (open the chart by "
        "clicking the name cell — the NAME is then excluded from the band, "
        "so DOB/MRN/Sex/Status carry identity) and `click_action` (click the "
        "row's Open button — the NAME stays in the band).",
        "- **Replay conditions** (the record frame is always crisp; the RISK "
        "variable is the replay surface): "
        f"{', '.join('`' + c + '`' for c in meta['replay_conditions'])} "
        "(`hi_res_arial` = same crisp dsf2 control; `native_arial` = dsf1; "
        "`small_dense` = dsf1, 12px, tighter rows; `serif_drift` = dsf1, "
        "Georgia — an app font change between record and replay).",
        "- **False abort** = resolver on the CORRECT row, identity fails to "
        "verify. **False accept** = resolver on the adjacent SIBLING row, "
        "identity verifies it as the target (catastrophic; must stay 0). "
        "Siblings are realistic different patients (distinct MRN); the "
        "confusable/transposed classes put the sole difference in the MRN.",
        "",
        f"Operating point (pinned, from the ROC): {meta['operating_point']}.",
        "",
        "## Headline (dense surface, armed clicks)",
        "",
        f"- **per-click false abort: {_pct(fa_rate)}** "
        f"({h['false_abort']}/{h['n']}) — of which "
        f"{h['false_abort_mismatch']} readable-but-mismatch and "
        f"{h['false_abort_unreadable']} unreadable.",
        f"- **per-click false accept: {_pct(h['false_accept_rate'])}** "
        f"({h['false_accept']}/{h['n']}).",
        f"- unarmed clicks (no band recorded, identity gate never runs): "
        f"{agg['unarmed_count']}.",
        "",
        f"**Versus the synthetic baseline** (false abort "
        f"{_pct(SYNTHETIC_FALSE_ABORT)}, false accept "
        f"{_pct(SYNTHETIC_FALSE_ACCEPT)}): the real dense-surface false "
        f"abort is **{_pct(fa_rate)}**, i.e. **{higher}** than the synthetic "
        f"{_pct(SYNTHETIC_FALSE_ABORT)} by {_pct(abs(delta))}. "
        + (
            "False accept STAYED 0 on real dense OCR."
            if h["false_accept"] == 0
            else f"FALSE ACCEPT DID NOT STAY 0 — {h['false_accept']} sibling "
            "rows verified as their target (details below)."
        )
        + "",
        "",
    ]

    # -- Structured-text (DOM) identity path: the headline ------------------
    sp = agg.get("structured_path")
    if sp and sp["headline"]["n"]:
        sh = sp["headline"]
        lines += [
            "## Structured-text (DOM) identity path (the headline)",
            "",
            "Identity here is verified against STRUCTURED text -- the DOM row "
            "text under the click point (`backend.structured_text_at`, the "
            "same signal a native desktop backend gets from the UIA/AX tree) "
            "-- NOT OCR. The recorded target's DOM identity string is compared "
            "to the live DOM string at the resolved row by exact/normalized "
            "match, in which `0` and `O`, `1` and `l` are DISTINCT characters. "
            "This runs on the browser backend, where the dense table's DOM is "
            "available; on a pure-pixel substrate it is unavailable and "
            "identity falls back to the OCR path measured below.",
            "",
            f"- **structured-path false accept: {_pct(sh['false_accept_rate'])}"
            f"** ({sh['false_accept']}/{sh['n']}).",
            f"- **structured-path false abort (over-halt): "
            f"{_pct(sh['false_abort_rate'])}** "
            f"({sh['false_abort']}/{sh['n']}).",
            "",
            "The digit-flanked glyph-collapse (`MG4408` vs `MG44O8`, `AC50061`"
            " vs `AC5OO61`) that produces false accepts on the OCR path in "
            "`click_action` -- and over-halts on the OCR path in `click_name` "
            "(identity resting solely on the collapsible MRN) -- does NOT "
            "occur here: the two MRNs are different strings in the DOM, so the "
            "sibling MISMATCHES and the true row VERIFIES. Because the DOM text "
            "is invariant across replay font/resolution, the structured path "
            "carries NO OCR-availability cost: it closes the class without "
            "#27's over-halt.",
            "",
        ]
        lines += _struct_rate_table(
            "Structured path -- by collision class", sp["by_collision_class"]
        )
        lines += _struct_rate_table(
            "Structured path -- by click config", sp["by_click_config"]
        )
        lines += [
            "The OCR band path (the pixel-substrate FALLBACK) is measured "
            "below. UPDATED for the 8th wrong-patient reopening: #27's "
            '"disclosed digit-flanked residual" -- a same-name/same-DOB '
            "homonym whose collapsible MRN OCR-collapses to the target's, "
            "name shown -- was a LIVE wrong-patient VERIFY (proved on the "
            "real replayer in PR #31). The OCR tier now ABSTAINS on ANY "
            "band resting on a glyph-confusable identifier, REGARDLESS of a "
            "matched name+DOB, so that residual is closed (0 false accept) "
            "at the cost of a higher halt rate on the OCR path; a "
            "different-NAME sibling still MISMATCHES and a clean name+DOB "
            "with a NON-confusable identifier still VERIFIES. The structured "
            "tier never lets the OCR fallback override a structured mismatch.",
            "",
        ]

    lines += _rate_table(
        "By replay condition (OCR resolution)", agg["by_replay_condition"]
    )
    lines += _rate_table("By click config", agg["by_click_config"])
    lines += _rate_table("By collision class", agg["by_collision_class"])
    lines += _rate_table(
        "By collision class x click config", agg["by_class_and_config"]
    )

    # Worst collision class
    worst = max(
        agg["by_collision_class"].items(),
        key=lambda kv: kv[1]["false_abort_rate"],
        default=(None, None),
    )
    lines += ["## Worst collision class", ""]
    if worst[0] is not None:
        lines.append(
            f"Highest false-abort collision class: **`{worst[0]}`** at "
            f"{_pct(worst[1]['false_abort_rate'])} "
            f"({worst[1]['false_abort']}/{worst[1]['n']})."
        )
    lines.append("")

    # Adjacent-row bleed
    b = agg["bleed"]
    lines += [
        "## Adjacent-row bleed",
        "",
        f"- Bands whose raw OCR lines included a token from a NEIGHBOUR row "
        f"(above/below the resolved row): {b['bleed_present']}/{b['trials']} "
        f"({_pct(b['bleed_present_rate'])}).",
        f"- Neighbour tokens that SURVIVED the `lines_near_point` row filter "
        f"into the identity band: {b['bleed_survived_rowfilter']}.",
        f"- Trials where the row filter CHANGED the false-abort verdict "
        f"(i.e. bleed would have changed the decision without it): "
        f"{b['bleed_changed_fa_verdict']}.",
        "",
        (
            "**Finding:** the `lines_near_point` row refinement absorbs "
            "adjacent-row bleed — neighbour tokens are picked up in the coarse "
            "64px band but filtered out before the verdict, and removing the "
            "filter would change decisions in the count above."
            if b["bleed_survived_rowfilter"] == 0
            else "**Finding:** neighbour tokens SURVIVED the row filter in "
            f"{b['bleed_survived_rowfilter']} trials — adjacent-row bleed "
            "reaches the verdict on this dense surface."
        ),
        "",
    ]

    # False-accept details (headline safety section)
    lines += ["## False accepts (headline safety finding)", ""]
    fads = agg["false_accept_details"]
    if not fads:
        lines += [
            "**Zero.** No seeded sibling was verified as its target on the "
            "real dense-OCR'd surface, across every collision class, click "
            "config, and replay condition. The catastrophic direction held.",
            "",
        ]
    else:
        lines += [
            f"**{len(fads)} FALSE ACCEPTS.** Each is a wrong-patient verify "
            "on a real rendered, OCR'd sibling row. Exact rows and band "
            "strings:",
            "",
            "| class | config | condition | target -> sibling | recorded "
            "band (expected) | observed band at sibling | cov |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for t in fads:
            lines.append(
                f"| `{t['collision_class']}` | {t['click_config']} | "
                f"{t['replay_condition']} | {t['target_name']} "
                f"({t['target_mrn']}) -> {t['sibling_name']} "
                f"({t['sibling_mrn']}) | `{t['acc_expected']}` | "
                f"`{t['acc_observed']}` | {t['acc_coverage']} |"
            )
        lines.append("")

        # Mechanism: detect the glyph-collapse signature — a false accept
        # where the recorded and observed bands are RAW-IDENTICAL after
        # squashing (OCR read the target's '0' and the sibling's 'O' as the
        # same glyph), so the match is a raw match and the confusion-suspect
        # rule never fires.
        collapse = [
            t
            for t in fads
            if squash(t["acc_expected"] or "") == squash(t["acc_observed"] or "")
        ]
        if collapse:
            lines += [
                "### Mechanism: OCR glyph-collapse defeats the string-level "
                "identifier-suspect rule",
                "",
                f"{len(collapse)} of the {len(fads)} false accepts are "
                "**raw-identical bands**: OCR read the target's identifier "
                "and the sibling's one-glyph-apart identifier as the SAME "
                "string (e.g. target MRN `C0X3834` with a digit ZERO and "
                "sibling `COX3834` with a letter O both read as `COX3834`). "
                "This is the exact class the ROC and `docs/LIMITS.md` claim "
                "the identifier-**suspect** rule closes at 0.000% false "
                "accept (v3 `id_letter_digit_collision`, 'A01234' vs "
                "'AO1234'). That defense assumes the two identifiers reach "
                "the matcher as DIFFERENT strings, so the match is "
                "confusion-only and `_suspicious_pair` fires. On the real "
                "rendered surface the confusion happens INSIDE OCR — both "
                "glyphs collapse to one string BEFORE the matcher sees them "
                "— so the bands are raw-equal, the match is a raw match, the "
                "suspect rule never triggers, and the sibling verifies. The "
                "synthetic v3 corpus cannot surface this because it injects "
                "the confusion as a text edit that keeps the two variants "
                "textually distinct, which is precisely the condition the "
                "suspect rule was built for. The glyph-collapse false accept "
                "is a property of the OCR layer, not the matcher's confusion "
                "table, and no string-level rule downstream of OCR can "
                "recover the destroyed distinction.",
                "",
                "The same instability produces the flip-side availability "
                "cost: when OCR reads the confusable glyph INCONSISTENTLY "
                "between record and replay (recorded `COX`, replayed `C0X`), "
                "the identifier now looks confusion-DIFFERENT, the suspect "
                "rule fires on the TRUE row, and the correct target safe-"
                "halts (a false abort). One unstable glyph thus drives both "
                "error directions on this class.",
                "",
            ]

    # Honest verdict
    lines += [
        "## Honest verdict (does the product clear the flagship bar?)",
        "",
        f"On the dense sibling surface the TRUE per-click false abort is "
        f"**{_pct(fa_rate)}**, {'above' if delta > 0 else 'below'} the "
        f"synthetic {_pct(SYNTHETIC_FALSE_ABORT)}. "
        + (
            "False accept stayed at 0 — the catastrophic wrong-patient "
            "direction held on real dense OCR, which is the number that "
            "gates the regulated-clinic buyer."
            if h["false_accept"] == 0
            else "False accept did NOT stay 0 — the product does NOT clear the "
            "safety bar on this surface until the classes below are closed."
        ),
        "",
        "The availability cost (false abort) is a per-click hybrid-fallback "
        "escalation (~$0.10) or a human retry; it is the cheap direction and "
        "is the price paid for the zero-false-accept posture. Selection-bias "
        "disclosure: this is measured on THIS rendered fixture + RapidOCR, "
        "not 'in the world' — a different renderer, font stack, or OCR engine "
        "would shift the false-abort rate (and could, in principle, surface a "
        "false accept the frozen confusion table does not model).",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default="benchmark/dense_surface", help="output directory"
    )
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    parser.add_argument("--n-rows", type=int, default=40)
    parser.add_argument(
        "--save-frames", action="store_true", help="save record+replay PNGs for audit"
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    result = run_trials(args.seeds, n_rows=args.n_rows, progress=args.progress)
    agg = aggregate(result)

    (out / "dense_surface.json").write_text(
        json.dumps(
            {"meta": result["meta"], "aggregate": agg, "trials": result["trials"]},
            indent=2,
        )
    )
    (out / "DENSE_SURFACE.md").write_text(render_markdown(result, agg))

    if args.save_frames:
        seed = args.seeds[0]
        table = build_dense_table(seed, n_rows=args.n_rows)
        rec = render_frame(table, RECORD_CONDITION, top_offset_px=0)
        (out / f"record_seed{seed}.png").write_bytes(rec.png)
        for k, cond in enumerate(REPLAY_CONDITIONS):
            rep = render_frame(table, cond, top_offset_px=4 + k * 3)
            (out / f"replay_{cond.name}_seed{seed}.png").write_bytes(rep.png)

    h = agg["headline"]
    print(
        f"false abort {_pct(h['false_abort_rate'])} "
        f"({h['false_abort']}/{h['n']}); "
        f"false accept {_pct(h['false_accept_rate'])} "
        f"({h['false_accept']}/{h['n']}); "
        f"wrote {out}/DENSE_SURFACE.md"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
