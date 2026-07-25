#!/usr/bin/env python3
"""Apple Vision OCR for PowerChart section screenshots.

Replaces the PaddleOCR pass. Vision runs on-device, needs no model download,
and returns bounding boxes, which lets us rebuild the "Verified Local Record
Data" tables by row/column instead of guessing from flat text.

Usage:
  python app-run/powerchart/scripts/vision_ocr.py \\
      --run app-run/powerchart/out/080-975-37 \\
      --out app-run/powerchart/out/080-975-37/080-975-37.json \\
      --mrn 080-975-37
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
from pathlib import Path

import Quartz
import Vision
from Foundation import NSURL

# Force pyobjc to resolve ImageIO/Vision selectors on the importing thread.
# Concurrent first-touch of lazy attrs races inside objc._lazyimport.
_ = (
    Quartz.CGImageSourceCreateWithURL,
    Quartz.CGImageSourceCreateImageAtIndex,
    Quartz.CGImageGetWidth,
    Quartz.CGImageGetHeight,
    Vision.VNRecognizeTextRequest,
    Vision.VNImageRequestHandler,
    Vision.VNRequestTextRecognitionLevelAccurate,
    Vision.VNRequestTextRecognitionLevelFast,
)

# Table starts here; these end it.
VERIFIED_ANCHOR = "verified local record data"
SECTION_ENDS = (
    "unverified data from outside sources",
    "important links",
    "recommendations",
    "reminders",
    "reconciliation status",
    "home medications",
    "import outside records",
    "return to review",
)
# Embedded-list pager on dense charts ("Page 1 of 3" + First/Prev/Next/Last).
PAGE_RE = re.compile(r"page\s+(\d+)\s+of\s+(\d+)", re.IGNORECASE)
# Header of the separate encounter list that shares the Histories widget
# (Name | Condition Type | Effective Date) — not the verified table.
OTHER_LIST_MARKERS = ("condition type", "effective date")
# Encounter-list rows carry datetimes like "DEC 13, 2021 18:00".
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
# Unverified outside-source rows carry "New procedure/problem/drug allergy found".
UNVERIFIED_ROW_RE = re.compile(r"new .{0,24}found", re.IGNORECASE)
# Non-record texts that can land in the name column on continuation pages
# (section headings, column headers, pager buttons).
NON_RECORD_NAMES = {
    "allergies",
    "histories",
    "problem history",
    "procedure history",
    "substance",
    "name",
    "procedure",
    "severity",
    "reactions",
    "classification",
    "date",
    "actions",
    "view",
    "first",
    "previous",
    "next",
    "last",
    "none",
}
# Fallbacks when a frame has no readable column header (window ~3440 wide).
NAME_X = (630, 700)
DETAIL_PANEL_X = 3000
# Prefer deriving these from the live header row — see detect_columns().
try:
    from pc_config import (  # noqa: WPS433
        COLUMN_HEADER_ALIASES,
        LAYOUT,
        NAME_COLUMN,
    )
except ImportError:  # pragma: no cover
    COLUMN_HEADER_ALIASES = {}
    NAME_COLUMN = {
        "allergies": "substance",
        "problem_history": "name",
        "procedure_history": "procedure",
    }

    class _L:  # noqa: N801
        content_x0 = 0.17
        content_x1 = 0.87
        content_y0 = 0.32
        detail_x0 = 0.87

    LAYOUT = _L()

MONTHS = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)


def clean_name(text: str) -> str:
    """Drop the leading severity/warning glyph Vision reads as 'A', '-' or '•'."""
    t = text.strip()
    for pre in ("A ", "• ", "- ", "· ", "® ", "@ "):
        if t.startswith(pre):
            t = t[len(pre) :].strip()
    # Row-action glyphs at the name's right edge get read as stray symbols.
    return re.sub(r"[\s@•·®|_\-]+$", "", t)


MONTH_DATE = re.compile(
    r"\b(?:" + "|".join(MONTHS) + r")\s*\d{1,2},\s*\d{4}\b", re.IGNORECASE
)


def is_date(text: str) -> bool:
    t = text.strip()
    return bool(MONTH_DATE.search(t)) or (t.isdigit() and len(t) == 4)


def find_month_date(cells: list[str]) -> str | None:
    """Narrow columns make Vision merge source + date into one cell."""
    for c in cells:
        m = MONTH_DATE.search(c)
        if m:
            mon, day, year = re.split(r"[\s,]+", m.group(0).strip())
            return f"{mon.upper()} {int(day):02d}, {year}"
    return None


def find_source(cells: list[str]) -> str | None:
    for c in cells:
        if "local record" in c.lower():
            # Strip a merged-in date and any wrapped reaction text before it.
            s = MONTH_DATE.sub("", c).strip()
            i = s.lower().find("local record")
            return s[i:].strip(" ,") or None
    return None


def ocr_lines(
    path: Path,
    min_conf: float = 0.3,
    *,
    fast: bool = False,
) -> list[dict]:
    """Return [{text, x, y, w, h, conf}] in top-left pixel coordinates.

    fast=True uses VNRequestTextRecognitionLevelFast — good enough for
    mid-capture gates (stop markers, tabs, pager, nav labels). Final table
    extract keeps Accurate (fast=False).
    """
    url = NSURL.fileURLWithPath_(str(path))
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        raise RuntimeError(f"cannot read image: {path}")
    image = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    iw = Quartz.CGImageGetWidth(image)
    ih = Quartz.CGImageGetHeight(image)

    req = Vision.VNRecognizeTextRequest.alloc().init()
    level = (
        Vision.VNRequestTextRecognitionLevelFast
        if fast
        else Vision.VNRequestTextRecognitionLevelAccurate
    )
    req.setRecognitionLevel_(level)
    req.setUsesLanguageCorrection_(False)  # clinical terms beat the language model
    req.setRecognitionLanguages_(["en-US"])

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(f"Vision failed on {path}: {err}")

    out: list[dict] = []
    for obs in req.results() or []:
        cand = obs.topCandidates_(1)
        if not cand:
            continue
        c = cand[0]
        if c.confidence() < min_conf:
            continue
        bb = obs.boundingBox()  # normalized, origin bottom-left
        x = bb.origin.x * iw
        w = bb.size.width * iw
        h = bb.size.height * ih
        y = (1.0 - bb.origin.y - bb.size.height) * ih
        out.append(
            {
                "text": c.string(),
                "x": round(x, 1),
                "y": round(y, 1),
                "w": round(w, 1),
                "h": round(h, 1),
                "conf": round(float(c.confidence()), 3),
            }
        )
    out.sort(key=lambda d: (d["y"], d["x"]))
    return out


def ocr_text(
    path: Path,
    crop: tuple[int, int, int, int] | None = None,
    min_conf: float = 0.3,
    *,
    fast: bool = False,
) -> str:
    """Joined OCR text for a full frame or a pixel crop (x0,y0,x1,y1).

    Crops by filtering Vision boxes — no temp crop PNG, one OCR pass.
    """
    lines = ocr_lines(path, min_conf=min_conf, fast=fast)
    if crop is not None:
        x0, y0, x1, y1 = crop
        lines = [
            l
            for l in lines
            if x0 <= l["x"] + l["w"] / 2 <= x1 and y0 <= l["y"] + l["h"] / 2 <= y1
        ]
    return " ".join(l["text"] for l in lines)


def peek_progress(
    path: Path,
    kind: str,
    carry: str | None = None,
    *,
    fast: bool = True,
) -> tuple[set[str], int | None, str | None]:
    """Mid-capture peek: (name_keys, ui_count, end_state).

    fast=True for routine scroll gates; pass fast=False to confirm count-met.
    """
    bands = {k: tuple(v) for k, v in COLUMNS[kind].items()}
    recs, expected, state = frame_records(
        ocr_lines(path, fast=fast), carry, kind=kind, bands=bands
    )
    keys = {_name_key(r["name"]) for r in recs if _name_key(r["name"])}
    return keys, expected, state


def group_rows(lines: list[dict], tol: float = 14.0) -> list[list[dict]]:
    """Cluster lines into visual table rows by y-center."""
    rows: list[list[dict]] = []
    for ln in sorted(lines, key=lambda d: d["y"]):
        cy = ln["y"] + ln["h"] / 2
        for row in rows:
            rcy = sum(r["y"] + r["h"] / 2 for r in row) / len(row)
            if abs(cy - rcy) <= tol:
                row.append(ln)
                break
        else:
            rows.append([ln])
    for row in rows:
        row.sort(key=lambda d: d["x"])
    return rows


# Column x-bands from the PowerChart tables (window width ~3440).
COLUMNS = {
    "allergies": {
        "substance": (630, 900),
        "severity": (1050, 1450),
        "reactions": (1450, 1900),
        "originating_source": (1900, 2250),
        "last_modified_date": (2250, 2700),
    },
    "problem_history": {
        "name": (630, 1700),
        "classification": (1800, 2050),
        "originating_source": (2050, 2450),
        "last_modified_date": (2450, 2850),
    },
    "procedure_history": {
        "procedure": (630, 1450),
        "date": (1750, 2050),
        "originating_source": (2050, 2350),
        "last_modified_date": (2350, 2700),
    },
}

SECTION_META = {
    "allergies": {
        "section": "Allergies",
        "tab": None,
        "subsection": "Verified Local Record Data",
        "columns": [
            "substance",
            "severity",
            "reactions",
            "originating_source",
            "last_modified_date",
        ],
    },
    "problem_history": {
        "section": "Histories",
        "tab": "Problem History",
        "subsection": "Verified Local Record Data",
        "columns": [
            "name",
            "classification",
            "originating_source",
            "last_modified_date",
        ],
    },
    "procedure_history": {
        "section": "Histories",
        "tab": "Procedure History",
        "subsection": "Verified Local Record Data",
        "columns": [
            "procedure",
            "date",
            "originating_source",
            "last_modified_date",
        ],
    },
}


def _image_width(lines: list[dict]) -> float:
    if not lines:
        return 3440.0
    return max(l["x"] + l["w"] for l in lines) or 3440.0


def detail_x_cutoff(lines: list[dict]) -> float:
    """Right edge of the content pane (left of the detail flyout)."""
    w = _image_width(lines)
    return LAYOUT.detail_x0 * w


def content_band(lines: list[dict]) -> tuple[float, float, float]:
    """(x0, x1, y0) of the main content pane in pixel coords."""
    w = _image_width(lines)
    # y0: below the Histories tab strip. Floor ~430px keeps banner / tooltip
    # noise (seen at y≈419) out of the verified table.
    y_max = max((l["y"] + l["h"] for l in lines), default=1313.0)
    return (
        LAYOUT.content_x0 * w,
        LAYOUT.content_x1 * w,
        max(430.0, LAYOUT.content_y0 * y_max - 20),
    )


def detect_columns(
    lines: list[dict], kind: str
) -> dict[str, tuple[float, float]]:
    """Derive column x-bands from the live header row; fall back to COLUMNS.

    Looks for header aliases in the content pane near the verified table, then
    splits the x-axis at midpoints between consecutive header centers.
    """
    fallback = {k: tuple(v) for k, v in COLUMNS[kind].items()}
    aliases = COLUMN_HEADER_ALIASES.get(kind) or {}
    if not aliases:
        return fallback  # type: ignore[return-value]

    x0, x1, y0 = content_band(lines)
    # Headers sit just under the "Verified Local Record Data" title.
    header_y1 = y0 + 220
    candidates = [
        l
        for l in lines
        if x0 <= l["x"] + l["w"] / 2 <= x1 and y0 <= l["y"] <= header_y1
    ]
    found: dict[str, float] = {}
    for col, names in aliases.items():
        best = None
        for l in candidates:
            low = l["text"].strip().lower()
            if any(low == n or low.startswith(n) for n in names):
                cx = l["x"] + l["w"] / 2
                if best is None or l["conf"] > best[1]:
                    best = (cx, l["conf"])
        if best is not None:
            found[col] = best[0]

    order = list(aliases.keys())
    centers = [(col, found[col]) for col in order if col in found]
    if len(centers) < 2:
        return fallback  # type: ignore[return-value]
    centers.sort(key=lambda t: t[1])

    bands: dict[str, tuple[float, float]] = {}
    for i, (col, cx) in enumerate(centers):
        left = (centers[i - 1][1] + cx) / 2 if i else x0
        right = (cx + centers[i + 1][1]) / 2 if i + 1 < len(centers) else x1
        bands[col] = (left, right)

    # Keep fallback columns Vision never saw (e.g. truncated "Last Modified").
    for col, band in fallback.items():
        bands.setdefault(col, band)  # type: ignore[arg-type]
    return bands


def name_x_band(
    bands: dict[str, tuple[float, float]], kind: str
) -> tuple[float, float]:
    """Tight x-band for the identity column (substance / name / procedure).

    The logical column can be very wide (problem Name spans half the table);
    identity *glyphs* always start in a ~70–90 px strip at the column's left.
    """
    key = NAME_COLUMN.get(kind, "name")
    if key in bands:
        x0, x1 = bands[key]
        return (x0, min(x0 + 90, x1))
    return NAME_X


def frame_records(
    lines: list[dict],
    carry: str | None,
    kind: str | None = None,
    bands: dict[str, tuple[float, float]] | None = None,
) -> tuple[list[dict], int | None, str | None]:
    """Walk one frame's rows with cross-frame group state.

    The Verified header often scrolls off-screen, so `carry` brings the group
    state from the previous (overlapping) frame: 'verified' | 'unverified' |
    'other' (encounter list) | None (before any header was ever seen).

    Returns (records, expected_count, end_state).
    """
    x0, x1, y0 = content_band(lines)
    detail = detail_x_cutoff(lines)
    content = [l for l in lines if x0 <= l["x"] < detail and l["y"] > y0]
    rows = group_rows(content)
    name_band = name_x_band(bands or {}, kind or "") if bands else NAME_X
    state = carry
    # If the Verified header is inside this frame, rows above it belong to
    # earlier groups — never let carried state mark them verified.
    full_text = " ".join(l["text"] for l in content).lower()
    if VERIFIED_ANCHOR in full_text:
        state = None
    expected: int | None = None
    records: list[dict] = []
    orphans: list[dict] = []
    for row in rows:
        joined = " ".join(c["text"] for c in row)
        low = joined.lower()
        # Pager row ends the widget; anything below is the next section.
        if PAGE_RE.search(low):
            break
        if VERIFIED_ANCHOR in low:
            state = "verified"
            m = re.search(r"\((\d+)\)", joined)
            if m:
                expected = int(m.group(1))
            continue
        if "unverified data" in low:
            state = "unverified"
            continue
        if all(m in low for m in OTHER_LIST_MARKERS):
            state = "other"
            continue
        if "population health record" in low:
            # Separate widget below the allergies table with its own pager.
            state = "other"
            continue
        if any(e in low for e in SECTION_ENDS):
            # Above the table these are banner noise ("Return to Review");
            # once inside the verified table they mark its end — and nothing
            # after it (including pager pages) can be verified.
            if state == "verified":
                state = "other"
                break
            continue
        if state != "verified":
            continue
        if TIME_RE.search(joined):
            continue  # encounter-list row that leaked past a missed header
        if UNVERIFIED_ROW_RE.search(low):
            continue  # unverified row that leaked past a missed group header
        head = next(
            (c for c in row if name_band[0] <= c["x"] <= name_band[1]), None
        )
        if head is not None:
            name = clean_name(head["text"])
            if (
                name.lower() in NON_RECORD_NAMES
                or not re.search(r"[A-Za-z]{3}", name)
            ):
                continue
            records.append(
                {
                    "name": name,
                    "cy": head["y"] + head["h"] / 2,
                    "cells": [
                        c
                        for c in row
                        if c is not head
                        and c["text"].strip() not in ("-", "--", "")
                    ],
                }
            )
        elif records:
            orphans.extend(
                c for c in row if c["text"].strip() not in ("-", "--", "")
            )
    for o in orphans:
        ocy = o["y"] + o["h"] / 2
        nearest = min(records, key=lambda r: abs(r["cy"] - ocy))
        nearest["cells"].append(o)
    for rec in records:
        rec["cells"].sort(key=lambda c: (c["y"], c["x"]))
    return records, expected, state


def _bucket_cells(
    cells: list[dict],
    kind: str,
    bands: dict[str, tuple[float, float]] | None = None,
) -> dict[str, list[str]]:
    """Assign Vision cells to named table columns by x-band."""
    bands = bands or COLUMNS[kind]  # type: ignore[assignment]
    buckets: dict[str, list[str]] = {k: [] for k in bands}
    for c in cells:
        text = c["text"].strip()
        if not text or text in ("-", "--"):
            continue
        for col, (x0, x1) in bands.items():
            if x0 <= c["x"] < x1:
                # Split merged "reaction … Local Record …" / "Local Record … DATE"
                if col in ("reactions", "substance", "name", "procedure"):
                    i = text.lower().find("local record")
                    if i > 0:
                        left = text[:i].strip(" ,")
                        if left:
                            buckets[col].append(left)
                        text = text[i:]
                        if "originating_source" in bands:
                            buckets["originating_source"].append(text)
                        break
                if col == "originating_source":
                    m = MONTH_DATE.search(text)
                    if m and "last_modified_date" in bands:
                        buckets["last_modified_date"].append(m.group(0))
                        text = MONTH_DATE.sub("", text).strip(" ,")
                    i = text.lower().find("local record")
                    if i >= 0:
                        text = text[i:]
                buckets[col].append(text)
                break
    return buckets


def _norm_date(text: str | None) -> str | None:
    if not text:
        return None
    m = MONTH_DATE.search(text)
    if not m:
        t = text.strip()
        return t if t.isdigit() and len(t) == 4 else None
    mon, day, year = re.split(r"[\s,]+", m.group(0).strip())
    return f"{mon.upper()} {int(day):02d}, {year}"


def _join(parts: list[str]) -> str | None:
    if not parts:
        return None
    # de-dupe consecutive identical fragments from multi-frame merge
    out: list[str] = []
    for p in parts:
        p = p.strip(" ,")
        if not p:
            continue
        if out and p.lower() == out[-1].lower():
            continue
        out.append(p)
    return " ".join(out) if out else None


def build_row(
    rec: dict,
    kind: str,
    source: str,
    bands: dict[str, tuple[float, float]] | None = None,
) -> dict:
    """One table row with the same columns the UI shows."""
    buckets = _bucket_cells(rec["cells"], kind, bands=bands)
    # name column was peeled off as rec["name"] already
    name_key = NAME_COLUMN.get(kind) or {
        "allergies": "substance",
        "problem_history": "name",
        "procedure_history": "procedure",
    }[kind]
    buckets[name_key] = [rec["name"]] + buckets.get(name_key, [])

    if kind == "allergies":
        sev = next(
            (
                t
                for t in buckets["severity"]
                if t.lower() in ("severe", "moderate", "mild", "unknown", "low")
            ),
            None,
        )
        return {
            "substance": rec["name"],
            "severity": sev,
            "reactions": _join(
                [t for t in buckets["reactions"] if t.lower() not in ("severe", "moderate", "mild", "unknown", "low")]
            ),
            "originating_source": find_source(buckets["originating_source"])
            or _join(buckets["originating_source"]),
            "last_modified_date": _norm_date(
                find_month_date(buckets["last_modified_date"] + buckets["originating_source"])
            ),
            "status": "verified",
            "source_file": source,
        }
    if kind == "problem_history":
        return {
            "name": rec["name"],
            "classification": _join(buckets["classification"]),
            "originating_source": find_source(buckets["originating_source"])
            or _join(buckets["originating_source"]),
            "last_modified_date": _norm_date(
                find_month_date(buckets["last_modified_date"] + buckets["originating_source"])
            ),
            "status": "verified",
            "source_file": source,
        }
    # procedure_history
    year = next(
        (t for t in buckets["date"] if t.strip().isdigit() and len(t.strip()) == 4),
        None,
    )
    return {
        "procedure": rec["name"],
        "date": year,
        "originating_source": find_source(buckets["originating_source"])
        or _join(buckets["originating_source"]),
        "last_modified_date": _norm_date(
            find_month_date(buckets["last_modified_date"] + buckets["originating_source"])
        ),
        "status": "verified",
        "source_file": source,
    }


def frame_sort_key(p: Path) -> tuple[int, int]:
    """Order scroll frames before pager pages, pages numerically (p2 < p10)."""
    m = re.search(r"_p(\d+)_(\d+)", p.name)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"_(\d+)$", p.stem)
    return (1, int(m.group(1)) if m else 0)


def _name_key(name: str) -> str:
    """Dedupe key: 'Milk Products' == 'MilkProducts', 'Tussin (d00797)' == 'Tussin'."""
    n = re.sub(r"\([^)]*\)", "", name.lower())
    return re.sub(r"[^a-z0-9]+", "", n)


# Rows clipped by the viewport edge OCR as noise ("Umbilical hernia" →
# "Imhiliaal hauni-"), and always in a single frame, while the real row is read
# cleanly in several. Similarity alone cannot decide this (distinct rows such as
# "Cholecystectomy"/"TOTAL HYSTERECTOMY" score 0.63), so a drop additionally
# requires the suspect to appear exactly once against a partner seen repeatedly.
MISREAD_SIMILARITY = 0.60


def drop_misreads(
    seen: dict[str, tuple[tuple[int, int], dict]], frames: dict[str, int]
) -> tuple[dict, list[str]]:
    """Remove single-frame rows that are near-identical to a repeated row."""
    dropped: list[str] = []
    for key in list(seen):
        if frames.get(key, 0) != 1:
            continue
        for other, n in frames.items():
            if other == key or n < 2 or other not in seen:
                continue
            # A longer real name often extends a shorter one
            # ("Gallbladder" / "Gallbladder absent") — never treat as a misread.
            if key.startswith(other) or other.startswith(key):
                continue
            if (
                difflib.SequenceMatcher(None, key, other).ratio()
                >= MISREAD_SIMILARITY
            ):
                dropped.append(key)
                del seen[key]
                break
    return seen, dropped


def _looks_garble_key(key: str, display: str | None = None) -> bool:
    """Heuristic for OCR soup (mixed scripts / no vowels / runaway repeats)."""
    if not key:
        return True
    raw = display or key
    raw_letters = [c for c in raw if c.isalpha()]
    if raw_letters:
        ascii_raw = [c for c in raw_letters if c.isascii()]
        if len(ascii_raw) / len(raw_letters) < 0.85:
            return True
    letters = [c for c in key if c.isalpha()]
    if not letters:
        return True
    ascii_letters = [c for c in letters if c.isascii()]
    if len(ascii_letters) / len(letters) < 0.85:
        return True
    vowels = sum(c.lower() in "aeiou" for c in ascii_letters)
    if len(ascii_letters) >= 8 and vowels / len(ascii_letters) < 0.12:
        return True
    # "aaaaataatiaaliaaaaliataa" — one letter dominates a long key.
    if len(key) >= 16:
        top = max(key.count(c) for c in set(key) if c.isalpha())
        if top / len(key) >= 0.45:
            return True
    return False


def trim_surplus(
    seen: dict[str, tuple[tuple[int, int], dict]],
    frames: dict[str, int],
    expected: int | None,
) -> tuple[dict, list[str]]:
    """Drop OCR garble rows; if still over UI count, drop weakest singles.

    Prefers dropping singles most similar to an existing multi-frame row
    (OCR garbage like 'Ctrintrira of vac dofarano' vs 'Stricture of vas deferens').
    """
    dropped: list[str] = []
    for key in list(seen):
        row = seen[key][1]
        display = (
            row.get("substance") or row.get("name") or row.get("procedure") or ""
        )
        if frames.get(key, 0) == 1 and _looks_garble_key(key, display):
            dropped.append(key)
            del seen[key]

    if expected is None or len(seen) <= expected:
        return seen, dropped

    def best_sim(key: str) -> float:
        best = 0.0
        for other, n in frames.items():
            if other == key or n < 1 or other not in seen:
                continue
            if key.startswith(other) or other.startswith(key):
                continue
            best = max(
                best, difflib.SequenceMatcher(None, key, other).ratio()
            )
        return best

    while len(seen) > expected:
        singles = [k for k in seen if frames.get(k, 0) == 1]
        if not singles:
            break
        victim = max(singles, key=best_sim)
        # Only drop if it looks like a garble of something we already have,
        # or if every remaining surplus row is a singleton.
        if best_sim(victim) < 0.45 and len(singles) < len(seen) - expected + 1:
            # Prefer dropping the singleton with the lowest frame score / shortest name.
            victim = min(singles, key=lambda k: (len(k), k))
        dropped.append(victim)
        del seen[victim]
    return seen, dropped


def parse_section(
    paths: list[Path],
    kind: str,
    ocr_by_path: dict[Path, list[dict]] | None = None,
) -> dict:
    """Merge verified table rows across scroll frames and pager pages."""
    from concurrent.futures import ThreadPoolExecutor

    seen: dict[str, tuple[tuple[int, int], dict]] = {}
    frames: dict[str, int] = {}
    expected_counts: list[int] = []
    carry: str | None = None
    page = 1
    ordered = sorted(paths, key=frame_sort_key)

    # Parallel Accurate OCR unless the caller already ran a shared pool
    # (extract_details) — avoids nested ThreadPoolExecutor + pyobjc races.
    if ocr_by_path is None:
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(ordered)))) as pool:
            ocr_by_path = dict(zip(ordered, pool.map(ocr_lines, ordered)))
    else:
        missing = [p for p in ordered if p not in ocr_by_path]
        if missing:
            with ThreadPoolExecutor(max_workers=min(4, max(1, len(missing)))) as pool:
                ocr_by_path = {
                    **ocr_by_path,
                    **dict(zip(missing, pool.map(ocr_lines, missing))),
                }

    bands: dict[str, tuple[float, float]] = {
        k: tuple(v) for k, v in COLUMNS[kind].items()
    }
    bands_source = "fallback"
    # Prefer a page-1 frame that still shows the column header for band detect.
    for p in ordered:
        if "_p" in p.name:
            continue
        detected = detect_columns(ocr_by_path[p], kind)
        default = {k: tuple(v) for k, v in COLUMNS[kind].items()}
        if detected != default:
            bands = detected
            bands_source = f"header:{p.name}"
            break
        bands = detected
        bands_source = f"default:{p.name}"
        break

    for p in ordered:
        m = re.search(r"_p(\d+)_", p.name)
        frame_page = int(m.group(1)) if m else 1
        if frame_page != page:
            # A pager page re-renders the widget from its top, so the state
            # carried from the previous page's tail no longer applies.
            page, carry = frame_page, "verified"
        vlines = ocr_by_path[p]
        records, expected, carry = frame_records(
            vlines, carry, kind=kind, bands=bands
        )
        if expected:
            expected_counts.append(expected)
        for rec in records:
            row = build_row(rec, kind, p.name, bands=bands)
            key = _name_key(
                row.get("substance") or row.get("name") or row.get("procedure") or ""
            )
            if not key:
                continue
            frames[key] = frames.get(key, 0) + 1
            score = (len(records), sum(1 for v in row.values() if v))
            if key not in seen or score > seen[key][0]:
                seen[key] = (score, row)

    seen, dropped = drop_misreads(seen, frames)
    expected = max(expected_counts) if expected_counts else None
    seen, trimmed = trim_surplus(seen, frames, expected)
    dropped.extend(trimmed)

    rows = []
    for key, (_, row) in seen.items():
        row["frames_seen"] = frames.get(key, 0)
        # Surfaced for review rather than deleted: a row seen once, in a section
        # holding more rows than the UI claims, may be an OCR artifact.
        if row["frames_seen"] == 1 and expected is not None and len(seen) > expected:
            row["review"] = "seen_once"
        rows.append(row)
    rows.sort(
        key=lambda d: (
            d.get("substance") or d.get("name") or d.get("procedure") or ""
        ).lower()
    )

    meta = SECTION_META[kind]
    suspect = [r for r in rows if r.get("review") == "seen_once"]
    return {
        **meta,
        "verified_count_ui": expected,
        "row_count": len(rows),
        # >= because OCR can still yield an extra artifact row.
        "complete": expected is None or len(rows) >= expected,
        "misreads_dropped": dropped,
        "rows_needing_review": len(suspect),
        "column_bands": {k: [round(a, 1), round(b, 1)] for k, (a, b) in bands.items()},
        "column_bands_source": bands_source,
        "rows": rows,
    }


def patient_from_banner(path: Path) -> dict:
    """Pull patient demographics from the blue banner OCR."""
    lines = ocr_lines(path)
    # Wider than the blue strip alone — title-bar "LAST, FIRST - MRN" often
    # sits just above the vitals band on some Citrix scalings.
    text = " ".join(l["text"] for l in lines if l["y"] < 280)
    out: dict = {}
    # Name is "LAST, FIRST" appearing just before "MRN:". Handles ZZTEST/TEST variants.
    m = re.search(r"([A-Z][A-Za-z]+,\s*[A-Z][A-Za-z]+)\s+MRN", text)
    if not m:
        # Some layouts only carry the name in the title bar:
        # "ZZTEST, ZZTEST - 027-307-03 Opened by ...".
        m = re.search(
            r"([A-Z][A-Za-z]+,\s*[A-Z][A-Za-z]+)\s*-\s*\d{3}-\d{3}-\d{2}", text
        )
    if m:
        out["patient"] = re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(r"MRN[:\s]*([0-9]{3}-[0-9]{3}-[0-9]{2})", text, re.I)
    if m:
        out["mrn"] = m.group(1)
    m = re.search(r"DOB[:\s]*([0-9]{2}/[0-9]{2}/[0-9]{4})", text, re.I)
    if m:
        out["dob"] = m.group(1)
    m = re.search(r"Age[:\s]*([0-9]+)\s*years?", text, re.I)
    if m:
        out["age_years"] = int(m.group(1))
    m = re.search(r"Sex[:\s]*(Female|Male)", text, re.I)
    if m:
        out["sex"] = m.group(1).title()
    return out


def extract_details(run_dir: Path, mrn: str | None = None) -> dict:
    """OCR verified screenshots under run_dir → details dict (one MRN)."""
    from concurrent.futures import ThreadPoolExecutor

    verified = run_dir / "verified"
    if not verified.is_dir():
        raise FileNotFoundError(f"no verified dir under {run_dir}")

    result: dict = {
        "mrn": mrn,
        "patient": None,
        "source_run": str(run_dir),
        "engine": "apple-vision",
        "subsection": "Verified Local Record Data",
        "tables": {},
    }
    banner_path: Path | None = None
    jobs: list[tuple[str, list[Path]]] = []

    for kind in ("allergies", "problem_history", "procedure_history"):
        d = verified / kind
        if not d.is_dir():
            continue
        paths = sorted(
            p
            for p in d.glob(f"{kind}_*.png")
            if "_small" not in p.name and "ocr_crop" not in p.name
        )
        if paths and banner_path is None:
            banner_path = paths[0]
        if paths:
            jobs.append((kind, paths))

    # One shared OCR pool across all sections (no nested executors).
    all_paths = [p for _, paths in jobs for p in paths]
    ocr_all: dict[Path, list[dict]] = {}
    if all_paths:
        with ThreadPoolExecutor(max_workers=min(6, len(all_paths))) as pool:
            ocr_all = dict(zip(all_paths, pool.map(ocr_lines, all_paths)))

    for kind, paths in jobs:
        result["tables"][kind] = parse_section(paths, kind, ocr_by_path=ocr_all)

    if banner_path:
        dem = patient_from_banner(banner_path)
        result["patient"] = dem.get("patient")
        result["demographics"] = {
            k: dem[k] for k in ("dob", "age_years", "sex") if k in dem
        }
        if dem.get("mrn"):
            result["mrn"] = dem["mrn"]
        elif mrn:
            result["mrn"] = mrn
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--mrn", default=None)
    ap.add_argument("--raw", type=Path, default=None, help="Also dump raw Vision lines")
    args = ap.parse_args()

    result = extract_details(args.run, mrn=args.mrn)
    for kind, sec in result["tables"].items():
        flag = "OK" if sec["complete"] else "INCOMPLETE"
        print(
            f"{kind}: {sec['row_count']}/{sec['verified_count_ui']} [{flag}]"
        )
        for row in sec["rows"]:
            print(f"  - {row}")

    out = args.out or (args.run / f"{result.get('mrn') or 'details'}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(f"\nwrote {out}")

    if args.raw:
        raw = {}
        verified = args.run / "verified"
        for kind in ("allergies", "problem_history", "procedure_history"):
            d = verified / kind
            if not d.is_dir():
                continue
            paths = sorted(
                p
                for p in d.glob(f"{kind}_*.png")
                if "_small" not in p.name and "ocr_crop" not in p.name
            )
            raw[kind] = {p.name: ocr_lines(p) for p in paths}
        args.raw.parent.mkdir(parents=True, exist_ok=True)
        args.raw.write_text(json.dumps(raw, indent=2))
        print(f"wrote {args.raw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
