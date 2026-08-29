"""Runtime of a ProcessContract: admissions first, then Execute."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from openadapt_flow.admitted_composition import (
    ProcessContract,
    author_process_contract,
    resolve_pointer,
)
from openadapt_flow.compiler.compose_authoring import author_composition
from openadapt_flow.composition import HandoffBinding
from openadapt_flow.ir import Workflow
from openadapt_flow.qualification_admission import QualificationAdmissionEnvelope
from openadapt_flow.runtime.admitted_composition import (
    AdmittedCapability,
    child_run_via_execute_client,
    execute,
    execute_process_contract,
)
from openadapt_flow.runtime.composition import ChildRunResult
from tests.test_admitted_composition_authoring import (
    INTAKE_ADMISSION,
    POSTING_ADMISSION,
    _reader,
    _save,
    _two_admitted,
    _writer,
)
from tests.test_qualification_admission import NOW, _trust


def _author_two(tmp_path: Path, **kwargs) -> tuple[ProcessContract, Path]:
    intake, intake_env, posting, posting_env = _two_admitted(tmp_path)
    out = tmp_path / "process"
    contract = author_process_contract(
        [
            ("intake", intake_env, intake),
            ("posting", posting_env, posting),
        ],
        handoffs=kwargs.pop(
            "handoffs",
            [
                HandoffBinding(
                    from_child="intake",
                    source="patient_id",
                    to_child="posting",
                    target="patient_id",
                )
            ],
        ),
        name="two-child",
        out=out,
        **kwargs,
    )
    return contract, out


def test_missing_handoff_halts(tmp_path: Path) -> None:
    contract, parent = _author_two(tmp_path)
    seen: list[str] = []

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        seen.append(child)
        assert isinstance(capability, AdmittedCapability)
        assert isinstance(admission, QualificationAdmissionEnvelope)
        if child == "intake":
            return ChildRunResult(
                child=child,
                outcome="VERIFIED",
                bound_params={},
                effect_facts={},
                success=True,
            )
        raise AssertionError("posting must not start without handoff evidence")

    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        now=NOW,
    )
    assert report.outcome == "HALTED"
    assert report.halted_at == "posting"
    assert "missing handoff evidence" in report.reason
    assert seen == ["intake"]


def test_expired_admission_halts_before_execute(tmp_path: Path) -> None:
    contract, parent = _author_two(tmp_path)

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        raise AssertionError("Execute must not run on an expired admission")

    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        now=NOW + timedelta(days=31),
    )
    assert report.outcome == "HALTED"
    assert report.halted_at == "intake"
    assert "expired" in report.reason
    assert report.children == []


def test_revoked_admission_halts_before_execute(tmp_path: Path) -> None:
    contract, parent = _author_two(tmp_path)

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        raise AssertionError("Execute must not run on a revoked admission")

    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        revoked_admission_ids={INTAKE_ADMISSION},
        now=NOW,
    )
    assert report.outcome == "HALTED"
    assert report.halted_at == "intake"
    assert "revoked" in report.reason
    assert report.children == []


def test_predecessor_not_verified_halts(tmp_path: Path) -> None:
    contract, parent = _author_two(tmp_path)
    seen: list[str] = []

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        seen.append(child)
        if child == "intake":
            return ChildRunResult(
                child=child,
                outcome="HALTED",
                bound_params={"patient_id": "alice"},
                effect_facts={"patient_id": "alice"},
                success=False,
            )
        raise AssertionError("posting must not start after an unverified predecessor")

    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        now=NOW,
    )
    assert report.outcome == "HALTED"
    assert report.halted_at == "intake"
    assert seen == ["intake"]
    assert report.children[0].effect_facts == {}


def test_allowed_halt_class_continues_without_minting_facts(tmp_path: Path) -> None:
    intake, intake_env, posting, posting_env = _two_admitted(tmp_path)
    out = tmp_path / "process"
    contract = author_process_contract(
        [
            ("intake", intake_env, intake),
            ("posting", posting_env, posting),
        ],
        allow_halt={"intake": ["HALTED"]},
        out=out,
    )
    seen: list[str] = []

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        seen.append(child)
        if child == "intake":
            return ChildRunResult(
                child=child,
                outcome="HALTED",
                bound_params={"patient_id": "alice"},
                effect_facts={"patient_id": "alice"},
                success=False,
            )
        return ChildRunResult(
            child=child,
            outcome="VERIFIED",
            bound_params=dict(inputs),
            effect_facts={},
            success=True,
        )

    report = execute_process_contract(
        contract,
        parent_dir=out,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        now=NOW,
    )
    assert seen == ["intake", "posting"]
    assert report.outcome != "VERIFIED"
    assert report.children[0].effect_facts == {}
    assert [item.child for item in report.children] == ["intake", "posting"]


def test_scripted_verified_handoff_copies_bound_param(tmp_path: Path) -> None:
    contract, parent = _author_two(tmp_path)
    received: dict[str, str] = {}

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        assert capability.admission_id in {INTAKE_ADMISSION, POSTING_ADMISSION}
        assert admission.payload.admission_id == capability.admission_id
        if child == "intake":
            return ChildRunResult(
                child=child,
                outcome="VERIFIED",
                bound_params={"patient_id": "alice", "note": "triage"},
                effect_facts={"patient_id": "alice"},
                success=True,
            )
        received.update(inputs)
        return ChildRunResult(
            child=child,
            outcome="VERIFIED",
            bound_params=dict(inputs),
            effect_facts={},
            success=True,
        )

    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        now=NOW,
        inputs={"note": "triage"},
    )
    assert report.outcome == "VERIFIED"
    assert report.model_calls == 0
    assert received["patient_id"] == "alice"
    assert received["note"] == "triage"


def test_model_call_on_child_forbids_parent_verified(tmp_path: Path) -> None:
    contract, parent = _author_two(tmp_path)

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        if child == "intake":
            return ChildRunResult(
                child=child,
                outcome="VERIFIED",
                bound_params={"patient_id": "alice"},
                effect_facts={"patient_id": "alice"},
                model_calls=1,
                success=True,
            )
        return ChildRunResult(
            child=child,
            outcome="VERIFIED",
            bound_params=dict(inputs),
            effect_facts={},
            success=True,
        )

    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        now=NOW,
    )
    assert report.outcome == "COMPLETED_UNVERIFIED"
    assert report.model_calls == 1
    assert report.success is False


def test_receipt_names_admissions_redacts_handoffs_omits_window_titles(
    tmp_path: Path,
) -> None:
    contract, parent = _author_two(tmp_path)

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        if child == "intake":
            return ChildRunResult(
                child=child,
                outcome="VERIFIED",
                bound_params={"patient_id": "alice", "window_title": "Chart"},
                effect_facts={"patient_id": "alice"},
                success=True,
            )
        return ChildRunResult(
            child=child,
            outcome="VERIFIED",
            bound_params=dict(inputs),
            effect_facts={},
            success=True,
        )

    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        now=NOW,
    )
    assert report.outcome == "VERIFIED"
    path = tmp_path / "run" / "process-report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8")
    assert INTAKE_ADMISSION in text
    assert POSTING_ADMISSION in text
    assert payload["children"][0]["admission_id"] == INTAKE_ADMISSION
    assert payload["children"][1]["admission_id"] == POSTING_ADMISSION
    assert payload["children"][0]["bundle_content_digest"]
    assert payload["children"][1]["bundle_content_digest"]
    assert "alice" not in text
    assert "<bound>" in text
    assert "window_title" not in text
    assert "Chart" not in text
    assert "window titles" not in text.lower()


def test_execute_refuses_none_admission(tmp_path: Path) -> None:
    intake = _save(tmp_path, _writer(), "intake")
    workflow = _writer()

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        raise AssertionError("must not run")

    with pytest.raises(Exception, match="real AdmittedCapability"):
        execute(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            {},
            workflow=workflow,
            bundle_dir=intake,
            run_dir=tmp_path / "run",
            child="intake",
            child_run=child_run,
        )


def test_pointing_runtime_at_compose_recordings_fails_admission(
    tmp_path: Path,
) -> None:
    intake = _save(tmp_path, _writer(), "intake")
    posting = _save(tmp_path, _reader(), "posting")
    composed = tmp_path / "composed"
    author_composition(
        [("intake", intake), ("posting", posting)],
        out=composed,
    )

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        raise AssertionError("compose recordings must not reach Execute")

    with pytest.raises(Exception, match="process-contract"):
        ProcessContract.load(composed)
    # Green path: two envelopes still execute.
    admitted = tmp_path / "admitted"
    admitted.mkdir()
    contract, parent = _author_two(admitted)
    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run-ok",
        child_run=lambda capability, admission, inputs, *, workflow, bundle_dir, run_dir, child: (
            ChildRunResult(
                child=child,
                outcome="VERIFIED",
                bound_params={"patient_id": "alice"}
                if child == "intake"
                else dict(inputs),
                effect_facts={"patient_id": "alice"} if child == "intake" else {},
                success=True,
            )
        ),
        trusted_signers=_trust(),
        now=NOW,
    )
    assert report.outcome == "VERIFIED"


def test_digest_mismatch_halts_before_execute(tmp_path: Path) -> None:
    contract, parent = _author_two(tmp_path)
    bundle = resolve_pointer(parent, contract.child("intake").bundle)
    workflow = Workflow.load(bundle)
    workflow.name = "tampered-intake"
    workflow.save(bundle)

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        raise AssertionError("Execute must not run on a digest mismatch")

    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        now=NOW,
    )
    assert report.outcome == "HALTED"
    assert report.halted_at == "intake"
    assert "digest" in report.reason
    assert report.children == []


def _fake_execute_client(*, outcome: str = "verified", model_used: bool = False):
    pytest.importorskip("openadapt_types")
    from openadapt_types.execute import (
        EffectStrengthV1,
        ExecuteAcceptedV1,
        ExecuteEvidenceContractV1,
        ExecuteEvidenceReceiptV1,
        ExecuteLifecycleStateV1,
        ExecuteStatusV1,
        ExecuteTerminalOutcomeV1,
    )

    terminal = ExecuteTerminalOutcomeV1(outcome)
    passed = terminal is ExecuteTerminalOutcomeV1.VERIFIED

    class FakeExecuteClient:
        def __init__(self) -> None:
            self.requests: list[object] = []

        def create_execution(self, request: object) -> object:
            self.requests.append(request)
            return ExecuteAcceptedV1(execution_id="exec_intake01")

        def get_execution(self, execution_id: str) -> object:
            return ExecuteStatusV1(
                execution_id=execution_id,
                state=ExecuteLifecycleStateV1.TERMINAL,
                terminal_outcome=terminal,
                evidence_receipt_id="receipt_intake01",
                updated_at="2026-01-15T12:00:00Z",
            )

        def get_receipt(self, execution_id: str) -> object:
            digest = "sha256:" + "ab" * 32
            return ExecuteEvidenceReceiptV1(
                receipt_id="receipt_intake01",
                execution_id=execution_id,
                workflow_digest=digest,
                outcome=terminal,
                contracts=ExecuteEvidenceContractV1(
                    authorization_passed=passed,
                    identity_passed=passed,
                    postcondition_passed=passed,
                    effect_passed=passed,
                    minimum_effect_strength=(
                        EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD
                    ),
                    observed_effect_strength=(
                        EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD
                        if passed
                        else None
                    ),
                    model_used=model_used,
                    external_network_used=False,
                ),
                delivery_uncertain=False,
                evidence_digest=digest,
                issued_at="2026-01-15T12:00:01Z",
            )

    return FakeExecuteClient()


def test_process_children_hit_execute_client_with_real_admission(
    tmp_path: Path,
) -> None:
    contract, parent = _author_two(tmp_path)
    client = _fake_execute_client()
    child_run = child_run_via_execute_client(
        client,
        environment_id="environment_12345678",
        actor_id="caller_agent_12345678",
    )
    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        now=NOW,
        inputs={"note": "triage"},
    )
    assert len(client.requests) == 1
    request = client.requests[0]
    assert request.qualification_id == INTAKE_ADMISSION
    assert request.workflow_version == contract.child("intake").workflow_version_id
    assert request.workflow_digest.startswith("sha256:")
    assert report.children[0].outcome == "VERIFIED"
    assert report.halted_at == "posting"
    assert "missing handoff evidence" in report.reason


def test_execute_client_model_call_forbids_parent_verified(
    tmp_path: Path,
) -> None:
    intake, intake_env, posting, posting_env = _two_admitted(tmp_path)
    out = tmp_path / "process"
    contract = author_process_contract(
        [
            ("intake", intake_env, intake),
            ("posting", posting_env, posting),
        ],
        out=out,
    )
    client = _fake_execute_client(model_used=True)
    report = execute_process_contract(
        contract,
        parent_dir=out,
        run_dir=tmp_path / "run",
        child_run=child_run_via_execute_client(
            client,
            environment_id="environment_12345678",
            actor_id="caller_agent_12345678",
        ),
        trusted_signers=_trust(),
        now=NOW,
    )
    assert report.model_calls >= 1
    assert report.outcome != "VERIFIED"
