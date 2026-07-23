"""Pixel-perceptual identity-comparison probe.

Validates the core hypothesis behind the pixel-native identity fix tier:

    The RENDERED PIXELS retain the O/0 and l/1 distinction that OCR discards,
    so a pixel / perceptual comparison of the identifier (MRN) crop can
    distinguish a wrong-patient sibling that OCR collapses.

Context. The dense sibling-surface study
(``openadapt_flow.validation.dense_surface``) proved that RapidOCR collapses
a target MRN and a DIFFERENT patient's one-glyph-apart MRN (e.g. target
``C0X3834`` with a digit ZERO vs sibling ``COX3834`` with a letter O) to a
byte-identical string, so the string-level identifier-suspect rule never
fires and the wrong patient VERIFIES (a false accept). Every string-level
defense downstream of OCR is powerless once OCR has destroyed the
distinction. The only place the distinction still exists is the PIXELS.

This module is a standalone MEASUREMENT. It does NOT modify ``identity.py``,
the replayer, or the ``dense_surface`` harness. It REUSES the dense-surface
fixture's RENDERING (``render_table_html``) unchanged, renders the MRN cell
crop for both the target value and the OCR-colliding sibling value, and
evaluates several pixel / perceptual comparison methods at how cleanly each
SEPARATES:

- **different-value pairs** (target MRN vs OCR-colliding sibling MRN) -- must
  be judged DIFFERENT so the wrong patient is halted; from
- **same-value pairs** (the target MRN recorded vs re-rendered) -- must be
  judged SAME so the correct patient verifies (no over-halt).

It then measures ROBUSTNESS: how the same-value comparison DEGRADES under
cosmetic render drift (dark theme, 110% / 125% scale, a different
proportional font) -- the point where a naive pixel compare would false-halt
the correct patient and a VLM / robust-feature tier becomes necessary. And it
measures the TRUE FLOOR: fonts where O and 0 (or l and 1) render
pixel-identical, which no vision method can distinguish.

Run:
    python -m openadapt_flow.validation.pixel_identity_probe \
        --out benchmark/pixel_identity

ZERO Anthropic calls. OCR is used only to re-confirm the collapse baseline
(that the two identifiers DO reach the string layer as one string); the
separation itself is pure pixels.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np
from PIL import Image

from openadapt_flow.image_hash import (
    difference_hash,
    hash_distance,
    perceptual_hash,
)

# Reuse the dense-surface fixture's RENDERING unchanged (read it, call it,
# never edit it): the HTML/CSS that defines exactly how an MRN cell paints.
from openadapt_flow.validation.dense_surface import (
    DenseTable,
    Row,
    _filler_row,
    render_table_html,
)

# ---------------------------------------------------------------------------
# Collapse-pair fixture (O/0 and l/1, digit- and alpha-flanked)
# ---------------------------------------------------------------------------


@dataclass
class IdPair:
    """A target identifier and the DIFFERENT-patient sibling that OCR collapses
    onto it (one glyph apart: 0<->O or 1<->l)."""

    label: str
    target: str
    sibling: str
    glyph_class: str  # "O0" or "l1"
    flank: str  # "digit", "alpha", or "numeric" (all-digit body)
    note: str


# The digit-flanked O/0 (``MG4408`` / ``MG44O8``), the alpha-flanked
# (``C0X3834`` / ``COX3834`` -- the exact dense_surface id_confusion_O0
# class), the l/1 analogues, and -- the 9th wrong-patient reopening -- the
# PURELY NUMERIC MRNs the earlier alpha-prefixed corpus hid (``100512`` vs a
# different patient's ``1OO512``, letter O's, which OCR reads byte-identically).
# Siblings are realistic distinct patients.
COLLAPSE_PAIRS: list[IdPair] = [
    IdPair(
        "O0_digit_1",
        "MG4408",
        "MG44O8",
        "O0",
        "digit",
        "digit-flanked: target zero vs sibling letter-O (MG4408/MG44O8)",
    ),
    IdPair(
        "O0_digit_2",
        "AC50061",
        "AC5OO61",
        "O0",
        "digit",
        "digit-flanked, two zeros vs two O (AC50061/AC5OO61)",
    ),
    IdPair(
        "O0_digit_3",
        "RC90210",
        "RC9O210",
        "O0",
        "digit",
        "digit-flanked single 0 vs O (RC90210/RC9O210)",
    ),
    IdPair(
        "O0_alpha_1",
        "C0X3834",
        "COX3834",
        "O0",
        "alpha",
        "alpha-flanked 0 vs O -- dense_surface id_confusion_O0 class",
    ),
    IdPair(
        "O0_alpha_2",
        "B0X7521",
        "BOX7521",
        "O0",
        "alpha",
        "alpha-flanked 0 vs O (B0X7521/BOX7521)",
    ),
    IdPair(
        "l1_digit_1",
        "MG4118",
        "MG41l8",
        "l1",
        "digit",
        "digit-flanked 1 vs l (MG4118/MG41l8)",
    ),
    IdPair(
        "l1_digit_2",
        "AC50161",
        "AC50l61",
        "l1",
        "digit",
        "digit-flanked 1 vs l (AC50161/AC50l61)",
    ),
    IdPair(
        "l1_alpha_1",
        "PL1X904",
        "PLlX904",
        "l1",
        "alpha",
        "alpha-flanked 1 vs l (PL1X904/PLlX904)",
    ),
    IdPair(
        "l1_alpha_2",
        "RX1T552",
        "RXlT552",
        "l1",
        "alpha",
        "alpha-flanked 1 vs l (RX1T552/RXlT552)",
    ),
    # --- 9th reopening: PURELY NUMERIC MRNs (no letter prefix) ---------------
    IdPair(
        "O0_numeric_1",
        "100512",
        "1OO512",
        "O0",
        "numeric",
        "purely-numeric MRN, two 0 vs two O (100512/1OO512)",
    ),
    IdPair(
        "O0_numeric_2",
        "400761",
        "4OO761",
        "O0",
        "numeric",
        "purely-numeric MRN, two 0 vs two O (400761/4OO761)",
    ),
    IdPair(
        "O0_numeric_3",
        "501900",
        "5O19OO",
        "O0",
        "numeric",
        "purely-numeric MRN, three 0 vs three O (501900/5O19OO)",
    ),
    IdPair(
        "l1_numeric_1",
        "417063",
        "4l7063",
        "l1",
        "numeric",
        "purely-numeric MRN, digit 1 vs letter l (417063/4l7063)",
    ),
    IdPair(
        "l1_numeric_2",
        "110234",
        "ll0234",
        "l1",
        "numeric",
        "purely-numeric MRN, leading 1s vs ls (110234/ll0234)",
    ),
]


def all_values(pairs: list[IdPair]) -> list[str]:
    """Ordered, de-duplicated list of every identifier string (targets then
    siblings) -- rendered together so each value keeps a STABLE row index
    across renders."""
    vals: list[str] = []
    for p in pairs:
        for v in (p.target, p.sibling):
            if v not in vals:
                vals.append(v)
    return vals


# ---------------------------------------------------------------------------
# Render an MRN cell crop, reusing render_table_html unchanged
# ---------------------------------------------------------------------------


@dataclass
class RenderSpec:
    """A named render surface. ``dark``/``zoom`` are cosmetic drift knobs
    applied as runtime style overrides AFTER set_content -- they do NOT touch
    the fixture; ``font_family``/``font_px``/``dsf`` are the fixture's own
    knobs."""

    name: str
    font_family: str = "Arial"
    font_px: int = 15
    dsf: int = 2
    row_pad_px: int = 6
    dark: bool = False
    zoom: float = 1.0
    top_offset_px: int = 0


_DARK_CSS = (
    "body{background:#0f1720 !important;color:#e6edf3 !important;}"
    "#hdr{background:#0b1f38 !important;}"
    "#toolbar{color:#aeb9c4 !important;border-bottom-color:#333 !important;}"
    "thead th{background:#1b2530 !important;color:#dfe6ee !important;"
    "border-bottom-color:#3a4650 !important;}"
    "tbody td{border-bottom-color:#222c36 !important;}"
    "tbody tr:nth-child(even){background:#161d26 !important;}"
    ".mrn{color:#e6edf3 !important;}"
)


def _values_table(
    values: list[str], seed: int = 7
) -> tuple[DenseTable, dict[str, int]]:
    """Build a dense table with each identifier value at a STABLE, same-parity
    row index (each value row preceded by one deterministic filler row, so
    every value row lands on an odd index -> identical background stripe),
    plus lead/trail filler for realistic neighbours. Returns the table and a
    ``value -> row index`` map."""
    rng = random.Random(seed)
    rows: list[Row] = [_filler_row(rng), _filler_row(rng)]
    index: dict[str, int] = {}
    for v in values:
        rows.append(_filler_row(rng))
        index[v] = len(rows)
        rows.append(Row(f"Patient, Row{index[v]}", "1970-01-01", v, "F", "Active"))
    rows.append(_filler_row(rng))
    return DenseTable(rows=rows, pairs=[], n_rows=len(rows)), index


def render_value_crops(
    values: list[str], spec: RenderSpec, viewport_w: int = 1120
) -> dict[str, np.ndarray]:
    """Render the identifier values under ``spec`` and return the MRN CELL crop
    (BGR) for each value, cropped to the live ``.mrn`` cell bounding box.

    Reuses ``render_table_html`` for the actual pixels; the only additions are
    the runtime dark/zoom style overrides (cosmetic drift) and the per-cell
    crop extraction (which the fixture's ``render_frame`` does not expose)."""
    from playwright.sync_api import sync_playwright

    table, index = _values_table(values)
    html = render_table_html(
        table,
        font_family=spec.font_family,
        font_px=spec.font_px,
        row_pad_px=spec.row_pad_px,
        top_offset_px=spec.top_offset_px,
    )
    dsf = spec.dsf
    crops: dict[str, np.ndarray] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": viewport_w, "height": 1600},
            device_scale_factor=dsf,
        )
        page.set_content(html, wait_until="networkidle")
        if spec.dark:
            page.add_style_tag(content=_DARK_CSS)
        if spec.zoom != 1.0:
            page.add_style_tag(content=f"body{{zoom:{spec.zoom};}}")
        full_h = page.evaluate("document.body.scrollHeight")
        page.set_viewport_size({"width": viewport_w, "height": int(full_h) + 20})
        png = page.screenshot(full_page=True)
        frame = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        fh, fw = frame.shape[:2]
        for v, i in index.items():
            bb = page.eval_on_selector(
                f'[data-row="{i}"] .mrn',
                "el => { const r = el.getBoundingClientRect();"
                " return [r.x, r.y, r.width, r.height]; }",
            )
            x = max(0, int(bb[0] * dsf))
            y = max(0, int(bb[1] * dsf))
            w = int(bb[2] * dsf)
            h = int(bb[3] * dsf)
            x2 = min(fw, x + w)
            y2 = min(fh, y + h)
            crops[v] = frame[y:y2, x:x2].copy()
        browser.close()
    return crops


# ---------------------------------------------------------------------------
# Comparison methods -- each returns a DISTANCE (higher == more different)
# ---------------------------------------------------------------------------

CANON = (48, 240)  # (H, W) canonical grayscale canvas for size-sensitive methods


def _gray(bgr: np.ndarray) -> np.ndarray:
    if bgr.ndim == 3:
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return bgr


def _canon(bgr: np.ndarray) -> np.ndarray:
    g = _gray(bgr)
    return cv2.resize(g, (CANON[1], CANON[0]), interpolation=cv2.INTER_AREA)


def _pil(bgr: np.ndarray) -> Image.Image:
    return Image.fromarray(_gray(bgr))


def m_l1(a: np.ndarray, b: np.ndarray) -> float:
    ca, cb = _canon(a).astype(np.float32), _canon(b).astype(np.float32)
    return float(np.mean(np.abs(ca - cb)) / 255.0)


def m_l2(a: np.ndarray, b: np.ndarray) -> float:
    ca, cb = _canon(a).astype(np.float32), _canon(b).astype(np.float32)
    return float(np.sqrt(np.mean((ca - cb) ** 2)) / 255.0)


def m_ncc(a: np.ndarray, b: np.ndarray) -> float:
    ca, cb = _canon(a).astype(np.float32), _canon(b).astype(np.float32)
    if ca.std() < 1e-6 or cb.std() < 1e-6:
        return 0.0
    score = float(cv2.matchTemplate(ca, cb, cv2.TM_CCOEFF_NORMED)[0, 0])
    return max(0.0, 1.0 - score)


def m_ssim(a: np.ndarray, b: np.ndarray) -> float:
    from skimage.metrics import structural_similarity

    s = structural_similarity(_canon(a), _canon(b))
    return float(1.0 - s)


def m_phash(a: np.ndarray, b: np.ndarray) -> float:
    return float(
        hash_distance(
            perceptual_hash(_pil(a), hash_size=16),
            perceptual_hash(_pil(b), hash_size=16),
        )
    )


def m_dhash(a: np.ndarray, b: np.ndarray) -> float:
    return float(
        hash_distance(
            difference_hash(_pil(a), hash_size=16),
            difference_hash(_pil(b), hash_size=16),
        )
    )


def m_edge(a: np.ndarray, b: np.ndarray) -> float:
    ea = cv2.Canny(_canon(a), 60, 160) > 0
    eb = cv2.Canny(_canon(b), 60, 160) > 0
    union = np.logical_or(ea, eb).sum()
    if union == 0:
        return 0.0
    inter = np.logical_and(ea, eb).sum()
    return float(1.0 - inter / union)  # 1 - IoU of edge pixels


def m_orb(a: np.ndarray, b: np.ndarray) -> float:
    ca, cb = _canon(a), _canon(b)
    orb = cv2.ORB_create(nfeatures=200)
    ka, da = orb.detectAndCompute(ca, None)
    kb, db = orb.detectAndCompute(cb, None)
    if da is None or db is None or len(ka) < 4 or len(kb) < 4:
        return float("nan")
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    matches = bf.match(da, db)
    good = [m for m in matches if m.distance <= 32]
    ratio = len(good) / max(len(ka), len(kb))
    return float(1.0 - min(1.0, ratio))


def m_charcell(a: np.ndarray, b: np.ndarray, n_cells: int = 8) -> float:
    """Character-cell-aligned: split the canonical crop into ``n_cells`` equal
    vertical strips and take the MAX per-strip (1 - SSIM). A single collapsed
    glyph lives in ~one strip, so this ISOLATES it instead of diluting it over
    the whole identifier."""
    from skimage.metrics import structural_similarity

    ca, cb = _canon(a), _canon(b)
    w = ca.shape[1]
    step = w // n_cells
    worst = 0.0
    for k in range(n_cells):
        x0 = k * step
        x1 = w if k == n_cells - 1 else (k + 1) * step
        sa, sb = ca[:, x0:x1], cb[:, x0:x1]
        if sa.shape[1] < 7:
            continue
        s = structural_similarity(sa, sb)
        worst = max(worst, 1.0 - s)
    return float(worst)


def m_localmax(a: np.ndarray, b: np.ndarray, win: int = 24) -> float:
    """Localized max mean-abs-diff: slide a ``win``-wide window across the
    canonical crop and take the MAX window mean |diff|. Segmentation-free
    localization of the differing glyph."""
    ca = _canon(a).astype(np.float32)
    cb = _canon(b).astype(np.float32)
    d = np.abs(ca - cb)
    w = d.shape[1]
    worst = 0.0
    for x0 in range(0, w - win + 1, max(1, win // 3)):
        worst = max(worst, float(d[:, x0 : x0 + win].mean()))
    return worst / 255.0


METHODS: dict[str, Callable[[np.ndarray, np.ndarray], float]] = {
    "l1_global": m_l1,
    "l2_global": m_l2,
    "ncc_global": m_ncc,
    "ssim_global": m_ssim,
    "phash_hamming": m_phash,
    "dhash_hamming": m_dhash,
    "edge_iou": m_edge,
    "orb_feature": m_orb,
    "charcell_ssim_max": m_charcell,
    "local_maxdiff": m_localmax,
}

# Methods whose category the deliverable groups by (for the report).
METHOD_CATEGORY = {
    "l1_global": "raw pixel L1",
    "l2_global": "raw pixel L2",
    "ncc_global": "normalized cross-correlation (template)",
    "ssim_global": "SSIM",
    "phash_hamming": "perceptual hash (phash)",
    "dhash_hamming": "perceptual hash (dhash)",
    "edge_iou": "edge-map (Canny IoU)",
    "orb_feature": "feature (ORB)",
    "charcell_ssim_max": "character-cell-aligned (SSIM)",
    "local_maxdiff": "localized max abs-diff",
}


# ---------------------------------------------------------------------------
# Separation statistics
# ---------------------------------------------------------------------------


def auc(same: list[float], diff: list[float]) -> float:
    """AUC = P(distance_different > distance_same). 1.0 == perfect (different
    always scores worse than same). NaNs dropped."""
    s = [x for x in same if not np.isnan(x)]
    d = [x for x in diff if not np.isnan(x)]
    if not s or not d:
        return float("nan")
    wins = 0.0
    for ds_ in d:
        for ss_ in s:
            if ds_ > ss_:
                wins += 1.0
            elif ds_ == ss_:
                wins += 0.5
    return wins / (len(s) * len(d))


def separation(same: list[float], diff: list[float]) -> dict:
    s = [x for x in same if not np.isnan(x)]
    d = [x for x in diff if not np.isnan(x)]
    if not s or not d:
        return {"auc": float("nan"), "clean": False}
    max_same, min_diff = max(s), min(d)
    clean = min_diff > max_same
    return {
        "auc": auc(s, d),
        "same_max": max_same,
        "same_median": float(np.median(s)),
        "diff_min": min_diff,
        "diff_median": float(np.median(d)),
        "clean_separation": clean,
        "gap": min_diff - max_same,
        "threshold": (max_same + min_diff) / 2.0 if clean else None,
        "n_same": len(s),
        "n_diff": len(d),
    }


# ---------------------------------------------------------------------------
# OCR-collapse baseline (confirms OCR discards what the pixels keep)
# ---------------------------------------------------------------------------


def ocr_string(bgr: np.ndarray) -> str:
    from openadapt_flow.runtime.identity import squash
    from openadapt_flow.vision.ocr import ocr

    ok, png = cv2.imencode(".png", bgr)
    if not ok:
        return ""
    lines = ocr(png.tobytes())
    return squash(" ".join(ln.text for ln in lines))


# ---------------------------------------------------------------------------
# Experiment driver
# ---------------------------------------------------------------------------

STABLE_REF = RenderSpec("stable_ref", top_offset_px=0)
STABLE_RERENDER = RenderSpec("stable_rerender", top_offset_px=7)

DRIFT_SPECS = [
    RenderSpec("dark_theme", dark=True, top_offset_px=7),
    RenderSpec("scale_110", zoom=1.10, top_offset_px=7),
    RenderSpec("scale_125", zoom=1.25, top_offset_px=7),
    RenderSpec("font_georgia", font_family="Georgia", top_offset_px=7),
    RenderSpec("font_verdana", font_family="Verdana", top_offset_px=7),
    RenderSpec("font_times", font_family="Times New Roman", top_offset_px=7),
]

# Fonts probed for the pixel-identical glyph FLOOR.
FLOOR_FONTS = [
    "Arial",
    "Helvetica",
    "Verdana",
    "Tahoma",
    "Trebuchet MS",
    "Georgia",
    "Times New Roman",
    "Courier New",
    "Courier",
    "Menlo",
    "Monaco",
    "Andale Mono",
    "Comic Sans MS",
    "monospace",
]


def run_separation(pairs: list[IdPair], *, with_ocr: bool = True) -> dict:
    """Core experiment: render stable reference + re-render, build same-value
    and different-value distance distributions per method, and confirm the OCR
    collapse baseline."""
    values = all_values(pairs)
    ref = render_value_crops(values, STABLE_REF)
    rer = render_value_crops(values, STABLE_RERENDER)

    # OCR collapse baseline: do the target and sibling read as one string?
    ocr_rows = []
    if with_ocr:
        for p in pairs:
            ot, os_ = ocr_string(ref[p.target]), ocr_string(ref[p.sibling])
            ocr_rows.append(
                {
                    "label": p.label,
                    "target": p.target,
                    "sibling": p.sibling,
                    "ocr_target": ot,
                    "ocr_sibling": os_,
                    "ocr_collapsed": ot == os_ and ot != "",
                }
            )

    per_method: dict[str, dict] = {}
    sample_rows: list[dict] = []
    for name, fn in METHODS.items():
        same, diff = [], []
        for v in values:  # same value across two renders
            same.append(fn(ref[v], rer[v]))
        for p in pairs:  # target vs sibling, cross-render
            d1 = fn(ref[p.target], rer[p.sibling])
            d2 = fn(ref[p.sibling], rer[p.target])
            diff.extend([d1, d2])
        per_method[name] = {
            "category": METHOD_CATEGORY[name],
            "separation": separation(same, diff),
            "same": same,
            "diff": diff,
        }

    # Per-pair distances for the recommended method (audit detail).
    for p in pairs:
        row = {
            "label": p.label,
            "target": p.target,
            "sibling": p.sibling,
            "glyph_class": p.glyph_class,
            "flank": p.flank,
        }
        for name, fn in METHODS.items():
            row[f"{name}__diff"] = fn(ref[p.target], rer[p.sibling])
            row[f"{name}__same_target"] = fn(ref[p.target], rer[p.target])
        sample_rows.append(row)

    return {
        "methods": per_method,
        "ocr_baseline": ocr_rows,
        "per_pair": sample_rows,
        "n_values": len(values),
        "n_pairs": len(pairs),
    }


def run_drift(pairs: list[IdPair], recommended: list[str]) -> dict:
    """Render the SAME target values under cosmetic drift and measure how far
    the same-value distance degrades vs the stable different-value floor
    (min diff distance) for the recommended method(s)."""
    values = [p.target for p in pairs]
    ref = render_value_crops(values, STABLE_REF)
    out: dict[str, dict] = {}
    for spec in DRIFT_SPECS:
        drift = render_value_crops(values, spec)
        rec: dict[str, list[float]] = {m: [] for m in recommended}
        for v in values:
            for m in recommended:
                rec[m].append(METHODS[m](ref[v], drift[v]))
        out[spec.name] = {
            m: {
                "same_value_median": float(np.median(rec[m])),
                "same_value_max": float(np.max(rec[m])),
                "same_value_min": float(np.min(rec[m])),
            }
            for m in recommended
        }
    return out


# ---------------------------------------------------------------------------
# Pixel-identical-font floor
# ---------------------------------------------------------------------------


def _glyph_crop(page, ch: str, font_family: str, px: int = 80) -> np.ndarray:
    box_w, box_h = 120, 150
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "*{margin:0;padding:0;} body{background:#fff;}"
        f"#g{{display:block;width:{box_w}px;height:{box_h}px;"
        f"font-family:{font_family};font-size:{px}px;line-height:{box_h}px;"
        "text-align:center;color:#000;background:#fff;}"
        "</style></head><body>"
        f"<div id='g'>{ch}</div></body></html>"
    )
    page.set_content(html, wait_until="networkidle")
    bb = page.eval_on_selector(
        "#g",
        "el => { const r = el.getBoundingClientRect();"
        " return [r.x, r.y, r.width, r.height]; }",
    )
    png = page.screenshot(
        clip={"x": bb[0], "y": bb[1], "width": bb[2], "height": bb[3]}
    )
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    return img


def run_font_floor(fonts: list[str]) -> list[dict]:
    """For each font, render O/0 and l/1 as isolated glyphs and report whether
    the two render pixel-identical (max abs diff == 0) -- the true floor no
    vision method can cross."""
    from playwright.sync_api import sync_playwright

    rows: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(device_scale_factor=2)
        for ff in fonts:
            g = {c: _glyph_crop(page, c, ff) for c in ("O", "0", "l", "1", "I")}

            def _cmp(x: str, y: str) -> dict:
                a, b = g[x], g[y]
                h = min(a.shape[0], b.shape[0])
                w = min(a.shape[1], b.shape[1])
                a2, b2 = a[:h, :w].astype(np.int16), b[:h, :w].astype(np.int16)
                d = np.abs(a2 - b2)
                return {
                    "max_abs_diff": int(d.max()),
                    "mean_abs_diff": float(d.mean()),
                    "identical": bool(d.max() == 0),
                    "near_identical": bool(d.mean() < 0.5),
                }

            rows.append(
                {
                    "font": ff,
                    "O_vs_0": _cmp("O", "0"),
                    "l_vs_1": _cmp("l", "1"),
                    "l_vs_I": _cmp("l", "I"),
                }
            )
        browser.close()
    return rows


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _fmt(x) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        if np.isnan(x):
            return "nan"
        return f"{x:.4f}"
    return str(x)


def render_markdown(
    sep: dict, drift: dict, floor: list[dict], recommended: list[str]
) -> str:
    L: list[str] = []
    ap = L.append
    ap("# Pixel-perceptual identity-comparison probe")
    ap("")
    ap(
        "**Hypothesis under test.** The rendered pixels retain the O/0 and l/1 "
        "distinction that OCR discards, so a pixel / perceptual comparison of "
        "the identifier (MRN) crop can distinguish a wrong-patient sibling that "
        "OCR collapses. This is a standalone measurement that de-risks the "
        "pixel-native fix tier (tier 3) before integration. It does NOT modify "
        "`identity.py`, the replayer, or the `dense_surface` harness -- it "
        "reuses `render_table_html` unchanged and only adds crop extraction and "
        "pixel comparison. Zero Anthropic calls."
    )
    ap("")

    # OCR baseline
    ap("## OCR collapse baseline (what the pixels must overcome)")
    ap("")
    ap(
        "Each pair is rendered and the MRN cell OCR'd with the repo's own "
        "RapidOCR. If the target and its one-glyph-apart sibling read as the "
        "SAME string, every string-level identifier rule downstream is blind to "
        "the difference -- only the pixels still carry it."
    )
    ap("")
    ap("| pair | target | sibling | OCR(target) | OCR(sibling) | collapsed? |")
    ap("| --- | --- | --- | --- | --- | --- |")
    n_collapse = 0
    for r in sep["ocr_baseline"]:
        n_collapse += 1 if r["ocr_collapsed"] else 0
        ap(
            f"| `{r['label']}` | `{r['target']}` | `{r['sibling']}` | "
            f"`{r['ocr_target']}` | `{r['ocr_sibling']}` | "
            f"{'YES' if r['ocr_collapsed'] else 'no'} |"
        )
    ap("")
    ap(
        f"**{n_collapse}/{len(sep['ocr_baseline'])} pairs collapse under OCR** "
        "-- target and sibling become byte-identical strings, exactly the "
        "wrong-patient false-accept mechanism `dense_surface` found. The "
        "question is whether the pixels separate what these strings cannot."
    )
    ap("")

    # Separation per method
    ap("## Separation per method (same-value vs different-value on the collapse crops)")
    ap("")
    ap(
        "Same-value distance = the target (or sibling) MRN crop **recorded vs "
        "re-rendered** (must stay LOW so the correct patient verifies). "
        "Different-value distance = target crop vs OCR-colliding sibling crop, "
        "cross-render (must be HIGH so the wrong patient halts). `AUC` = "
        "P(different > same); `clean` = every different-value scored strictly "
        "worse than every same-value (a threshold splits them with no overlap)."
    )
    ap("")
    ap(
        "| method | category | AUC | same median | same max | diff min | "
        "diff median | clean split | threshold |"
    )
    ap("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    ranked = sorted(
        sep["methods"].items(),
        key=lambda kv: (
            kv[1]["separation"].get("auc") or 0,
            kv[1]["separation"].get("gap") or -9,
        ),
        reverse=True,
    )
    for name, m in ranked:
        s = m["separation"]
        ap(
            f"| `{name}` | {m['category']} | {_fmt(s.get('auc'))} | "
            f"{_fmt(s.get('same_median'))} | {_fmt(s.get('same_max'))} | "
            f"{_fmt(s.get('diff_min'))} | {_fmt(s.get('diff_median'))} | "
            f"{'YES' if s.get('clean_separation') else 'no'} | "
            f"{_fmt(s.get('threshold'))} |"
        )
    ap("")

    clean = [n for n, m in ranked if m["separation"].get("clean_separation")]
    ap(
        f"**Cleanly separating methods (AUC 1.0, no overlap): "
        f"{', '.join('`' + c + '`' for c in clean) if clean else 'NONE'}.**"
    )
    ap("")
    ap(
        "Two honest caveats on this table. (1) The same-value distance is "
        "**0.0** for every bounded method: a re-render at the IDENTICAL config "
        "(same font/scale/theme, only a few-pixel vertical offset that the "
        "cell-crop realigns) is byte-identical, so the stable-render separation "
        "is trivially perfect and any method suffices. The realistic same-value "
        "noise -- and the real test of a threshold -- is the cosmetic-drift "
        "section below. (2) `orb_feature` returns nan: the MRN crop is too "
        "small and low-texture for ORB to find stable keypoints, so feature "
        "matching is not usable at this crop size (a documented negative "
        "result, not a separation)."
    )
    ap("")

    # Recommendation
    ap("## Recommended method + threshold")
    ap("")
    for name in recommended:
        s = sep["methods"][name]["separation"]
        ap(
            f"- **`{name}`** ({sep['methods'][name]['category']}): AUC "
            f"{_fmt(s.get('auc'))}, "
            + (
                f"clean split at threshold **{_fmt(s.get('threshold'))}** "
                f"(same-value up to {_fmt(s.get('same_max'))}, different-value "
                f"from {_fmt(s.get('diff_min'))} -- a gap of "
                f"{_fmt(s.get('gap'))})."
                if s.get("clean_separation")
                else f"NO clean split (same max {_fmt(s.get('same_max'))} >= "
                f"diff min {_fmt(s.get('diff_min'))})."
            )
        )
    ap("")

    # Drift degradation
    ap("## Cosmetic-drift degradation (where same-value starts to false-halt)")
    ap("")
    ap(
        "The SAME target MRN is re-rendered under cosmetic drift and compared "
        "to its stable reference crop. If the same-value distance climbs past "
        "the different-value floor (`diff min` above), a naive pixel compare "
        "can no longer tell 'same patient, drifted render' from 'different "
        "patient' -- it would FALSE-HALT the correct patient. That is the "
        "escalation point to a VLM / robust-feature tier."
    )
    ap("")
    for name in recommended:
        s = sep["methods"][name]["separation"]
        floor_v = s.get("diff_min")
        thr = s.get("threshold")
        ap(
            f"### `{name}` (stable different-value floor = {_fmt(floor_v)}, "
            f"clean threshold = {_fmt(thr)})"
        )
        ap("")
        ap(
            "| drift condition | same-value median | same-value max | "
            "crosses diff-floor? | verdict |"
        )
        ap("| --- | --- | --- | --- | --- |")
        for cond, md in drift.items():
            mv = md[name]["same_value_median"]
            mx = md[name]["same_value_max"]
            crosses = floor_v is not None and mx >= floor_v
            verdict = (
                "FALSE-HALT RISK (needs VLM tier)" if crosses else "still separable"
            )
            ap(
                f"| `{cond}` | {_fmt(mv)} | {_fmt(mx)} | "
                f"{'YES' if crosses else 'no'} | {verdict} |"
            )
        ap("")

    # Font floor
    ap("## Pixel-identical-font floor (the true limit)")
    ap("")
    ap(
        "O/0 and l/1 rendered as isolated glyphs in common fonts. Where the two "
        "glyphs render pixel-identical (`max abs diff == 0`), NO vision method "
        "-- pixel, perceptual, or VLM -- can distinguish them; the distinction "
        "does not exist in the raster. This is a real, disclosed limit."
    )
    ap("")
    ap(
        "| font | O vs 0 max-diff | O/0 identical | l vs 1 max-diff | "
        "l/1 identical | l vs I max-diff |"
    )
    ap("| --- | --- | --- | --- | --- | --- |")
    id_o0, id_l1 = [], []
    for r in floor:
        o0, l1, lI = r["O_vs_0"], r["l_vs_1"], r["l_vs_I"]
        if o0["identical"]:
            id_o0.append(r["font"])
        if l1["identical"]:
            id_l1.append(r["font"])
        ap(
            f"| `{r['font']}` | {o0['max_abs_diff']} | "
            f"{'IDENTICAL' if o0['identical'] else 'distinct'} | "
            f"{l1['max_abs_diff']} | "
            f"{'IDENTICAL' if l1['identical'] else 'distinct'} | "
            f"{lI['max_abs_diff']} |"
        )
    ap("")
    ap(
        f"- Fonts where **O and 0 are pixel-identical**: "
        f"{', '.join('`' + f + '`' for f in id_o0) if id_o0 else 'none of those tested'}."
    )
    ap(
        f"- Fonts where **l and 1 are pixel-identical**: "
        f"{', '.join('`' + f + '`' for f in id_l1) if id_l1 else 'none of those tested'}."
    )
    ap("")

    # Verdict
    best = recommended[0]
    bsep = sep["methods"][best]["separation"]
    closes = bsep.get("clean_separation")
    ap("## Verdict")
    ap("")
    ap(
        "**Does pixel-perceptual comparison of the identifier crop close the "
        "OCR-collapse wrong-patient gap on pure pixels (no DOM / a11y)?**"
    )
    ap("")
    if closes:
        ap(
            f"**YES, on stable renders.** OCR collapsed {n_collapse}/"
            f"{len(sep['ocr_baseline'])} target/sibling pairs to identical "
            f"strings, yet `{best}` separates every different-value pair from "
            f"every same-value pair with AUC {_fmt(bsep.get('auc'))} and a "
            f"clean threshold ({_fmt(bsep.get('threshold'))}). The pixels DO "
            "retain what OCR discards. A cheap pixel-compare (tier 3) is "
            "sufficient to catch the wrong-patient sibling when the replay "
            "render matches the recorded render."
        )
    else:
        ap(
            f"**Partially.** `{best}` reaches AUC {_fmt(bsep.get('auc'))} but "
            "does not achieve a fully clean split on this set; see the table."
        )
    ap("")
    # Determine escalation point from the drift table for the best method.
    floor_v = bsep.get("diff_min")
    escalate = []
    for cond, md in drift.items():
        if floor_v is not None and md[best]["same_value_max"] >= floor_v:
            escalate.append(cond)
    ap(
        "**At what render-drift point does it need a more robust (VLM / "
        "feature) tier?**"
    )
    ap("")
    if escalate:
        ap(
            f"The clean pixel separation holds only while the replay render "
            f"tracks the recorded one. It BREAKS under: "
            f"{', '.join('`' + c + '`' for c in escalate)} -- there the same "
            f"(correct) patient's drifted crop scores at or beyond the "
            f"different-patient floor, so a pixel-only compare would false-halt "
            f"the right patient. Those drifts are exactly where tier 4 (a VLM / "
            f"drift-robust feature comparison) must take over."
        )
    else:
        ap(
            "No tested drift pushed the same-value distance past the "
            "different-value floor for the best method, but font substitution "
            "and the pixel-identical-font floor above still require a semantic "
            "tier for the hardest cases."
        )
    ap("")
    ap(
        "**Bottom line for the tiering:** on a STABLE render the cheap pixel "
        "compare (tier 3) alone catches the OCR-collapse wrong-patient case "
        "that every string rule misses; the VLM (tier 4) is needed only once "
        "the replay render drifts in scale / font / theme past the points "
        "above, and can never recover a pixel-identical-font collapse."
    )
    ap("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="benchmark/pixel_identity")
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="skip the RapidOCR collapse baseline (faster)",
    )
    parser.add_argument(
        "--recommended",
        nargs="+",
        default=[],
        help="force these methods first in the recommendation "
        "(default: choose by interpretability + drift "
        "tolerance among cleanly-separating methods)",
    )
    parser.add_argument("--save-crops", action="store_true")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    sep = run_separation(COLLAPSE_PAIRS, with_ocr=not args.no_ocr)
    # Pick the recommended list: keep only methods that exist; put the best
    # cleanly-separating one first.
    # All cleanly-separating methods reach AUC 1.0 on stable renders, so the
    # choice among them is by INTERPRETABILITY + drift tolerance, not by a
    # cross-method gap comparison (the hashes' Hamming gap is not comparable to
    # a bounded [0,1] metric's gap). Prefer a bounded, interpretable metric
    # that LOCALIZES the differing glyph.
    clean = [
        n for n, m in sep["methods"].items() if m["separation"].get("clean_separation")
    ]
    preference = args.recommended + [
        "local_maxdiff",
        "ssim_global",
        "charcell_ssim_max",
        "ncc_global",
        "l1_global",
        "l2_global",
        "edge_iou",
        "dhash_hamming",
        "phash_hamming",
    ]
    seen: set[str] = set()
    ordered = [m for m in preference if m in clean and not (m in seen or seen.add(m))]
    if not ordered:  # nothing separated cleanly; fall back to best AUC
        ordered = [
            max(
                sep["methods"],
                key=lambda n: sep["methods"][n]["separation"].get("auc") or 0,
            )
        ]
    best = ordered[0]
    recommended = ordered[:4]

    drift = run_drift(COLLAPSE_PAIRS, recommended)
    floor = run_font_floor(FLOOR_FONTS)

    md = render_markdown(sep, drift, floor, recommended)
    (out / "PIXEL_IDENTITY.md").write_text(md)

    # Strip the big raw sample arrays from the JSON for readability but keep
    # the separation stats, per-pair distances, drift, and floor.
    methods_out = {
        n: {"category": m["category"], "separation": m["separation"]}
        for n, m in sep["methods"].items()
    }
    (out / "pixel_identity.json").write_text(
        json.dumps(
            {
                "methods": methods_out,
                "ocr_baseline": sep["ocr_baseline"],
                "per_pair": sep["per_pair"],
                "drift": drift,
                "font_floor": floor,
                "recommended": recommended,
                "best": best,
            },
            indent=2,
        )
    )

    if args.save_crops:
        values = all_values(COLLAPSE_PAIRS)
        ref = render_value_crops(values, STABLE_REF)
        for p in COLLAPSE_PAIRS[:3]:
            cv2.imwrite(str(out / f"crop_{p.label}_target.png"), ref[p.target])
            cv2.imwrite(str(out / f"crop_{p.label}_sibling.png"), ref[p.sibling])

    bsep = sep["methods"][best]["separation"]
    print(
        f"best method: {best} AUC={_fmt(bsep.get('auc'))} "
        f"clean={bsep.get('clean_separation')} "
        f"threshold={_fmt(bsep.get('threshold'))}; wrote {out}/PIXEL_IDENTITY.md"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
