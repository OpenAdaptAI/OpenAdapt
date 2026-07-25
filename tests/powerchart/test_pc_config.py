"""Unit tests for PowerChart site/layout config (no Vision / no Citrix)."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "app-run" / "powerchart" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pc_config import (  # noqa: E402
    LAYOUT,
    looks_like_mrn,
    is_chart_title,
    is_organizer_title,
)


def test_mrn_pattern():
    assert looks_like_mrn("027-307-03")
    assert looks_like_mrn("080-975-37")
    assert not looks_like_mrn("02730703")
    assert not looks_like_mrn("ZZTEST")
    assert not looks_like_mrn("")


def test_chart_title_by_mrn():
    assert is_chart_title(
        "ZZTEST, ZZTEST - 027-307-03 Opened by Khauta, Rajeeb",
        mrn="027-307-03",
    )
    assert is_chart_title(
        "SMITH, JANE - 080-975-37 Opened by Someone",
        mrn="080-975-37",
    )
    # No patient-name literal required.
    assert is_chart_title(
        "DOE, JOHN - 101-015-77 Opened by Doc",
        mrn=None,
    )


def test_chart_title_excludes_dialogs():
    assert not is_chart_title("Patient Search", mrn="027-307-03")
    assert not is_chart_title("Ad Hoc Charting", mrn="027-307-03")
    assert not is_chart_title("Custom View", mrn="027-307-03")


def test_organizer_title():
    assert is_organizer_title("PowerChart Organizer for Khauta, Rajeeb")
    assert is_organizer_title("Organizer")
    assert not is_organizer_title("Safari")


def test_layout_fractions_in_unit_interval():
    for name in (
        "nav_x1",
        "content_x0",
        "content_x1",
        "detail_x0",
        "close_x",
        "close_y",
        "scroll_x",
        "park_x",
    ):
        v = getattr(LAYOUT, name)
        assert 0.0 <= v <= 1.0, name
    assert LAYOUT.nav_x1 < LAYOUT.content_x0 + 0.05
    assert LAYOUT.content_x1 <= LAYOUT.detail_x0 + 0.01
