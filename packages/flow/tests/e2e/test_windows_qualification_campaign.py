"""All six native qualification cases against real WinForms and SQLite.

This is an opt-in Windows-only integration test. The existing synthetic form
is recorded through the real TLS-pinned win_agent, then qualified with real
UIA/physical screen mutations. Independent complete SQLite snapshots judge
each save, and the final local admission must be signed and bound to Windows.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tests.e2e.test_windows_hosted_replay import (
    DEMO_NOTE,
    DEMO_SEARCH,
    RUN_E2E,
    TARGET_ID,
    _Host,
    _points,
)

pytestmark = [
    pytest.mark.skipif(not RUN_E2E, reason="opt-in native Windows campaign"),
    pytest.mark.timeout(1800),
]


def test_native_windows_campaign_reads_back_normal_and_all_faults(
    tmp_path, monkeypatch
):
    from openadapt_flow.adapters.desktop_recorder import record_desktop_demo
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import ActionKind, StructuralLocator
    from openadapt_flow.qualify import QualifyHooks, parse_verify_config, qualify

    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", "synthetic-windows-qualification-key")
    host = _Host(tmp_path)
    evidence = {"cases": {}, "completed": False}
    try:
        backend = host.start_agent(allow_qualification_faults=True)
        assert "qualification_faults_v1" in backend.agent_capabilities()
        host.launch_app()
        points = _points(backend)

        def demonstrate(rec):
            rec.click(*points["searchBox"])
            rec.type_text(DEMO_SEARCH)
            rec.press("Enter")
            time.sleep(0.4)
            rec.click(*points["searchButton"])
            time.sleep(0.4)
            rec.click(*points["noteBox"])
            rec.type_text(DEMO_NOTE, param="note")
            rec.click(*points["saveButton"])
            time.sleep(0.4)

        recording = record_desktop_demo(backend, tmp_path / "recording", demonstrate)
        # The demonstrator marks the record identity, just as record --identifier
        # does. The real UIA rectangle supplies the crop; no identity gate is relaxed.
        header = backend.locate_structural(
            StructuralLocator(automation_id="patientHeader")
        )
        assert header is not None and header.region is not None
        meta_path = recording / "meta.json"
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        metadata["identifier_region"] = [int(value) for value in header.region]
        meta_path.write_text(json.dumps(metadata), encoding="utf-8")
        assert (
            next(row for row in host.rows() if row["id"] == TARGET_ID)["note"]
            == DEMO_NOTE
        )
        bundle = tmp_path / "compiled"
        workflow = compile_recording(
            recording, bundle, name="native-qualification-fixture"
        )
        clicks = [step for step in workflow.steps if step.action is ActionKind.CLICK]
        assert all(step.identity_armed for step in clicks)
        save = clicks[-1]
        save.risk = "irreversible"
        workflow.save(bundle)
        config = parse_verify_config(
            {
                "schema_version": "openadapt.verify/v1",
                "app": {
                    "target": "windows",
                    "surface": "remote_in_session",
                    "name": "Synthetic native notes",
                    "url": "windows://synthetic-notes",
                    "version": "in-tree-fixture-v1",
                    "session": "fixture:open_session",
                    "reset": {"command": "unused (hook provided)"},
                },
                "verifier": {
                    "kind": "sql",
                    "sqlite_database": str(host.root / "patients.db"),
                    "sql_query": "SELECT id, first, last, dob, note FROM patients",
                },
                "save_step": save.id,
                "effects": [
                    {
                        "kind": "field_equals",
                        "match": {"id": {"literal": "1"}},
                        "field": "note",
                        "value": {"literal": DEMO_NOTE},
                        "idempotency_key": {"literal": DEMO_NOTE},
                        "key_field": "note",
                        "risk": "reversible",
                        "probe": "read complete SQLite rows",
                    }
                ],
                "inputs": {"params": {"note": DEMO_NOTE}},
                "campaign": {"trials": 3, "policy": "clinical-write"},
            },
            env={},
        )
        baseline = []

        def reset():
            host.launch_app()
            points = _points(backend)
            backend.click(*points["searchBox"])
            time.sleep(0.3)
            baseline[:] = host.rows()

        def after_run(ctx, current_backend, report):
            after = host.rows()
            evidence["cases"][ctx.case_id] = {
                "outcome": report.execution_outcome,
                "before": list(baseline),
                "after": after,
                "fault_staged": report.governed_qualification_fault_driver_id,
                "halts": [
                    str(result.error) for result in report.results if result.error
                ],
            }
            expected = [dict(row) for row in baseline]
            if ctx.case_id == "representative":
                next(row for row in expected if row["id"] == TARGET_ID)["note"] = (
                    DEMO_NOTE
                )
                assert report.execution_outcome == "VERIFIED"
            else:
                assert report.execution_outcome == "HALTED"
            assert after == expected

        result = qualify(
            bundle,
            config,
            out=tmp_path / "campaign",
            certify="clinical-write",
            hooks=QualifyHooks(
                open_session=lambda: (backend, lambda: None),
                reset=reset,
                after_run=after_run,
            ),
            log=print,
        )
        assert result.passed and result.certified and not result.partial
        assert result.result["fault_driver"]["kind"] == "windows-generic"
        assert len(result.result["cases"]) == 18
        assert all(case["passed"] for case in result.result["cases"])
        assert result.result["silent_incorrect_success_count"] == 0
        assert result.result["verifier_model_calls"] == 0
        from base64 import b64decode

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        from openadapt_flow.qualification_admission import (
            SIGNATURE_DOMAIN,
            QualificationAdmissionEnvelope,
            QualificationAdmissionError,
            expected_from_payload,
            verify_qualification_admission,
        )
        from openadapt_flow.qualification_dev_signer import LOCAL_DEV_PUBLIC_BYTES
        from openadapt_flow.qualify.result import result_sha256

        assert result.admission_note is None
        admission = QualificationAdmissionEnvelope.model_validate_json(
            (result.out / "qualification" / "admission.local.json").read_text()
        )
        Ed25519PublicKey.from_public_bytes(LOCAL_DEV_PUBLIC_BYTES).verify(
            b64decode(admission.signature, validate=True),
            SIGNATURE_DOMAIN + admission.payload.canonical_bytes(),
        )
        assert admission.payload.campaign.outcomes_sha256 == result_sha256(
            result.result
        )
        assert all(
            task.observed_trials >= 3 for task in admission.payload.campaign.tasks
        )
        with pytest.raises(QualificationAdmissionError, match="local-dev"):
            verify_qualification_admission(
                admission,
                trusted_signers={},
                expected=expected_from_payload(admission.payload),
            )
        evidence.update(
            completed=True,
            result=result.result,
            admission_sha256=admission.artifact_sha256(),
            admission_verified=True,
        )

    finally:
        host.close()
        if os.environ.get("OAFLOW_WINDOWS_QUALIFICATION_EVIDENCE"):
            Path(os.environ["OAFLOW_WINDOWS_QUALIFICATION_EVIDENCE"]).write_text(
                json.dumps(evidence, indent=2, default=str), encoding="utf-8"
            )
