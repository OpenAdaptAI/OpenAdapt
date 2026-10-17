"""Hash-bound guard over the live-AX macOS structured-identity evidence.

``scripts/qualify_macos_ax_identity.py`` drove the macOS backend's AX
IdentityBackend / StructuralActionBackend against a REAL TextEdit document on an
Apple-Silicon host and wrote a byte-preserved evidence JSON plus a hash-bound
adjudication. This test pins that artifact so the recorded live result cannot be
silently edited, and asserts the adjudication references the evidence's exact
bytes -- the same integrity contract the TextEdit actuation qualification uses
(``test_macos_qualification``).

It does NOT re-run the live capture (CI is headless Linux with no AX); it guards
the committed evidence of the local run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

EVIDENCE_DIR = Path(__file__).parents[1] / "benchmark" / "macos_native"
EVIDENCE = EVIDENCE_DIR / "ax_identity_20260720.json"
ADJUDICATION = EVIDENCE_DIR / "ax_identity_20260720.adjudication.json"
EVIDENCE_SHA256 = "23848e0be694f97c0b749bf34f3aa6cb26fffe7a0ad5b29fc6ebe98e2dcb4f74"


def test_evidence_is_byte_preserved() -> None:
    assert hashlib.sha256(EVIDENCE.read_bytes()).hexdigest() == EVIDENCE_SHA256


def test_live_ax_identity_run_passed_with_exact_structured_text() -> None:
    report = json.loads(EVIDENCE.read_bytes())
    assert report["task"] == "macos_ax_structured_identity"
    assert report["status"] == "passed"
    # A healthy structured-identity resolution makes NO model calls.
    assert report["model_calls"] == 0

    identity = report["identity_under_test"]
    # The live AX text matched the file's identity band EXACTLY -- the glyph
    # fidelity (a literal digit 0 AND a literal letter O) that OCR cannot
    # guarantee, which is the whole point of the structured-identity tier.
    assert identity["exact_match"] is True
    assert identity["observed"] == identity["expected"]
    assert identity["glyph_fidelity"]["contains_digit_zero"] is True
    assert identity["glyph_fidelity"]["contains_letter_O"] is True


def test_all_three_ax_capabilities_were_proven_live() -> None:
    report = json.loads(EVIDENCE.read_bytes())
    assert set(report["capabilities_proven"]) == {
        "IdentityBackend.structured_text_at",
        "StructuralActionBackend.structural_locator_at",
        "StructuralActionBackend.locate_structural",
    }
    # A window-scoped, unique, non-truncated enumeration on live AX.
    assert report["enumeration"] == {"candidate_count": 1, "truncated": False}
    handle = report["structural_handle"]
    assert handle["candidate_count"] == 1
    assert len(handle["target_fingerprint"]) == 64
    locator = report["structural_locator"]
    assert locator["automation_id"] == "First Text View"
    assert locator["role"] == "textbox"


def test_live_negative_controls_are_safe_misses_not_wrong_targets() -> None:
    report = json.loads(EVIDENCE.read_bytes())
    controls = report["negative_controls"]
    assert controls["nonexistent_locator_is_miss"] is True
    assert controls["out_of_window_point_text_is_none"] is True


def test_cleanup_preserved_every_unrelated_textedit_process() -> None:
    report = json.loads(EVIDENCE.read_bytes())
    cleanup = report["cleanup"]
    assert cleanup["unrelated_textedit_pids_preserved"] is True
    assert cleanup["temporary_root_removed"] is True
    assert cleanup["terminate"]["verified_absent"] is True


def test_adjudication_is_hash_bound_to_the_exact_evidence() -> None:
    report_bytes = EVIDENCE.read_bytes()
    adjudication = json.loads(ADJUDICATION.read_bytes())
    original = adjudication["original_evidence"]
    assert original["sha256"] == EVIDENCE_SHA256
    assert original["sha256"] == hashlib.sha256(report_bytes).hexdigest()
    assert original["bytes"] == len(report_bytes)
    assert original["status"] == "passed"
    assert original["preserved_byte_for_byte"] is True
    # Honest maturity: development-lane engineering evidence, not a release-lane
    # scoped acceptance, and the refusal contract lives in the unit suite.
    assert adjudication["evidence_classification"] == (
        "diagnostic_local_engineering_evidence"
    )
    assert (
        adjudication["refusal_contract_is_unit_covered_in"]
        == "tests/test_macos_structural.py"
    )
