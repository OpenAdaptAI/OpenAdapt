"""Tests for Mitigation #1: Online False-Pause Calibration Collector (zero-PHI)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_flow.calibration_collector import (
    CALIBRATION_RECORD_SCHEMA,
    CalibrationCollector,
    CalibrationVerdict,
    OverrideKind,
    record_operator_override,
)
from openadapt_flow.multi_lora_veto import VetoDecision

_HASH_A = "a" * 64
_HASH_B = "b" * 64


def test_record_operator_override_writes_zero_phi_tenant_jsonl(tmp_path: Path) -> None:
    rec = record_operator_override(
        tenant_id="clinic-north",
        app_id="ehr-portal",
        workflow_id="patient-intake",
        step_id="step-submit-01",
        screenshot_hash=_HASH_A,
        predicted_verdict=VetoDecision.VETO,
        operator_verdict=VetoDecision.ALLOW,
        queue_root=tmp_path,
        bound_tenant_id="clinic-north",
        pre_submit_screenshot_hash=_HASH_B,
        adapter_id="veto-ehr-v1",
        calibrated_fault_prob=0.61,
        structural_metadata={
            "viewport_width": 1280,
            "viewport_height": 800,
            "dom_field_count": 14,
            "dom_claims_success": True,
            "roi_crop_used": True,
            "theme_hint": "light_enterprise",
        },
    )
    assert rec.schema_version == CALIBRATION_RECORD_SCHEMA
    assert rec.override_kind is OverrideKind.FALSE_PAUSE
    assert rec.predicted_verdict is CalibrationVerdict.VETO
    assert rec.operator_verdict is CalibrationVerdict.ALLOW

    queue_file = tmp_path / "clinic-north" / "ehr-portal.jsonl"
    assert queue_file.is_file()
    raw = json.loads(queue_file.read_text(encoding="utf-8").strip())
    assert raw["tenant_id"] == "clinic-north"
    assert raw["screenshot_hash"] == _HASH_A
    assert raw["override_kind"] == "false_pause"
    assert "expected_fields" not in raw
    assert "reviewer_note" not in raw


def test_zero_phi_rejects_disallowed_metadata_keys_and_pii_values(
    tmp_path: Path,
) -> None:
    collector = CalibrationCollector(tmp_path, bound_tenant_id="clinic-north")

    for bad_key in (
        "expected_fields",
        "patient_name",
        "raw_text",
        "reviewer_note",
        "dom_html",
    ):
        with pytest.raises(ValueError, match="Zero-PHI violation"):
            collector.record_operator_override(
                tenant_id="clinic-north",
                app_id="ehr-portal",
                workflow_id="intake",
                step_id="step-1",
                screenshot_hash=_HASH_A,
                predicted_verdict="VETO",
                operator_verdict="ALLOW",
                structural_metadata={bad_key: "secret"},
            )

    for pii_val in ("jane.doe@clinic.org", "123-45-6789", "555-234-5678", "x" * 65):
        with pytest.raises(ValueError, match="Zero-PHI violation"):
            collector.record_operator_override(
                tenant_id="clinic-north",
                app_id="ehr-portal",
                workflow_id="intake",
                step_id="step-1",
                screenshot_hash=_HASH_A,
                predicted_verdict="VETO",
                operator_verdict="ALLOW",
                structural_metadata={"theme_hint": pii_val},
            )

    with pytest.raises(ValueError, match="64 lowercase hex"):
        collector.record_operator_override(
            tenant_id="clinic-north",
            app_id="ehr-portal",
            workflow_id="intake",
            step_id="step-1",
            screenshot_hash="/tmp/patient_john_doe.png",
            predicted_verdict="VETO",
            operator_verdict="ALLOW",
        )


def test_fail_closed_tenant_isolation_on_write_and_read(tmp_path: Path) -> None:
    collector = CalibrationCollector(tmp_path, bound_tenant_id="clinic-north")

    # Missing or path-traversal tenant IDs are rejected.
    for bad_tenant in ("", "   ", "../clinic-south", "clinic-north/sub"):
        with pytest.raises(ValueError, match="tenant_id"):
            collector.record_operator_override(
                tenant_id=bad_tenant,
                app_id="ehr-portal",
                workflow_id="intake",
                step_id="step-1",
                screenshot_hash=_HASH_A,
                predicted_verdict="VETO",
                operator_verdict="ALLOW",
            )

    # Cross-tenant write is refused.
    with pytest.raises(PermissionError, match="Cross-tenant"):
        collector.record_operator_override(
            tenant_id="clinic-south",
            app_id="ehr-portal",
            workflow_id="intake",
            step_id="step-1",
            screenshot_hash=_HASH_A,
            predicted_verdict="VETO",
            operator_verdict="ALLOW",
        )

    # Cross-tenant read is refused.
    with pytest.raises(PermissionError, match="Cross-tenant"):
        collector.read_tenant_queue("clinic-south")

    # Tampered JSONL containing another tenant's record fails closed on read.
    tampered_dir = tmp_path / "clinic-north"
    tampered_dir.mkdir(parents=True, exist_ok=True)
    other_collector = CalibrationCollector(tmp_path)
    other_rec = other_collector.record_operator_override(
        tenant_id="clinic-south",
        app_id="ehr-portal",
        workflow_id="intake",
        step_id="step-1",
        screenshot_hash=_HASH_A,
        predicted_verdict="VETO",
        operator_verdict="ALLOW",
    )
    (tampered_dir / "ehr-portal.jsonl").write_text(
        json.dumps(other_rec.model_dump(mode="json")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(PermissionError, match="Tenant isolation violation"):
        collector.read_tenant_queue("clinic-north", app_id="ehr-portal")


def test_deduplication_and_calibration_readiness_flywheel(tmp_path: Path) -> None:
    collector = CalibrationCollector(tmp_path, bound_tenant_id="clinic-north")

    # Record 10 unique overrides (8 false pauses, 1 true fault, 1 missed fault) + 1 duplicate.
    for idx in range(8):
        h = f"{idx:064x}"
        collector.record_operator_override(
            tenant_id="clinic-north",
            app_id="billing-app",
            workflow_id="claim-submit",
            step_id=f"step-{idx}",
            screenshot_hash=h,
            predicted_verdict=VetoDecision.HALT_ESCALATE,
            operator_verdict=VetoDecision.ALLOW,
        )
    # Duplicate of step-0 should deduplicate cleanly.
    collector.record_operator_override(
        tenant_id="clinic-north",
        app_id="billing-app",
        workflow_id="claim-submit",
        step_id="step-0",
        screenshot_hash=f"{0:064x}",
        predicted_verdict=VetoDecision.HALT_ESCALATE,
        operator_verdict=VetoDecision.ALLOW,
    )
    collector.record_operator_override(
        tenant_id="clinic-north",
        app_id="billing-app",
        workflow_id="claim-submit",
        step_id="step-fault",
        screenshot_hash="c" * 64,
        predicted_verdict=VetoDecision.VETO,
        operator_verdict=VetoDecision.VETO,
    )
    collector.record_operator_override(
        tenant_id="clinic-north",
        app_id="billing-app",
        workflow_id="claim-submit",
        step_id="step-missed",
        screenshot_hash="d" * 64,
        predicted_verdict=VetoDecision.ALLOW,
        operator_verdict=VetoDecision.VETO,
    )

    summary = collector.calibration_readiness(
        "clinic-north", "billing-app", min_samples=10
    )
    assert summary.total_records == 10
    assert summary.false_pause_count == 8
    assert summary.true_fault_count == 1
    assert summary.missed_fault_count == 1
    assert summary.ready_for_calibration is True
