"""Deterministic in-process tests for the Citrix ICA/HDX STAND-IN campaign.

These run in CI with no Docker, no network, and no Playwright: the whole
Section 10 condition matrix is exercised against the unmodified
``CitrixWorkspaceBackend`` through the synthetic fixture. They assert the
acceptance gate, the honesty invariants (this is NOT real ICA/HDX), and the
reviewable volatile-mask contract.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "benchmark" / "citrix_ica_hdx" / "run_ica_hdx_qualification.py"
FIXTURE = REPO / "benchmark" / "citrix_ica_hdx" / "fixture.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


harness = _load("ica_hdx_qualification", HARNESS)
fixture = _load("ica_hdx_fixture_test", FIXTURE)


# The full Section 10 condition matrix, each as a scenario with its expectation.
EXPECTED_SCENARIOS = {
    "session_launch_ready": "pass",
    "application_not_ready": "halt",
    "reconnect_roaming_identity_change": "halt",
    "session_lock": "halt",
    "session_unlock_recovery": "pass",
    "window_minimize": "halt",
    "window_occlusion": "halt",
    "window_move_after_acquire": "halt",
    "window_resize_after_acquire": "halt",
    "dpi_scale_consistent": "pass",
    "dpi_anisotropic_uncalibrated": "halt",
    "single_monitor_geometry": "pass",
    "multimonitor_secondary_offset": "pass",
    "multimonitor_ambiguous_window": "halt",
    "codec_artifacts_mild_legible": "pass",
    "codec_artifacts_severe_illegible": "halt",
    "stale_frame_latency": "halt",
    "delayed_frame_settles": "pass",
    "frame_never_settles": "halt",
    "keyboard_named_key_ok": "pass",
    "ime_unmapped_key": "halt",
    "clipboard_restricted_paste": "halt",
    "focus_theft_after_acquire": "halt",
    "unexpected_dialog_overlay": "halt",
    "unverifiable_application_identity": "halt",
    "uncertain_submission_no_blind_retry": "halt",
    "duplicate_write_prevention_one_shot": "halt",
    "persisted_state_readback": "pass",
    "optimistic_banner_effect_refused": "effect_refused",
}


@pytest.fixture(scope="module")
def evidence() -> dict:
    return harness.run_campaign()


def test_campaign_accepted_with_zero_unsafe_outcomes(evidence: dict) -> None:
    assert evidence["deterministic_standin_accepted"] is True
    assert evidence["scenarios_passed"] == evidence["trial_count"]
    assert evidence["silent_incorrect_successes"] == 0
    assert evidence["silent_writes"] == 0
    assert evidence["healthy_over_halts"] == 0
    assert evidence["model_calls"] == 0


def test_every_section10_condition_has_a_scenario(evidence: dict) -> None:
    by_id = {t["id"]: t for t in evidence["trials"]}
    assert set(by_id) == set(EXPECTED_SCENARIOS)
    for sid, expectation in EXPECTED_SCENARIOS.items():
        assert by_id[sid]["expectation"] == expectation, sid


def test_pass_scenarios_complete_and_halt_scenarios_refuse(evidence: dict) -> None:
    for t in evidence["trials"]:
        if t["expectation"] == "pass":
            assert t["outcome"] == "completed", t["id"]
            assert t["passed"], t["id"]
        elif t["expectation"] == "halt":
            assert t["outcome"] == "halted", t["id"]
            assert t["halt_reason"], t["id"]
            assert t["passed"], t["id"]


def test_write_pass_scenarios_have_verified_out_of_band_effect(evidence: dict) -> None:
    for t in evidence["trials"]:
        if t["expectation"] == "pass" and t["kind"] == "write":
            assert t["effect_verified"] is True, t["id"]
            assert t["effect_observed"] == list(harness.EXPECTED), t["id"]


def test_no_silent_write_on_any_refusal(evidence: dict) -> None:
    for t in evidence["trials"]:
        if t["expectation"] in ("halt", "effect_refused"):
            # The single legitimate write in the duplicate scenario is exempt.
            if t["id"] == "duplicate_write_prevention_one_shot":
                assert t["oracle_write_count"] == 1
            else:
                assert t["oracle_write_count"] == 0, t["id"]


def test_optimistic_banner_is_not_confirmed_by_screen(evidence: dict) -> None:
    by_id = {t["id"]: t for t in evidence["trials"]}
    row = by_id["optimistic_banner_effect_refused"]
    assert row["banner_shown"] is True  # the screen claims "Saved"
    assert row["independent_record_present"] is False  # the oracle disagrees
    assert row["passed"] is True  # verification withholds success


def test_honesty_invariants_not_real_ica_hdx(evidence: dict) -> None:
    assert evidence["is_real_ica_hdx"] is False
    assert evidence["ica_hdx_accepted"] is False
    assert evidence["ica_hdx_status"].startswith("pending")
    assert "NOT real" in evidence["label"]


def test_status_dimensions_are_separate_and_real_protocol_pending(
    evidence: dict,
) -> None:
    dims = evidence["status_dimensions"]
    # The six published dimensions are never collapsed into one "Available".
    for key in (
        "backend_shipped",
        "installed_driver_available",
        "real_protocol_environment_evidence",
        "managed_execution_available",
        "customer_controlled_execution_available",
        "exact_application_qualification_available",
    ):
        assert key in dims
    assert dims["real_protocol_environment_evidence"]["status"] == "pending"
    assert dims["backend_shipped"]["status"] == "shipped"
    assert dims["deterministic_standin_qualification"]["status"] == "qualified"


def test_zero_model_calls_on_every_trial(evidence: dict) -> None:
    assert all(t["model_calls"] == 0 for t in evidence["trials"])


def test_default_volatile_mask_spec_is_reviewable_and_safe(evidence: dict) -> None:
    review = evidence["volatile_mask_review"]
    assert review["violations"] == []
    assert review["reviewable_and_safe"] is True


def test_volatile_mask_covering_a_protected_region_is_rejected() -> None:
    spec = fixture.default_mask_spec()
    bad = fixture.VolatileMaskSpec(
        # A mask that (wrongly) covers the Save button target region.
        volatile={**spec.volatile, "bad_mask": spec.protected["target_save_button"]},
        protected=spec.protected,
    )
    violations = fixture.check_masks_reviewable(bad)
    assert violations
    assert any("target_save_button" in v for v in violations)


def test_mask_never_covers_target_identity_or_effect_regions() -> None:
    spec = fixture.default_mask_spec()
    for name in (
        "target_roster_row",
        "target_note_field",
        "target_save_button",
        "identity_readiness",
        "identity_application",
        "workflow_state",
        "effect_saved_banner",
    ):
        assert name in spec.protected
    assert fixture.check_masks_reviewable(spec) == []
