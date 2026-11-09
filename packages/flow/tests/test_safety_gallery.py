"""Tests for the wrong-patient safety gallery generator.

Fast tests (no browser/OCR) cover the case fixture and the SAFE/CORRECT
classification logic. One Playwright + OCR guarded test runs the real
end-to-end generator and asserts EVERY dangerous case is safe and both
controls are correct — a regression guard so a future change to the identity
path can never silently ship a gallery that VERIFIES a wrong patient.
"""

from __future__ import annotations

import json

import pytest

from benchmark.safety_gallery import generate as gen


# ---------------------------------------------------------------------------
# Fixture (fast)
# ---------------------------------------------------------------------------


def test_cases_cover_every_required_class() -> None:
    ids = {c.id for c in gen.GALLERY_CASES}
    required = {
        "O0_alphanumeric",
        "l1_alphanumeric",
        "numeric",
        "separator",
        "sibling",
        "clean_control",
        "different_patient",
    }
    assert required <= ids


def test_danger_cases_are_wrong_patient_traps() -> None:
    # Every glyph-class danger pair is a DIFFERENT identifier one glyph apart;
    # the sibling pair shares name+DOB with a genuinely different MRN.
    by_id = {c.id: c for c in gen.GALLERY_CASES}
    for cid in ("O0_alphanumeric", "l1_alphanumeric", "numeric", "separator"):
        c = by_id[cid]
        assert c.kind == gen.DANGER
        assert c.recorded.mrn != c.live.mrn
        assert c.recorded.name == c.live.name  # same-name homonym
        assert c.recorded.dob == c.live.dob
    sib = by_id["sibling"]
    assert sib.recorded.name == sib.live.name and sib.recorded.mrn != sib.live.mrn


def test_controls_are_configured_as_controls() -> None:
    by_id = {c.id: c for c in gen.GALLERY_CASES}
    assert by_id["clean_control"].kind == gen.CONTROL_VERIFY
    assert by_id["clean_control"].recorded.mrn == by_id["clean_control"].live.mrn
    # the verify control's MRN must bear none of the confusable glyphs
    assert not (set(by_id["clean_control"].recorded.mrn.lower()) & set("0o1l|!i"))
    assert by_id["different_patient"].kind == gen.CONTROL_MISMATCH
    assert (
        by_id["different_patient"].recorded.name != by_id["different_patient"].live.name
    )


# ---------------------------------------------------------------------------
# Classification logic (fast, pure)
# ---------------------------------------------------------------------------


def test_is_safe_danger_only_verify_is_unsafe() -> None:
    for status in ("mismatch", "abstain", "unreadable"):
        assert gen.is_safe(gen.DANGER, status) is True
    assert gen.is_safe(gen.DANGER, "verified") is False


def test_is_safe_controls() -> None:
    assert gen.is_safe(gen.CONTROL_VERIFY, "verified") is True
    assert gen.is_safe(gen.CONTROL_VERIFY, "abstain") is False
    assert gen.is_safe(gen.CONTROL_MISMATCH, "mismatch") is True
    assert gen.is_safe(gen.CONTROL_MISMATCH, "verified") is False


def test_is_safe_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        gen.is_safe("bogus", "verified")


# ---------------------------------------------------------------------------
# End-to-end (real render + real OCR + real identity path)
# ---------------------------------------------------------------------------


def test_generator_end_to_end_every_dangerous_case_is_safe(tmp_path) -> None:
    pytest.importorskip("playwright.sync_api")
    pytest.importorskip("rapidocr_onnxruntime")

    hd = gen.build(tmp_path)

    # Files were written.
    assert (tmp_path / "gallery.html").exists()
    results_path = tmp_path / "results.json"
    assert results_path.exists()

    # The core safety invariant: NO dangerous case may VERIFY a wrong patient.
    assert hd["all_safe"], f"UNSAFE cases (P0): {hd['unsafe_ids']}"
    assert hd["danger_safe"] == hd["danger_total"]
    assert hd["controls_correct"] == hd["controls_total"]

    payload = json.loads(results_path.read_text())
    assert payload["model_calls"] == 0
    by_id = {c["id"]: c for c in payload["cases"]}

    # Every glyph-collapse pair must read byte-identically under OCR and the
    # gate must NOT verify.
    for cid in ("O0_alphanumeric", "l1_alphanumeric", "numeric", "separator"):
        case = by_id[cid]
        assert case["ocr_collapsed"] is True, f"{cid} did not collapse under OCR"
        assert case["ocr_recorded"] == case["ocr_live"]
        assert case["verdict"] != "verified", f"{cid} VERIFIED a wrong patient (P0)"
        assert case["safe"] is True

    # The controls anchor the gate: it is neither trivially abstaining nor
    # trivially verifying.
    assert by_id["clean_control"]["verdict"] == "verified"
    assert by_id["different_patient"]["verdict"] == "mismatch"
