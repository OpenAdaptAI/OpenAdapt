"""Software controls for the hosted Windows campaign's fixed report projection."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.execution_profiles import (
    ExecutionOutcome,
    ExecutionProfile,
    classify_execution_outcome,
)
from openadapt_flow.ir import SafetyRefusalEvidence, Workflow
from openadapt_flow.qualification import (
    QualificationCaseKind,
    _case_run_report_integrity_error,
)
from openadapt_flow.qualify.campaign import TrialContext
from openadapt_flow.runtime import Replayer
from tests.e2e.test_windows_hosted_campaign import _CampaignHost, _safe_case_evidence
from tests.test_qualification_fault_contract import (
    _fault_authorization,
    _fault_case_integrity_result,
    _FaultDriver,
    _ObservedBackend,
    _StrongEffectVerifier,
    _two_write_fault_workflow,
    _Vision,
)


def _returned_case(tmp_path: Path, *, early: bool = False):
    kind = QualificationCaseKind.MISSING_EFFECT
    driver = _FaultDriver(kind)
    workflow, bundle = _two_write_fault_workflow(tmp_path, driver)
    backend = _ObservedBackend()
    # Retain the actual observed backend class/session; its existing resolver
    # raises StructuralResolutionRefused for this synthetic ambiguity.
    backend.ambiguous = early
    run_id = "case-evidence-trial-01"
    run_dir = tmp_path / "run"
    report = Replayer(
        backend,
        vision=_Vision(),
        governed_authorization=_fault_authorization(
            workflow,
            kind,
            driver,
            run_id=run_id,
            action_paths={"prepare": "gui", "submit": "gui"},
            required_identity_step_ids=("prepare", "submit"),
            fault_step_id="submit",
        ),
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        run_id=run_id,
        execution_target_kind="web",
    )
    context = TrialContext(
        case_id="fault-missing-effect",
        expected="HALTED",
        oracle_kind="no_mutation",
        trial=1,
        trial_dir=tmp_path,
        run_dir=run_dir,
        run_id=run_id,
        params={},
    )
    return workflow, bundle, report, context, backend


@pytest.mark.parametrize("early", [True, False])
def test_returned_fault_case_preserves_actual_prefix_admission(
    tmp_path: Path, early: bool
):
    workflow, bundle, report, context, backend = _returned_case(tmp_path, early=early)
    original = report.model_dump_json()
    assert report.execution_outcome == "HALTED"
    projection = _safe_case_evidence(bundle, context, report)
    assert report.model_dump_json() == original
    assert projection["status"] == "available"
    assert projection["case_ordinal"] == 6
    assert projection["trial"] == 1
    assert projection["target_ordinal"] == 2
    assert projection["failed_ordinal"] == (1 if early else 2)
    assert projection["executed_count"] == (1 if early else 2)
    assert projection["exact_ordered_prefix"] is (not early)
    outcome = classify_execution_outcome(
        report,
        workflow,
        ExecutionProfile.STANDARD,
        transition_evidence_root=context.run_dir,
        _qualification_fault_target_step_id="submit",
    )
    assert projection["standard_prefix_outcome"] == outcome.value
    if early:
        assert backend.actions == []
        assert report.qualification_fault_mutations == []
        assert outcome is ExecutionOutcome.COMPLETED_UNVERIFIED
        assert projection["refusal_stage"] == "target_resolution"
        assert projection["refusal_code"] == "target_ambiguous"
    else:
        assert len(backend.actions) == 1
        assert len(report.qualification_fault_mutations) == 1
        assert outcome is ExecutionOutcome.VERIFIED
        assert projection["refusal_stage"] == "effect_verifier"
        assert projection["refusal_code"] == "effect_verifier_missing"
        case, result = _fault_case_integrity_result(
            workflow=workflow,
            report=report,
            evidence_root=tmp_path / "integrity",
            run_dir=context.run_dir,
        )
        assert (
            _case_run_report_integrity_error(
                workflow=workflow,
                project=workflow.qualification,
                case=case,
                result=result,
                evidence_root=tmp_path / "integrity",
            )
            is None
        )


@pytest.mark.parametrize("field", ["case", "trial", "run", "refusal"])
def test_unbound_or_nonfinite_projection_is_unavailable(tmp_path: Path, field: str):
    _workflow, bundle, report, context, _backend = _returned_case(tmp_path)
    if field == "case":
        context.case_id = "unpublished-sensitive-case"
    elif field == "trial":
        context.trial = True
    elif field == "run":
        context.run_id = "unpublished-sensitive-run"
    else:
        report.results[
            -1
        ].safety_refusal_evidence = SafetyRefusalEvidence.model_construct(
            stage="target_resolution",
            code="unpublished-sensitive-code",
            detector_input_sha256="f" * 64,
        )
    original = report.model_dump_json()
    projection = _safe_case_evidence(bundle, context, report)
    assert projection == {"status": "unavailable", "code": "case_evidence_unavailable"}
    assert "sensitive" not in json.dumps(projection)
    assert report.model_dump_json() == original


def test_projection_failure_does_not_replace_returned_report(
    tmp_path: Path, monkeypatch
):
    _workflow, bundle, report, context, _backend = _returned_case(tmp_path)
    original = report.model_dump_json()

    def unavailable(_path):
        raise RuntimeError("unpublished-sensitive-error")

    monkeypatch.setattr(Workflow, "load", unavailable)
    assert _safe_case_evidence(bundle, context, report) == {
        "status": "unavailable",
        "code": "case_evidence_unavailable",
    }
    assert report.model_dump_json() == original


@pytest.mark.parametrize("available", [True, False])
def test_actual_oracle_keeps_diagnostics_out_of_qualification_input(
    tmp_path: Path, available: bool
):
    _workflow, bundle, report, context, backend = _returned_case(tmp_path)
    root = tmp_path / "host"
    projected_bundle = root / "campaign" / "bundle"
    if available:
        shutil.copytree(bundle, projected_bundle)
    rows = [{"id": 1, "note": ""}]
    context.state["sqlite_before"] = rows
    host = SimpleNamespace(root=root, rows=lambda: [dict(row) for row in rows])
    owner = _CampaignHost(host, backend, None, ("", "", "", ""), (0, 0, 1, 1), ())
    original = report.model_dump_json()
    returned = owner.oracle(context, report)
    assert returned == {
        "schema_version": "openadapt.qualify-readback/v1",
        "oracle_kind": "no_mutation",
        "read_path": "independent SQLite CLI, complete row comparison",
        "independent": True,
        "passed": True,
        "observed": "no_mutation",
        "effect_verdict": "REFUTED",
        "landed": "none",
        "silent_incorrect_success": False,
        "changed_ids": [],
        "records_before": 1,
        "records_after": 1,
        "fault_receipt_count": 1,
        "fault_detector_error": None,
        "execution_outcome": "HALTED",
        "model_calls": 0,
    }
    assert report.model_dump_json() == original
    assert len(owner.oracles) == 1
    sidecar = owner.oracles[0]
    assert sidecar["case_id"] == context.case_id
    assert sidecar["trial"] == context.trial
    assert sidecar["informational"] is False
    projection = sidecar.pop("production_evidence")
    assert projection["status"] == ("available" if available else "unavailable")
    assert sidecar == {
        "case_id": context.case_id,
        "trial": context.trial,
        "informational": False,
        **returned,
    }
