#!/usr/bin/env python3
"""Site / layout config for PowerChart capture + OCR.

Magic numbers that used to live in coord_replay / vision_ocr live here as
*fractions of the window* or *label text*, so the runner generalizes across
window sizes, Retina scale, and Clinic Workflow nav order.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Site identity
# ---------------------------------------------------------------------------

# Adventist / Cerner local MRN shape. Override per site if needed.
MRN_RE = re.compile(r"^\d{3}-\d{3}-\d{2}$")

# Substrings that identify the Organizer window (any match).
ORGANIZER_TITLE_MARKERS = ("Organizer", "PowerChart")

# Chart window: title contains the MRN, or looks like "LAST, FIRST - MRN Opened by …"
CHART_TITLE_EXCLUDE = ("Ad Hoc", "Search", "Custom", "Patient Search")
CHART_TITLE_OPENED_RE = re.compile(
    r".+\s-\s*\d{3}-\d{3}-\d{2}\s+Opened by", re.IGNORECASE
)

# Preflight: Organizer is ready for MRN entry when any of these appear in OCR.
PREFLIGHT_READY_MARKERS = (
    "mrn",
    "patient search",
    "patient list",
    "search by",
)
# Organizer is NOT ready when these dominate and ready markers are absent.
PREFLIGHT_WRONG_VIEW_MARKERS = (
    "message center",
    "inbox summary",
    "modify receipts",
)


# ---------------------------------------------------------------------------
# Layout fractions (of screenshot / window size)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LayoutFractions:
    """Normalized regions used when Vision has no better text anchor."""

    # Left Clinic Workflow nav (Allergies / Histories / …).
    # Floor nav_x0 above 0 so edge OCR fragments (x≈0, w≈60) are ignored.
    nav_x0: float = 0.04
    nav_x1: float = 0.18
    nav_y0: float = 0.25
    nav_y1: float = 0.95

    # Histories sub-tab strip
    tab_x0: float = 0.17
    tab_x1: float = 0.70
    tab_y0: float = 0.30
    tab_y1: float = 0.42

    # Main content pane (verified tables live here)
    content_x0: float = 0.17
    content_x1: float = 0.87
    content_y0: float = 0.32
    content_y1: float = 0.95

    # Right detail flyout begins around here
    detail_x0: float = 0.87

    # Chart-tab close "×" lives in the top-left tab strip
    close_x: float = 0.040
    close_y: float = 0.085

    # Content scroll / park cursor
    scroll_x: float = 0.35
    scroll_y: float = 0.60
    park_x: float = 0.75
    park_y: float = 0.30


LAYOUT = LayoutFractions()


# ---------------------------------------------------------------------------
# Text anchors for navigation
# ---------------------------------------------------------------------------

# Left-nav labels → panel keys used by capture_verified_sections
PANEL_LABELS = {
    "allergies": ("allergies",),
    "histories": ("histories",),
    "home_medications": ("home medications", "home medication"),
    "diagnoses": ("diagnoses and problems", "diagnoses"),
}

# Histories sub-tabs
TAB_LABELS = {
    "problem_history": ("problem history",),
    "procedure_history": ("procedure history",),
}

# Fallback pixel candidates (window-relative, ~3440×1313) used only when Vision
# cannot find the label text. Kept as last-resort for the current site layout.
PANEL_FALLBACK_XY = {
    "allergies": (400, 430),
    "histories": (400, 575),
    "home_medications": (400, 475),
    "diagnoses": (400, 520),
}
TAB_FALLBACK_XY = {
    "problem_history": [(780, 468), (800, 465), (760, 475)],
    "procedure_history": [(1045, 468), (1070, 465), (1010, 475)],
}


# ---------------------------------------------------------------------------
# Table column headers (for deriving x-bands from a live frame)
# ---------------------------------------------------------------------------

# Aliases Vision may read for each logical column, ordered left→right.
COLUMN_HEADER_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "allergies": {
        "substance": ("substance",),
        "severity": ("severity",),
        "reactions": ("reactions", "reaction"),
        "originating_source": ("originating source", "source"),
        "last_modified_date": ("last modified date", "last modified", "modified"),
    },
    "problem_history": {
        "name": ("name",),
        "classification": ("classification",),
        "originating_source": ("originating source", "source"),
        "last_modified_date": ("last modified date", "last modified", "modified"),
    },
    "procedure_history": {
        "procedure": ("procedure",),
        "date": ("date",),
        "originating_source": ("originating source", "source"),
        "last_modified_date": ("last modified date", "last modified", "modified"),
    },
}

# Name / first column key per section (row identity).
NAME_COLUMN = {
    "allergies": "substance",
    "problem_history": "name",
    "procedure_history": "procedure",
}


def looks_like_mrn(text: str) -> bool:
    return bool(MRN_RE.fullmatch((text or "").strip()))


def is_chart_title(title: str, mrn: str | None = None) -> bool:
    t = title or ""
    if any(ex in t for ex in CHART_TITLE_EXCLUDE):
        return False
    if mrn and mrn in t:
        return True
    return bool(CHART_TITLE_OPENED_RE.search(t))


def is_organizer_title(title: str) -> bool:
    t = title or ""
    return any(m in t for m in ORGANIZER_TITLE_MARKERS)


@dataclass
class SiteConfig:
    """Optional per-site overrides loaded from JSON later if needed."""

    mrn_re: re.Pattern[str] = field(default=MRN_RE)
    layout: LayoutFractions = field(default_factory=LayoutFractions)


SITE = SiteConfig()
