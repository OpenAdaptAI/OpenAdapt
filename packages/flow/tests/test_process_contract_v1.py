from __future__ import annotations

import base64
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openadapt_types.authentication import (
    AuthenticationReceiptPayloadV1,
    AuthenticationRunBindingV1,
    issue_authentication_receipt,
)
from openadapt_types.human_decision import (
    HumanDecisionTaskV1,
    sign_human_decision_receipt_hmac,
)
from openadapt_types.process_capability import (
    ArtifactRefV1,
    CodeArtifactOutputV1,
    CodeCapabilityAdmissionEnvelopeV1,
    CodeCapabilityAdmissionPayloadV1,
    CodeCapabilityManifestV1,
    CodeIsolationProfile,
    CodeNetworkMode,
    CodePermissionContractV1,
)

from openadapt_flow.admitted_composition import (
    AdmittedChildSpec,
    ArtifactEdgeV1,
    CodeChildSpec,
    HumanChildSpec,
    ProcessContract,
    ProcessContractV1,
    topological_order,
)
from openadapt_flow.runtime.composition import ChildRunResult
from openadapt_flow.runtime.process_v1 import (
    HumanCompletionV1,
    ProcessV1Error,
    execute_process_contract_v1,
)
from openadapt_flow.visualize.admitted_composition import build_process_graph
from tests.test_admitted_composition_authoring import _two_admitted
from tests.test_qualification_admission import _trust


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _archive(path: Path, script: str) -> tuple[str, str]:
    lock = b"# exact empty environment\n"
    with zipfile.ZipFile(path, "w") as output:
        output.writestr("main.py", script)
        output.writestr("requirements.lock", lock)
    return _digest(path.read_bytes()), _digest(lock)


def _code_child(
    root: Path,
    name: str,
    script: str,
    *,
    role: str,
    private_key: Ed25519PrivateKey,
    runtime_digest: str,
    output: CodeArtifactOutputV1 | None,
) -> tuple[CodeChildSpec, bytes]:
    archive = root / f"{name}.zip"
    archive_digest, lock_digest = _archive(archive, script)
    permissions = CodePermissionContractV1(
        isolation_profile=CodeIsolationProfile.TRUSTED_LOCAL,
        network_mode=CodeNetworkMode.NONE,
        timeout_seconds=10,
        memory_limit_mb=128,
        output_limit_bytes=1_000_000,
    )
    manifest = CodeCapabilityManifestV1(
        capability_id=f"capability-{name}",
        capability_version_id=f"version-{name}-0001",
        runtime_version=f"{sys.version_info.major}.{sys.version_info.minor}",
        source_archive_digest=archive_digest,
        lockfile_path="requirements.lock",
        lockfile_digest=lock_digest,
        entrypoint=("main.py",),
        input_schema_digest=_digest(f"{name}-input".encode()),
        output_schema_digest=_digest(f"{name}-output".encode()),
        outputs=(() if output is None else (output,)),
        permissions=permissions,
        effect_contract_digest=_digest(f"{name}-effect".encode()),
        oracle_contract_digest=_digest(f"{name}-oracle".encode()),
        qualification_campaign_digest=_digest(f"{name}-campaign".encode()),
    )
    now = datetime.now(timezone.utc)
    payload = CodeCapabilityAdmissionPayloadV1(
        admission_id=f"admission-{name}-0001",
        tenant_id="tenant-local-0001",
        capability_id=manifest.capability_id,
        capability_version_id=manifest.capability_version_id,
        manifest_digest=manifest.digest,
        runtime_environment_digest=runtime_digest,
        permission_contract_digest=permissions.digest,
        input_schema_digest=manifest.input_schema_digest,
        output_schema_digest=manifest.output_schema_digest,
        effect_contract_digest=manifest.effect_contract_digest,
        oracle_contract_digest=manifest.oracle_contract_digest,
        operator_contract_digest=_digest(f"{name}-operator".encode()),
        qualification_campaign_digest=manifest.qualification_campaign_digest,
        issuer_key_id="code-signer-0001",
        issuer_workflow="issuer-workflow-0001",
        issuer_ref="issuer-ref-0001",
        issued_at=_timestamp(now),
        not_before=_timestamp(now),
        expires_at=_timestamp(now + timedelta(days=1)),
    )
    envelope = CodeCapabilityAdmissionEnvelopeV1(
        payload=payload,
        signature=base64.b64encode(
            private_key.sign(payload.canonical_bytes())
        ).decode(),
    )
    manifest_path = root / f"{name}-manifest.json"
    admission_path = root / f"{name}-admission.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    admission_path.write_text(envelope.model_dump_json(indent=2), encoding="utf-8")
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return (
        CodeChildSpec(
            name=name,
            manifest=manifest_path.name,
            admission=admission_path.name,
            source_archive=archive.name,
            role=role,
        ),
        public,
    )


def _no_flow(*_args, **_kwargs):
    raise AssertionError("the code-only ProcessContract must not call Flow")


def test_v1_sealed_code_artifact_verifier_and_signed_receipt(tmp_path: Path) -> None:
    signer = Ed25519PrivateKey.generate()
    receipt_key = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    runtime_digest = _digest(b"python-test-runtime")
    transform_script = """
import os
from pathlib import Path
out = Path(os.environ['OPENADAPT_PROCESS_OUTPUT'])
out.mkdir(parents=True, exist_ok=True)
(out / 'result.txt').write_text('42', encoding='utf-8')
"""
    verifier_script = """
import json, os
from pathlib import Path
inputs = json.loads(Path(os.environ['OPENADAPT_PROCESS_INPUT']).read_text())
artifact = inputs['verify_transform_result']['artifact_ref']
out = Path(os.environ['OPENADAPT_PROCESS_OUTPUT'])
out.mkdir(parents=True, exist_ok=True)
(out / 'verification.json').write_text(json.dumps({
  'schema_version': 'openadapt.artifact-verification/v1',
  'outcome': 'verified',
  'oracle_tier': 2,
  'artifact_digests': [artifact['content_digest']],
}))
"""
    transform, public = _code_child(
        tmp_path,
        "transform",
        transform_script,
        role="transform",
        private_key=signer,
        runtime_digest=runtime_digest,
        output=CodeArtifactOutputV1(
            name="result", relative_path="result.txt", media_type="text/plain"
        ),
    )
    verifier, _ = _code_child(
        tmp_path,
        "verifier",
        verifier_script,
        role="verifier",
        private_key=signer,
        runtime_digest=runtime_digest,
        output=None,
    )
    contract = ProcessContract(
        schema_version="openadapt.process-contract/v1",
        name="portable-process",
        code_children=[transform, verifier],
        after={"verifier": ["transform"]},
        artifact_edges=[
            ArtifactEdgeV1(
                from_child="transform",
                from_output="result",
                to_child="verifier",
                to_input="result",
                verifier_child="verifier",
            )
        ],
    )
    contract.save(tmp_path)

    result = execute_process_contract_v1(
        contract,
        parent_dir=tmp_path,
        run_dir=tmp_path / "run",
        inputs={},
        child_run=_no_flow,
        qualification_signers={},
        code_signers={"code-signer-0001": public},
        runtime_environment_digest=runtime_digest,
        receipt_private_key=receipt_key,
        receipt_issuer_key_id="receipt-signer-0001",
        environment_id="environment-local-0001",
        runner_id="runner-local-0001",
        allow_trusted_code=True,
    )

    assert result.state.outcome == "verified"
    assert result.receipt_path is not None
    assert result.state.completed["transform"].outcome == "verified"
    artifact = next(iter(result.state.artifacts.values()))
    assert artifact["verification_state"] == "verified"
    ArtifactRefV1.model_validate(
        {
            key: value
            for key, value in artifact.items()
            if key not in {"producer", "store_path"}
        }
    )
    assert artifact["storage_boundary"] == "local_protected"
    assert artifact["data_classification"] == "regulated"
    assert Path(tmp_path / "run" / artifact["store_path"]).read_text() == "42"


def test_v1_refuses_source_change_before_code_runs(tmp_path: Path) -> None:
    signer = Ed25519PrivateKey.generate()
    runtime_digest = _digest(b"runtime")
    child, public = _code_child(
        tmp_path,
        "transform",
        "raise SystemExit('must not run')",
        role="transform",
        private_key=signer,
        runtime_digest=runtime_digest,
        output=None,
    )
    (tmp_path / child.source_archive).write_bytes(b"changed")
    contract = ProcessContract(
        schema_version="openadapt.process-contract/v1",
        name="tampered",
        code_children=[child],
    )
    key = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    with pytest.raises(ProcessV1Error, match="source digest differs"):
        execute_process_contract_v1(
            contract,
            parent_dir=tmp_path,
            run_dir=tmp_path / "run",
            inputs={},
            child_run=_no_flow,
            qualification_signers={},
            code_signers={"code-signer-0001": public},
            runtime_environment_digest=runtime_digest,
            receipt_private_key=key,
            receipt_issuer_key_id="receipt-signer-0001",
            environment_id="environment-local-0001",
            runner_id="runner-local-0001",
            allow_trusted_code=True,
        )


def test_v1_runs_an_admitted_flow_child(tmp_path: Path) -> None:
    intake, intake_envelope_path, _, _ = _two_admitted(tmp_path)
    envelope = json.loads(intake_envelope_path.read_text())
    payload = envelope["payload"]
    contract = ProcessContract(
        schema_version="openadapt.process-contract/v1",
        name="flow-process",
        children=[
            AdmittedChildSpec(
                name="intake",
                admission_id=payload["admission_id"],
                workflow_version_id=payload["workflow_version_id"],
                bundle_content_digest=payload["bundle_content_digest"],
                envelope=intake_envelope_path.relative_to(tmp_path).as_posix(),
                bundle=intake.relative_to(tmp_path).as_posix(),
                surface="web",
            )
        ],
    )
    key = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    seen: list[str] = []

    def flow_run(capability, admission, inputs, **kwargs):
        seen.append(capability.name)
        return ChildRunResult(
            child=capability.name,
            outcome="VERIFIED",
            bound_params=dict(inputs),
            effect_facts={},
            model_calls=0,
            success=True,
        )

    result = execute_process_contract_v1(
        contract,
        parent_dir=tmp_path,
        run_dir=tmp_path / "run-flow",
        inputs={},
        child_run=flow_run,
        qualification_signers=_trust(),
        code_signers={},
        runtime_environment_digest=_digest(b"unused"),
        receipt_private_key=key,
        receipt_issuer_key_id="receipt-signer-0001",
        environment_id="environment-local-0001",
        runner_id="runner-local-0001",
        allow_trusted_code=False,
    )

    assert result.state.outcome == "verified"
    assert seen == ["intake"]


def _human_provider(child, task, authentication, request_dir):
    key = (request_dir.parents[1] / ".human-task.key").read_bytes()
    request_digest = _digest(
        json.dumps(
            {
                "child": child.name,
                "process_digest": task.capability_digest,
                "task_digest": task.digest,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    receipt = sign_human_decision_receipt_hmac(
        key=key,
        fields={
            "task_id": task.task_id,
            "task_revision": task.task_revision,
            "pause_id": task.pause_id,
            "capability_digest": task.capability_digest,
            "request_digest": request_digest,
            "decision_digest": _digest(b"decision"),
            "transition_receipt_digest": _digest(b"transition"),
            "action": "verify_and_resume",
            "state": "completed",
            "reason_code": "verified_and_resumed",
            "report_success": True,
            "decided_at": _timestamp(datetime.now(timezone.utc)),
        },
    )
    return HumanCompletionV1(receipt=receipt)


def test_v1_human_task_pauses_and_resumes_from_the_same_journal(tmp_path: Path) -> None:
    contract = ProcessContract(
        schema_version="openadapt.process-contract/v1",
        name="human-process",
        human_children=[
            HumanChildSpec(
                name="review",
                task_kind="review",
                substrate="browser",
                risk_class="consequential",
                required_authn="aal2",
            )
        ],
    )
    key = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    first = execute_process_contract_v1(
        contract,
        parent_dir=tmp_path,
        run_dir=tmp_path / "run",
        inputs={},
        child_run=_no_flow,
        qualification_signers={},
        code_signers={},
        runtime_environment_digest=_digest(b"unused"),
        receipt_private_key=key,
        receipt_issuer_key_id="receipt-signer-0001",
        environment_id="environment-local-0001",
        runner_id="runner-local-0001",
        allow_trusted_code=False,
    )
    assert first.state.status == "waiting_human"
    execution_id = first.state.process_execution_id
    request_dir = tmp_path / "run/human/review"
    task = HumanDecisionTaskV1.model_validate_json(
        (request_dir / "human-task.json").read_text()
    )
    (request_dir / "human-completion.json").write_text('{"done": true}')
    with pytest.raises(ProcessV1Error, match="completion envelope is invalid"):
        execute_process_contract_v1(
            contract,
            parent_dir=tmp_path,
            run_dir=tmp_path / "run",
            inputs={},
            child_run=_no_flow,
            qualification_signers={},
            code_signers={},
            runtime_environment_digest=_digest(b"unused"),
            receipt_private_key=key,
            receipt_issuer_key_id="receipt-signer-0001",
            environment_id="environment-local-0001",
            runner_id="runner-local-0001",
            allow_trusted_code=False,
        )
    completion = _human_provider(contract.human_children[0], task, None, request_dir)
    (request_dir / "human-completion.json").write_text(
        json.dumps(
            {
                "schema_version": "openadapt.process-human-completion/v1",
                "receipt": completion.receipt.model_dump(mode="json"),
                "authentication_binding": None,
                "authentication_receipt": None,
            }
        )
    )

    resumed = execute_process_contract_v1(
        contract,
        parent_dir=tmp_path,
        run_dir=tmp_path / "run",
        inputs={},
        child_run=_no_flow,
        qualification_signers={},
        code_signers={},
        runtime_environment_digest=_digest(b"unused"),
        receipt_private_key=key,
        receipt_issuer_key_id="receipt-signer-0001",
        environment_id="environment-local-0001",
        runner_id="runner-local-0001",
        allow_trusted_code=False,
    )
    assert resumed.state.process_execution_id == execution_id
    assert resumed.state.outcome == "verified"
    assert resumed.receipt_path is not None


def test_v1_human_actuation_cannot_self_verify_its_effect(tmp_path: Path) -> None:
    contract = ProcessContract(
        schema_version="openadapt.process-contract/v1",
        name="human-actuation",
        human_children=[
            HumanChildSpec(
                name="download",
                task_kind="actuate",
                substrate="browser",
                risk_class="state_changing",
                required_authn="aal2",
            )
        ],
    )
    key = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )

    result = execute_process_contract_v1(
        contract,
        parent_dir=tmp_path,
        run_dir=tmp_path / "run-actuation",
        inputs={},
        child_run=_no_flow,
        qualification_signers={},
        code_signers={},
        runtime_environment_digest=_digest(b"unused"),
        receipt_private_key=key,
        receipt_issuer_key_id="receipt-signer-0001",
        environment_id="environment-local-0001",
        runner_id="runner-local-0001",
        allow_trusted_code=False,
        human_completion_provider=_human_provider,
    )

    assert result.state.outcome == "failed_platform"
    assert result.state.completed["download"].outcome == "completed_unverified"
    assert result.state.completed["download"].oracle_tier == 0


def test_v1_authentication_requires_exact_live_cross_surface_binding(
    tmp_path: Path,
) -> None:
    template = {
        "substrate": "browser",
        "allowed_methods": ["passkey"],
        "principal_class": "named_user",
        "requires_user_presence": True,
        "mfa_policy": "required",
        "max_session_age_seconds": 900,
        "verifier_id": "auth-verifier-0001",
        "verifier_kind": "authenticated_session_probe",
        "verifier_contract_digest": _digest(b"verifier"),
        "principal_binding_contract_digest": _digest(b"principal"),
        "application_contract_digest": _digest(b"application"),
        "environment_contract_digest": _digest(b"environment"),
        "broker_attestation": "not_applicable",
    }
    (tmp_path / "authentication.json").write_text(json.dumps(template))
    contract = ProcessContract(
        schema_version="openadapt.process-contract/v1",
        name="authentication-process",
        human_children=[
            HumanChildSpec(
                name="authenticate",
                task_kind="authenticate",
                substrate="browser",
                risk_class="consequential",
                required_authn="webauthn",
                authentication_template="authentication.json",
            )
        ],
    )
    key = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    execute_process_contract_v1(
        contract,
        parent_dir=tmp_path,
        run_dir=tmp_path / "run",
        inputs={},
        child_run=_no_flow,
        qualification_signers={},
        code_signers={},
        runtime_environment_digest=_digest(b"unused"),
        receipt_private_key=key,
        receipt_issuer_key_id="receipt-signer-0001",
        environment_id="environment-local-0001",
        runner_id="runner-local-0001",
        allow_trusted_code=False,
    )

    def wrong_surface_provider(child, task, authentication, request_dir):
        completion = _human_provider(child, task, authentication, request_dir)
        binding_raw = json.loads((request_dir / "run-binding.json").read_text())
        binding = AuthenticationRunBindingV1(
            **binding_raw,
            principal_binding_hmac="hmac-sha256:" + "1" * 64,
            session_binding_hmac="hmac-sha256:" + "2" * 64,
            operator_authority_digest=_digest(b"operator"),
            verifier_evidence_digest=_digest(b"evidence"),
            capture_exclusion_receipt_digest=_digest(b"excluded"),
        )
        now = datetime.now(timezone.utc)
        receipt = issue_authentication_receipt(
            AuthenticationReceiptPayloadV1(
                task_contract_digest=authentication.digest,
                task_id=authentication.task_id,
                human_decision_task_digest=task.digest,
                app_version_digest=binding.app_version_digest,
                process_execution_id=binding.process_execution_id,
                step_id=binding.step_id,
                challenge_digest=binding.challenge_digest,
                method="passkey",
                substrate="windows",
                principal_class="named_user",
                principal_binding_contract_digest=authentication.principal_binding_contract_digest,
                principal_binding_hmac=binding.principal_binding_hmac,
                session_binding_hmac=binding.session_binding_hmac,
                operator_authority_digest=binding.operator_authority_digest,
                authenticated_at=_timestamp(now),
                verified_at=_timestamp(now),
                fresh_until=_timestamp(now + timedelta(minutes=5)),
                user_presence_outcome="verified",
                mfa_outcome="verified",
                verifier_id=authentication.verifier_id,
                verifier_contract_digest=authentication.verifier_contract_digest,
                verifier_evidence_digest=binding.verifier_evidence_digest,
                capture_exclusion_receipt_digest=binding.capture_exclusion_receipt_digest,
                outcome="verified",
            )
        )
        return HumanCompletionV1(
            receipt=completion.receipt,
            authentication_binding=binding,
            authentication_receipt=receipt,
        )

    with pytest.raises(ValueError, match="substrate differs"):
        execute_process_contract_v1(
            contract,
            parent_dir=tmp_path,
            run_dir=tmp_path / "run",
            inputs={},
            child_run=_no_flow,
            qualification_signers={},
            code_signers={},
            runtime_environment_digest=_digest(b"unused"),
            receipt_private_key=key,
            receipt_issuer_key_id="receipt-signer-0001",
            environment_id="environment-local-0001",
            runner_id="runner-local-0001",
            allow_trusted_code=False,
            human_completion_provider=wrong_surface_provider,
        )


def test_v1_contract_orders_all_three_capability_kinds() -> None:
    contract = ProcessContract(
        schema_version="openadapt.process-contract/v1",
        name="three-kinds",
        children=[],
        code_children=[
            CodeChildSpec(
                name="prepare",
                manifest="prepare/manifest.json",
                admission="prepare/admission.json",
                source_archive="prepare/source.zip",
            )
        ],
        human_children=[
            HumanChildSpec(
                name="review",
                task_kind="review",
                substrate="mixed",
                risk_class="consequential",
                required_authn="aal2",
            )
        ],
        after={"review": ["prepare"]},
    )
    assert topological_order(contract) == ["prepare", "review"]


def test_v1_visualizer_keeps_capability_kinds_and_artifact_edges(
    tmp_path: Path,
) -> None:
    contract = ProcessContract(
        schema_version="openadapt.process-contract/v1",
        name="artifact-review",
        code_children=[
            CodeChildSpec(
                name="prepare",
                manifest="prepare/manifest.json",
                admission="prepare/admission.json",
                source_archive="prepare/source.zip",
            ),
            CodeChildSpec(
                name="verify",
                manifest="verify/manifest.json",
                admission="verify/admission.json",
                source_archive="verify/source.zip",
                role="verifier",
            ),
        ],
        human_children=[
            HumanChildSpec(
                name="review",
                task_kind="review",
                substrate="browser",
                risk_class="consequential",
                required_authn="aal2",
            )
        ],
        after={"verify": ["prepare"], "review": ["verify"]},
        artifact_edges=[
            ArtifactEdgeV1(
                from_child="prepare",
                from_output="return",
                to_child="review",
                to_input="document",
                verifier_child="verify",
            )
        ],
    )
    contract.save(tmp_path)

    graph = build_process_graph(tmp_path)

    assert [node.capability_type for node in graph.nodes[:-1]] == [
        "code",
        "code",
        "human",
    ]
    artifact = next(edge for edge in graph.edges if edge.kind == "artifact")
    assert artifact.source == "prepare"
    assert artifact.target == "review"
    assert "verified by verify" in artifact.label


def test_v1_public_schema_matches_the_model() -> None:
    root = Path(__file__).resolve().parents[1]
    expected = json.loads((root / "schemas/process-contract-v1.json").read_text())
    assert expected == ProcessContractV1.model_json_schema()


def test_v0_save_keeps_the_existing_wire_shape(tmp_path: Path) -> None:
    from tests.test_admitted_composition_authoring import _two_admitted

    intake, intake_envelope, posting, posting_envelope = _two_admitted(tmp_path)
    from openadapt_flow.admitted_composition import author_process_contract

    contract = author_process_contract(
        [
            ("intake", intake_envelope, intake),
            ("posting", posting_envelope, posting),
        ],
        out=tmp_path / "parent",
    )
    raw = json.loads((tmp_path / "parent/process-contract.json").read_text())
    assert contract.schema_version == "openadapt.process-contract/v0"
    assert "code_children" not in raw
    assert "human_children" not in raw
    assert "artifact_edges" not in raw


def test_v1_resume_refuses_an_altered_event(tmp_path: Path) -> None:
    contract = ProcessContract(
        schema_version="openadapt.process-contract/v1",
        name="human-process",
        human_children=[
            HumanChildSpec(
                name="review",
                task_kind="review",
                substrate="browser",
                risk_class="consequential",
                required_authn="aal2",
            )
        ],
    )
    key = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    execute_process_contract_v1(
        contract,
        parent_dir=tmp_path,
        run_dir=tmp_path / "run",
        inputs={},
        child_run=_no_flow,
        qualification_signers={},
        code_signers={},
        runtime_environment_digest=_digest(b"unused"),
        receipt_private_key=key,
        receipt_issuer_key_id="receipt-signer-0001",
        environment_id="environment-local-0001",
        runner_id="runner-local-0001",
        allow_trusted_code=False,
    )
    journal = tmp_path / "run/process-events.jsonl"
    journal.write_text(journal.read_text().replace("process_started", "changed"))
    with pytest.raises(ProcessV1Error, match="journal digest"):
        execute_process_contract_v1(
            contract,
            parent_dir=tmp_path,
            run_dir=tmp_path / "run",
            inputs={},
            child_run=_no_flow,
            qualification_signers={},
            code_signers={},
            runtime_environment_digest=_digest(b"unused"),
            receipt_private_key=key,
            receipt_issuer_key_id="receipt-signer-0001",
            environment_id="environment-local-0001",
            runner_id="runner-local-0001",
            allow_trusted_code=False,
        )
