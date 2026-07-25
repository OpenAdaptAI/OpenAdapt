"""Regression: reparse golden PowerChart screenshots with Vision OCR.

These tests need Apple Vision (macOS) and the golden frames under
app-run/powerchart/golden/. They assert row counts stay in the ranges we
locked in after the dense-chart hardening — catching parser regressions
without a live Citrix session.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
POWERCHART = ROOT / "app-run" / "powerchart"
SCRIPTS = POWERCHART / "scripts"
GOLDEN = POWERCHART / "golden"
EXPECTATIONS = GOLDEN / "expectations.json"

sys.path.insert(0, str(SCRIPTS))

pytest.importorskip("Vision")
import vision_ocr  # noqa: E402


def _load_expectations() -> dict:
    assert EXPECTATIONS.is_file(), f"missing {EXPECTATIONS}"
    return json.loads(EXPECTATIONS.read_text())


def _run_dir(rel: str) -> Path:
    return POWERCHART / rel


@pytest.mark.parametrize("mrn", sorted(_load_expectations().keys()))
def test_golden_extract_counts(mrn: str):
    exp = _load_expectations()[mrn]
    run = _run_dir(exp["run"])
    verified = run / "verified"
    if not verified.is_dir():
        pytest.skip(f"golden frames missing: {verified}")

    result = vision_ocr.extract_details(run, mrn=mrn)

    patient = result.get("patient") or ""
    assert exp["patient_contains"].lower() in patient.lower(), patient
    assert result.get("mrn") == mrn

    for kind, bounds in exp["tables"].items():
        sec = result["tables"].get(kind)
        assert sec is not None, f"missing table {kind}"
        n = sec["row_count"]
        assert bounds["min_rows"] <= n <= bounds["max_rows"], (
            f"{kind}: rows={n} not in [{bounds['min_rows']}, {bounds['max_rows']}] "
            f"(ui={sec.get('verified_count_ui')})"
        )
        if bounds.get("ui") is not None:
            assert sec.get("verified_count_ui") == bounds["ui"], kind


def test_detect_columns_finds_header_or_falls_back():
    """Header-derived bands should at least return the allergy columns."""
    run = GOLDEN / "080-975-37" / "verified" / "allergies"
    paths = sorted(
        p
        for p in run.glob("allergies_*.png")
        if "_small" not in p.name and "ocr_crop" not in p.name
    )
    if not paths:
        pytest.skip("no allergy golden frames")
    bands = vision_ocr.detect_columns(vision_ocr.ocr_lines(paths[0]), "allergies")
    assert "substance" in bands
    assert "severity" in bands
    # Name column must sit left of severity.
    assert bands["substance"][0] < bands["severity"][0]


def test_window_scale_identity_on_1x_screenshot():
    """On the Citrix captures, screenshot pixels == window points (scale=1)."""
    import coord_replay

    class W:
        bounds = (0.0, 30.0, 3440.0, 1313.0)

    from PIL import Image

    im = Image.new("RGB", (3440, 1313))
    assert abs(coord_replay.window_scale(W(), im) - 1.0) < 1e-6
    ax, ay = coord_replay.px_to_screen(W(), im, 1720, 656)
    assert abs(ax - 1720) < 1e-6
    assert abs(ay - (30 + 656)) < 1e-6
