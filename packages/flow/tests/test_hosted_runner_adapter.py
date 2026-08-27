from __future__ import annotations

import hashlib
import json
import os
from base64 import b64decode, b64encode, urlsafe_b64encode
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import openadapt_flow.runner.hosted_adapter as hosted
from openadapt_flow.__main__ import _replay_params
from openadapt_flow.ir import ParamKind, ParamSpec, Workflow
from openadapt_flow.runner.hosted_adapter import (
    RUNNER_RENEWAL_HEADER,
    AdmissionArtifactBytes,
    CallbackRequest,
    DeliveryAuthority,
    HostedDispatch,
    HostedRunnerAdapter,
    HostedRunResult,
    HostedTerminalEvent,
    ManagedExecution,
    PollRequest,
    RegisterCapabilities,
    RegisterRequest,
    registration_renewal_headers,
)
from openadapt_flow.runner.inputs import resolve_admitted_params
from openadapt_flow.runner.product_release import (
    DOMAIN,
    TARGETS,
    ProductReleaseAdmissionArtifact,
    ProductReleaseAdmissionError,
    ProductReleaseAdmissionPayload,
    ProductReleaseSignerTrust,
    verify_product_release_admission,
)
from openadapt_flow.runner.protocol import (
    DispatchParamsRef,
    DispatchParamsValues,
    RunnerDispatchPayload,
    dispatch_binding_sha256,
)
from openadapt_flow.runner.verify import VerifiedDispatch
from openadapt_flow.runtime.authorization import (
    runtime_inputs_bytes,
    runtime_param_text,
)
from openadapt_flow.runtime.durable.authority import REMOTE_DISPATCH_SESSION_ID_ENV
from openadapt_flow.terminal_verification_v2 import (
    ProductionDeliveryPermit,
    ProductionDeliveryPermitChain,
    ProductionDeliveryPermitPayload,
    ProductionDeliveryReceiptPayload,
    evidence_runner_signer_sha256,
    sign_production_delivery_permit,
    sign_production_delivery_receipt,
    sign_production_terminal_verification,
)
from openadapt_flow.transaction import TransactionOutcome
from tests.test_run_receipt import _report as _production_report
from tests.test_runner_client_lib import dispatch_payload
from tests.test_terminal_verification_v2 import _payload, _private_key

pytest_plugins = ("tests.test_runner_client_lib",)


def _release_payload() -> dict[str, object]:
    targets = []
    for index, target in enumerate(TARGETS, start=1):
        targets.append(
            {
                "target": target,
                "admission_id": f"00000000-0000-4000-8000-{index:012d}",
                "admission_sha256": f"{index:x}" * 64,
                "release_id": "1.2.3",
                "release_artifact_sha256": f"{index + 7:x}" * 64,
                "admission_issued_at": "2026-08-25T00:00:00Z",
                "admission_expires_at": "2026-08-28T00:00:00Z",
                "revoked_at": None,
                "artifact_authority_sha256": f"{index + 8:x}" * 64,
                "artifact_authority_state": "active",
                "artifact_authority_checked_at": "2026-08-25T00:00:00Z",
                "artifact_authority_expires_at": "2026-08-28T00:00:00Z",
            }
        )
    return {
        "schema_version": "openadapt.product-release-admission-payload/v1",
        "set_id": "00000000-0000-4000-8000-000000000099",
        "sequence": 7,
        "policy_sha256": "a" * 64,
        "issued_at": "2026-08-25T00:00:00Z",
        "expires_at": "2026-08-28T00:00:00Z",
        "targets": tuple(targets),
    }


def _release_artifact() -> tuple[
    ProductReleaseAdmissionArtifact, ProductReleaseSignerTrust
]:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    payload = ProductReleaseAdmissionPayload.model_validate(_release_payload())
    signature = private_key.sign(DOMAIN + payload.canonical_bytes())
    public_b64 = b64encode(public_key).decode("ascii")
    artifact = ProductReleaseAdmissionArtifact.model_validate(
        {
            "schema_version": "openadapt.product-release-admission-artifact/v1",
            "payload": payload,
            "payload_sha256": payload.payload_sha256_value(),
            "signer": {
                "algorithm": "ed25519",
                "key_id": (
                    "release-admission-ed25519-"
                    + hashlib.sha256(public_key).hexdigest()[:16]
                ),
                "public_key": public_b64,
            },
            "signature": urlsafe_b64encode(signature).decode("ascii").rstrip("="),
        }
    )
    return artifact, ProductReleaseSignerTrust(
        public_key=public_b64,
        status="active",
        revoked_at=None,
    )


@pytest.mark.parametrize("sequence", [True, "7"])
def test_product_release_sequence_refuses_scalar_coercion(sequence: object) -> None:
    raw = _release_payload()
    raw["sequence"] = sequence
    with pytest.raises(ValueError):
        ProductReleaseAdmissionPayload.model_validate(raw)


def test_product_release_refuses_noncanonical_utc() -> None:
    raw = _release_payload()
    raw["issued_at"] = "2026-08-25T00:00:00+00:00"
    with pytest.raises(ValueError, match="canonical UTC"):
        ProductReleaseAdmissionPayload.model_validate(raw)


def test_product_release_refuses_revoked_signer() -> None:
    artifact, trust = _release_artifact()
    revoked = trust.model_copy(
        update={"status": "revoked", "revoked_at": "2026-08-25T00:00:00Z"}
    )
    with pytest.raises(ProductReleaseAdmissionError, match="revoked"):
        verify_product_release_admission(
            artifact,
            trusted_signers={artifact.signer.key_id: revoked},
            newest_sequence=7,
            now=datetime(2026, 8, 26, tzinfo=timezone.utc),
        )


def _hosted_dispatch(workflow) -> HostedDispatch:
    workflow_id = "33333333-3333-4333-8333-333333333333"
    version_id = "44444444-4444-4444-8444-444444444444"
    payload_raw = dispatch_payload(
        workflow,
        workflow_id=workflow_id,
        bundle={
            "version_id": version_id,
            "content_digest": workflow.manifest.content_digest,
            "url": "https://invalid.example/never-fetched",
        },
    )
    payload = RunnerDispatchPayload.model_validate(payload_raw)
    artifact_raw = b"{}"
    artifact = AdmissionArtifactBytes(
        artifact_bytes_base64=b64encode(artifact_raw).decode("ascii"),
        artifact_sha256=hashlib.sha256(artifact_raw).hexdigest(),
    )
    return HostedDispatch(
        schema_version="openadapt.hosted-runner/v1",
        dispatch_id="11111111-1111-4111-8111-111111111111",
        dispatch_session_id="12111111-1111-4111-8111-111111111111",
        tenant_id="22222222-2222-4222-8222-222222222222",
        runner_id="55555555-5555-4555-8555-555555555555",
        runner_session_id="66666666-6666-4666-8666-666666666666",
        run_id=payload.run_id,
        workflow_id=workflow_id,
        workflow_version_id=version_id,
        idempotency_key="hosted-dispatch-0001",
        lease_token="oal_" + "a" * 64,
        lease_expires_at="2099-01-01T00:00:00Z",
        product_release_admission=artifact,
        workflow_admission=artifact,
        managed_delivery_authority_url=(
            "https://cloud.example/api/internal/managed-delivery-permit"
        ),
        delivery_authority_token="b" * 64,
        payload=payload,
    )


def test_hosted_dispatch_accepts_lowercase_v8_run_id(sealed) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    run_id = "018f6c0a-4cce-8f47-8d71-c3d63bf1c001"
    payload = dispatch.payload.model_copy(
        update={
            "run_id": run_id,
            "dispatch_binding_sha256": dispatch_binding_sha256(
                run_id, dispatch.payload.authorization
            ),
        }
    )

    parsed = HostedDispatch.model_validate(
        dispatch.model_dump(mode="python") | {"run_id": run_id, "payload": payload}
    )

    assert parsed.run_id == run_id


def test_registration_refuses_without_protected_runner_origin(
    monkeypatch, tmp_path, config
) -> None:
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    monkeypatch.setattr(hosted, "load_runner_config", lambda *_args, **_kwargs: config)

    with pytest.raises(ValueError, match="protected runner host"):
        adapter.registration_request(
            runner_config=tmp_path / "runner.toml",
            name="runner",
            platform="linux",
            agent_version="1.0.0",
            engine_version="1.33.0",
            mode="service",
            capabilities=RegisterCapabilities(
                backends=("linux",),
                attended=False,
                effects_substrates=("linux",),
            ),
        )


def test_protected_runner_origin_is_public_strict_accessor(
    monkeypatch, tmp_path, config
) -> None:
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    local_config = replace(config, host="https://cloud.example")
    monkeypatch.setattr(
        hosted, "load_runner_config", lambda *_args, **_kwargs: local_config
    )

    assert adapter.protected_runner_origin(tmp_path / "runner.toml") == (
        "https://cloud.example"
    )


def test_runner_renewal_token_has_one_register_only_header_boundary() -> None:
    token = "oar_" + "a" * 64

    assert registration_renewal_headers(None) == {}
    assert registration_renewal_headers("") == {}
    assert registration_renewal_headers(token) == {
        RUNNER_RENEWAL_HEADER: token,
    }
    for invalid in ("runner-token", "oar_" + "A" * 64, "oar_" + "a" * 63):
        with pytest.raises(ValueError, match="renewal credential"):
            registration_renewal_headers(invalid)

    for request_type in (RegisterRequest, PollRequest, HostedDispatch, CallbackRequest):
        assert "runner_token" not in request_type.model_fields
        assert RUNNER_RENEWAL_HEADER not in request_type.model_fields
        assert all("renewal" not in name for name in request_type.model_fields)


def test_dispatch_param_scalars_round_trip_without_coercion() -> None:
    values = {
        "text": "1.5",
        "enabled": True,
        "count": 7,
        "ratio": 1.5,
        "small": 1e-7,
        "large": 1e21,
    }

    parsed = DispatchParamsValues.model_validate({"values": values})

    assert parsed.model_dump(mode="json")["values"] == values
    assert type(parsed.values["enabled"]) is bool
    assert type(parsed.values["count"]) is int
    assert type(parsed.values["ratio"]) is float


@pytest.mark.parametrize(
    "value",
    [None, {}, [], float("nan"), float("inf"), 9_007_199_254_740_993],
)
def test_dispatch_param_scalars_refuse_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        DispatchParamsValues.model_validate({"values": {"value": value}})


@pytest.mark.parametrize(
    "name",
    ["\ue000", "\U0001f600", "has-dash", "a" * 129],
)
def test_dispatch_param_names_use_shared_ascii_grammar(name: str) -> None:
    with pytest.raises(ValueError, match="parameter name"):
        DispatchParamsValues.model_validate({"values": {name: "value"}})


def test_private_params_file_preserves_scalar_types(tmp_path) -> None:
    values = {"text": "false", "enabled": False, "count": 0, "ratio": 1.5}

    path = HostedRunnerAdapter._write_params(tmp_path / "params.json", values)

    assert path is not None
    assert json.loads(path.read_bytes()) == values
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_scalar_dispatch_to_gui_boundary_preserves_exact_types(tmp_path) -> None:
    values = {
        "string_int": "7",
        "string_float": "1.5",
        "string_bool": "true",
        "bool_true": True,
        "bool_false": False,
        "whole": 7,
        "float": 1.5,
        "small": 1e-7,
        "mid": 1e20,
        "large": 1e21,
    }
    kinds = {
        "string_int": ParamKind.STRING,
        "string_float": ParamKind.STRING,
        "string_bool": ParamKind.STRING,
        "bool_true": ParamKind.BOOLEAN,
        "bool_false": ParamKind.BOOLEAN,
        "whole": ParamKind.NUMBER,
        "float": ParamKind.NUMBER,
        "small": ParamKind.NUMBER,
        "mid": ParamKind.NUMBER,
        "large": ParamKind.NUMBER,
    }
    workflow = Workflow(
        name="scalar-path",
        param_specs={
            name: ParamSpec(name=name, type=kind, required=True)
            for name, kind in kinds.items()
        },
    )

    wire = DispatchParamsValues.model_validate({"values": values})
    admitted = resolve_admitted_params(workflow, dict(wire.values), inline=True)
    expected = (
        b'{"params":{"bool_false":false,"bool_true":true,"float":1.5,'
        b'"large":1e+21,"mid":100000000000000000000,"small":1e-7,'
        b'"string_bool":"true","string_float":"1.5","string_int":"7",'
        b'"whole":7},"worklists":{}}'
    )
    params_path = HostedRunnerAdapter._write_params(tmp_path / "params.json", admitted)
    assert params_path is not None
    child_params = _replay_params(None, str(params_path))

    assert runtime_inputs_bytes(workflow, admitted, None) == expected
    assert child_params == values
    assert {name: type(value) for name, value in child_params.items()} == {
        name: type(value) for name, value in values.items()
    }
    assert {
        name: runtime_param_text(value) for name, value in child_params.items()
    } == {
        "string_int": "7",
        "string_float": "1.5",
        "string_bool": "true",
        "bool_true": "true",
        "bool_false": "false",
        "whole": "7",
        "float": "1.5",
        "small": "1e-7",
        "mid": "100000000000000000000",
        "large": "1e+21",
    }


def _prepared_adapter(monkeypatch, tmp_path, config, workflow, runner):
    config = replace(config, host="https://cloud.example")
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite", runner=runner)
    dispatch = _hosted_dispatch(workflow)
    verified = VerifiedDispatch(
        payload=dispatch.payload,
        bundle=config.bundles[workflow.manifest.content_digest],
        profile_path=config.profiles["default"],
        params={"visit_date": "2026-07-01"},
        workflow=workflow,
        consequential_steps=1,
        effect_covered_consequential_steps=1,
    )
    monkeypatch.setattr(hosted, "load_runner_config", lambda _, **__: config)
    monkeypatch.setattr(adapter, "_verify_product_release", lambda *_: None)
    monkeypatch.setattr(adapter, "_load_evidence_private_key", lambda *_: object())
    monkeypatch.setattr(
        adapter,
        "_verify_workflow_admission",
        lambda *_, **__: ({}, b"runtime:\n  durable: false\n"),
    )
    monkeypatch.setattr(adapter, "_resolve_params", lambda *_: verified.params)
    monkeypatch.setattr(
        hosted,
        "verify_dispatch",
        lambda _payload, staged_config, **_kwargs: replace(
            verified,
            profile_path=staged_config.profiles[dispatch.payload.deployment_profile_id],
        ),
    )

    class Guard:
        def __init__(self, *_args, **_kwargs):
            pass

        def authorization_binding(self, _workflow):
            return {}

    monkeypatch.setattr(hosted, "ProductionQualificationGuard", Guard)
    return adapter, dispatch


def _terminal_delivery_chain(
    dispatch: HostedDispatch,
    *,
    admission_sha256: str,
    evidence_identity_sha256: str,
    environment_digest: str,
    registry_sha256: str,
) -> ProductionDeliveryPermitChain:
    authority_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))
    permit_payload = ProductionDeliveryPermitPayload(
        execution_authority_id="00000000-0000-4000-8000-000000000008",
        execution_authority_sha256="1" * 64,
        permit_id="permit:hosted:1",
        run_id=dispatch.run_id,
        flow_run_id_sha256=hashlib.sha256(dispatch.run_id.encode("utf-8")).hexdigest(),
        run_request_sha256="3" * 64,
        action_request_sha256="4" * 64,
        admission_artifact_sha256=admission_sha256,
        evidence_identity_sha256=evidence_identity_sha256,
        environment_digest=environment_digest,
        qualification_signer_registry_sha256=registry_sha256,
        qualification_signer_registry_revision=7,
        qualification_signer_registry_checked_at="2026-08-26T11:59:30Z",
        qualification_signer_registry_expires_at="2026-08-28T12:00:00Z",
        input_edge_sequence=1,
        authority_sequence=0,
        issued_at="2026-08-26T12:00:00Z",
    )
    permit = sign_production_delivery_permit(permit_payload, authority_key)
    receipt_payload = ProductionDeliveryReceiptPayload(
        execution_authority_id=permit_payload.execution_authority_id,
        permit_id=permit_payload.permit_id,
        permit_artifact_sha256=permit.artifact_sha256(),
        authenticated_runner_id_sha256=hashlib.sha256(
            dispatch.runner_id.encode("utf-8")
        ).hexdigest(),
        authenticated_session_id_sha256=hashlib.sha256(
            dispatch.runner_session_id.encode("utf-8")
        ).hexdigest(),
        one_use_claim_id="00000000-0000-4000-8000-000000000010",
        runtime_delivery_sequence=9,
        delivered_at="2026-08-26T12:00:01Z",
    )
    receipt = sign_production_delivery_receipt(receipt_payload, authority_key)
    return ProductionDeliveryPermitChain.build(
        (ProductionDeliveryPermit.build(permit, receipt),)
    )


def test_outer_adapter_builds_stores_rereads_and_verifies_terminal_v2(
    monkeypatch, tmp_path, sealed
) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    verified_params = dict(dispatch.payload.params.values)
    report = _production_report(
        run_id_sha256=hashlib.sha256(dispatch.run_id.encode("utf-8")).hexdigest(),
        bundle_content_digest=dispatch.payload.bundle.content_digest,
        params=verified_params,
    )
    authorization = dispatch.payload.authorization.model_copy(
        update={
            "admitted_policy_name": report.governed_policy_name
            or dispatch.payload.authorization.admitted_policy_name,
            "admitted_policy_contract_sha256": (report.governed_policy_contract_sha256),
            "execution_profile": report.execution_profile,
            "minimum_effect_tier": report.governed_minimum_effect_tier,
            "qualified_effect_requirements": tuple(
                report.governed_qualified_effect_requirements
            ),
            "required_identity_step_ids": tuple(report.required_identity_step_ids),
            "approval_source": report.governed_approval_source,
        }
    )
    payload = dispatch.payload.model_copy(
        update={
            "authorization": authorization,
            "dispatch_binding_sha256": dispatch_binding_sha256(
                dispatch.run_id, authorization
            ),
        }
    )
    dispatch = dispatch.model_copy(update={"payload": payload})
    report = report.model_copy(
        update={
            "governed_authorization_id": authorization.authorization_id,
            "governed_authorization_created_at": authorization.created_at,
            "governed_approval_source": authorization.approval_source,
            "governed_policy_name": authorization.admitted_policy_name,
            "governed_runtime_inputs_digest": authorization.runtime_inputs_digest,
        }
    )
    private_key = _private_key()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    admission_sha256 = "5" * 64
    evidence_identity_sha256 = "6" * 64
    environment_digest = "7" * 64
    registry_sha256 = "8" * 64
    qualification_sha256 = "f" * 64
    evidence_identity = SimpleNamespace(
        admission_policy_sha256="9" * 64,
        artifact_sha256=lambda: evidence_identity_sha256,
    )
    runtime = SimpleNamespace(
        substrate="web",
        artifact_sha256=lambda: "0" * 64,
    )
    expected = SimpleNamespace(
        bundle_artifact_sha256="b" * 64,
        bundle_content_digest=dispatch.payload.bundle.content_digest,
        environment_digest=environment_digest,
        environment_contract_sha256="a" * 64,
        runtime_environment_sha256="b" * 64,
        identity_contract_sha256="c" * 64,
        effect_contract_sha256="d" * 64,
        runtime_validation_id="00000000-0000-4000-8000-000000000006",
        runtime_build_identity=runtime,
        evidence_runner_signer_sha256=evidence_runner_signer_sha256(public_key),
    )
    admission = SimpleNamespace(
        payload=SimpleNamespace(
            admission_id="00000000-0000-4000-8000-000000000001",
            evidence_identity=evidence_identity,
        )
    )
    qualification = SimpleNamespace(
        qualification_admission_sha256=admission_sha256,
        qualification_admission=admission,
        expected=expected,
        qualification_signer_registry_sha256=registry_sha256,
        qualification_signer_registry=SimpleNamespace(
            revision=7,
            expires_at="2026-08-28T12:00:00Z",
        ),
        immutable_binding_sha256=lambda: qualification_sha256,
    )
    chain = _terminal_delivery_chain(
        dispatch,
        admission_sha256=admission_sha256,
        evidence_identity_sha256=evidence_identity_sha256,
        environment_digest=environment_digest,
        registry_sha256=registry_sha256,
    )
    binding = dispatch.payload.dispatch_binding_sha256
    local_authorization = authorization.model_copy(
        update={
            "production_qualification_admission_id": (admission.payload.admission_id),
            "production_qualification_admission_sha256": admission_sha256,
            "production_qualification_evidence_identity_sha256": (
                evidence_identity_sha256
            ),
            "production_qualification_runtime_validation_id": (
                expected.runtime_validation_id
            ),
            "production_qualification_signer_registry_sha256": registry_sha256,
            "production_qualification_signer_registry_revision": 7,
            "production_qualification_signer_registry_expires_at": (
                "2026-08-28T12:00:00Z"
            ),
            "production_qualification_authority_sha256": qualification_sha256,
        }
    )
    manifest = SimpleNamespace(
        delivery_authority_kind="cloud_runner",
        remote_delivery_run_id=dispatch.run_id,
        managed_dispatch_binding_sha256=binding,
        params=verified_params,
        governed_authorization=local_authorization,
    )

    class Store:
        def __init__(self, _run_dir):
            pass

        def read_manifest(self):
            return manifest

    class Authority:
        def __init__(self, _run_dir, _store):
            pass

        def production_delivery_permit_chain(self):
            return chain

    fixed_now = datetime(2026, 8, 26, 12, 0, 2, tzinfo=timezone.utc)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(hosted, "CheckpointStore", Store)
    monkeypatch.setattr(hosted, "DurableAuthority", Authority)
    monkeypatch.setattr(hosted, "datetime", FixedDatetime)
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")

    mismatched_dir = tmp_path / "mismatched-run"
    mismatched_dir.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="run report differs"):
        adapter._produce_terminal_verification(
            dispatch=dispatch,
            report=report.model_copy(update={"params": {"visit_date": "wrong"}}),
            run_dir=mismatched_dir,
            qualification=qualification,
            private_key=private_key,
            verified_params=verified_params,
            dispatch_binding_sha256=binding,
        )
    assert not (mismatched_dir / "production-terminal-report.json").exists()
    assert not (mismatched_dir / "production-terminal-verification.json").exists()

    storage_failure_dir = tmp_path / "storage-failure-run"
    storage_failure_dir.mkdir(mode=0o700)
    original_read = adapter._read_private_bytes

    def fail_proof_reread(path, *, maximum_bytes, label):
        if label == "production terminal verification":
            raise OSError("simulated protected storage failure")
        return original_read(path, maximum_bytes=maximum_bytes, label=label)

    monkeypatch.setattr(adapter, "_read_private_bytes", fail_proof_reread)
    with pytest.raises(OSError, match="storage failure"):
        adapter._produce_terminal_verification(
            dispatch=dispatch,
            report=report,
            run_dir=storage_failure_dir,
            qualification=qualification,
            private_key=private_key,
            verified_params=verified_params,
            dispatch_binding_sha256=binding,
        )
    assert not (storage_failure_dir / "production-terminal-report.json").exists()
    assert not (storage_failure_dir / "production-terminal-verification.json").exists()
    monkeypatch.setattr(adapter, "_read_private_bytes", original_read)

    proof, report_sha256 = adapter._produce_terminal_verification(
        dispatch=dispatch,
        report=report,
        run_dir=run_dir,
        qualification=qualification,
        private_key=private_key,
        verified_params=verified_params,
        dispatch_binding_sha256=binding,
    )

    report_path = run_dir / "production-terminal-report.json"
    proof_path = run_dir / "production-terminal-verification.json"
    assert hashlib.sha256(report_path.read_bytes()).hexdigest() == report_sha256
    assert proof_path.read_bytes() == hosted.canonical_json(proof)
    assert hashlib.sha256(proof_path.read_bytes()).hexdigest() == (
        proof.artifact_sha256()
    )
    if os.name != "nt":
        assert report_path.stat().st_mode & 0o777 == 0o600
        assert proof_path.stat().st_mode & 0o777 == 0o600
    assert "2026-07-01" in report_path.read_text(encoding="utf-8")
    assert "2026-07-01" not in proof_path.read_text(encoding="utf-8")


def test_terminal_admission_is_revalidated_after_child_execution(
    monkeypatch, tmp_path, config, sealed
) -> None:
    workflow, _ = sealed
    report = _production_report()
    calls = 0

    def runner(_argv, _run_dir, _child_env):
        nonlocal calls
        calls += 1
        return ManagedExecution(
            returncode=0,
            report_bytes=report.model_dump_json().encode(),
        )

    adapter, dispatch = _prepared_adapter(
        monkeypatch, tmp_path, config, workflow, runner
    )
    release_checks = 0

    def verify_release(*_args):
        nonlocal release_checks
        release_checks += 1
        if release_checks == 2:
            raise ValueError("release admission was revoked during execution")

    monkeypatch.setattr(adapter, "_verify_product_release", verify_release)
    monkeypatch.setattr(
        adapter,
        "_produce_terminal_verification",
        lambda **_kwargs: pytest.fail("revoked run must not produce terminal proof"),
    )
    authority = DeliveryAuthority(
        dispatch.managed_delivery_authority_url,
        dispatch.delivery_authority_token,
    )

    first = adapter.execute(
        dispatch,
        runner_config=tmp_path / "runner.toml",
        run_dir=tmp_path / "run",
        authority=authority,
    )

    assert first.outcome is TransactionOutcome.RECONCILIATION_REQUIRED
    assert first.started is True
    assert first.terminal_verification is None
    assert calls == 1
    assert release_checks == 2


@pytest.mark.parametrize(
    ("fault", "execution"),
    [
        (
            "backend_response_lost",
            RuntimeError("backend response lost after possible actuation"),
        ),
        (
            "delivery_acknowledgment_response_lost",
            RuntimeError("delivery acknowledgment response lost after backend call"),
        ),
        (
            "receipt_unavailable",
            ManagedExecution(returncode=1, report_bytes=None),
        ),
        (
            "malformed_terminal_report",
            ManagedExecution(returncode=0, report_bytes=b"not-json"),
        ),
    ],
    ids=(
        "backend-response-lost",
        "delivery-acknowledgment-response-lost",
        "receipt-unavailable",
        "malformed-terminal-report",
    ),
)
def test_hosted_uncertain_delivery_fault_never_replays(
    monkeypatch, tmp_path, config, sealed, fault, execution
) -> None:
    workflow, _ = sealed
    calls = 0
    seen_child_env = None
    seen_argv = None

    def runner(argv, _run_dir, child_env):
        nonlocal calls
        nonlocal seen_child_env
        nonlocal seen_argv
        calls += 1
        seen_child_env = child_env
        seen_argv = argv
        if isinstance(execution, Exception):
            raise execution
        return execution

    adapter, dispatch = _prepared_adapter(
        monkeypatch, tmp_path, config, workflow, runner
    )
    authority = DeliveryAuthority(
        dispatch.managed_delivery_authority_url,
        dispatch.delivery_authority_token,
    )
    first = adapter.execute(
        dispatch,
        runner_config=tmp_path / "runner.toml",
        run_dir=tmp_path / "run",
        authority=authority,
    )
    second = adapter.execute(
        dispatch,
        runner_config=tmp_path / "runner.toml",
        run_dir=tmp_path / "run-again",
        authority=authority,
    )

    assert first.outcome is TransactionOutcome.RECONCILIATION_REQUIRED
    assert first.started is True
    assert first.uncertain_delivery is True
    assert second.outcome is TransactionOutcome.RECONCILIATION_REQUIRED
    assert second.started is True
    assert second.uncertain_delivery is True
    assert calls == 1
    assert seen_child_env[REMOTE_DISPATCH_SESSION_ID_ENV] == (
        dispatch.dispatch_session_id
    )
    profile_path = Path(seen_argv[seen_argv.index("--config") + 1])
    assert profile_path == tmp_path / "run" / "deployment.yaml"
    assert profile_path.read_bytes() == b"runtime:\n  durable: false\n"
    if os.name != "nt":
        assert profile_path.stat().st_mode & 0o777 == 0o600
    assert fault in {
        "backend_response_lost",
        "delivery_acknowledgment_response_lost",
        "receipt_unavailable",
        "malformed_terminal_report",
    }


def test_params_reference_resolves_only_from_protected_local_root(
    monkeypatch, tmp_path, config, sealed
) -> None:
    workflow, _ = sealed
    workflow.param_specs = {
        "visit_date": ParamSpec(
            name="visit_date",
            type=ParamKind.DATE,
            required=True,
        )
    }
    dispatch = _hosted_dispatch(workflow)
    expected_digest = dispatch.payload.authorization.runtime_inputs_digest
    dispatch = dispatch.model_copy(
        update={
            "payload": dispatch.payload.model_copy(
                update={
                    "params": DispatchParamsRef(
                        ref="records/run.json",
                        expected_digest=expected_digest,
                    )
                }
            )
        }
    )
    root = tmp_path / "params"
    nested = root / "records"
    nested.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    nested.chmod(0o700)
    ref_file = nested / "run.json"
    ref_file.write_text(json.dumps({"visit_date": "2026-07-01"}), encoding="utf-8")
    ref_file.chmod(0o600)
    local_config = replace(
        config,
        host="https://cloud.example",
        params_ref_root=root,
    )
    monkeypatch.setattr(hosted.Workflow, "load", lambda *_: workflow)

    adapter = HostedRunnerAdapter(tmp_path / "resolver-ledger.sqlite")
    assert adapter._resolve_params(dispatch, local_config) == {
        "visit_date": "2026-07-01"
    }

    traversing = dispatch.model_copy(
        update={
            "payload": dispatch.payload.model_copy(
                update={
                    "params": DispatchParamsRef(
                        ref="../outside.json",
                        expected_digest=expected_digest,
                    )
                }
            )
        }
    )
    with pytest.raises(ValueError, match="safe local path"):
        adapter._resolve_params(traversing, local_config)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink contract")
def test_params_reference_refuses_symlink(
    monkeypatch, tmp_path, config, sealed
) -> None:
    workflow, _ = sealed
    workflow.param_specs = {
        "visit_date": ParamSpec(name="visit_date", type=ParamKind.DATE)
    }
    dispatch = _hosted_dispatch(workflow)
    expected_digest = dispatch.payload.authorization.runtime_inputs_digest
    root = tmp_path / "params"
    root.mkdir(mode=0o700)
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"visit_date": "2026-07-01"}), encoding="utf-8")
    target.chmod(0o600)
    (root / "run.json").symlink_to(target)
    dispatch = dispatch.model_copy(
        update={
            "payload": dispatch.payload.model_copy(
                update={
                    "params": DispatchParamsRef(
                        ref="run.json",
                        expected_digest=expected_digest,
                    )
                }
            )
        }
    )
    monkeypatch.setattr(hosted.Workflow, "load", lambda *_: workflow)
    adapter = HostedRunnerAdapter(tmp_path / "resolver-ledger.sqlite")

    with pytest.raises(ValueError, match="private regular file"):
        adapter._resolve_params(dispatch, replace(config, params_ref_root=root))


def test_params_reference_digest_mismatch_refuses_before_managed_runner(
    monkeypatch, tmp_path, config, sealed
) -> None:
    workflow, _ = sealed
    workflow.param_specs = {
        "visit_date": ParamSpec(
            name="visit_date",
            type=ParamKind.DATE,
            required=True,
        )
    }
    dispatch = _hosted_dispatch(workflow)
    root = tmp_path / "params"
    root.mkdir(mode=0o700)
    ref_file = root / "run.json"
    ref_file.write_text(json.dumps({"visit_date": "2026-07-01"}), encoding="utf-8")
    ref_file.chmod(0o600)
    dispatch = dispatch.model_copy(
        update={
            "payload": dispatch.payload.model_copy(
                update={
                    "params": DispatchParamsRef(
                        ref="run.json",
                        expected_digest="f" * 64,
                    )
                }
            )
        }
    )
    local_config = replace(
        config,
        host="https://cloud.example",
        params_ref_root=root,
    )
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("managed runner must not start")

    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite", runner=runner)
    monkeypatch.setattr(
        hosted, "load_runner_config", lambda *_args, **_kwargs: local_config
    )
    monkeypatch.setattr(adapter, "_verify_product_release", lambda *_: None)
    monkeypatch.setattr(adapter, "_load_evidence_private_key", lambda *_: object())
    monkeypatch.setattr(
        adapter,
        "_verify_workflow_admission",
        lambda *_, **__: ({}, b"runtime:\n  durable: false\n"),
    )
    monkeypatch.setattr(hosted.Workflow, "load", lambda *_: workflow)
    authority = DeliveryAuthority(
        dispatch.managed_delivery_authority_url,
        dispatch.delivery_authority_token,
    )

    result = adapter.execute(
        dispatch,
        runner_config=tmp_path / "runner.toml",
        run_dir=tmp_path / "run",
        authority=authority,
    )

    assert result.outcome == "REJECTED_POLICY"
    assert result.code == "runtime_inputs_mismatch"
    assert result.started is False
    assert result.uncertain_delivery is False
    assert calls == 0


@pytest.mark.parametrize("manifest_kind", ["missing", "malformed", "public", "symlink"])
def test_untrusted_runner_manifest_refuses_before_managed_runner(
    tmp_path, sealed, manifest_kind
) -> None:
    if manifest_kind == "symlink" and os.name == "nt":
        pytest.skip("POSIX symlink contract")
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    manifest = tmp_path / "runner.toml"
    if manifest_kind == "malformed":
        manifest.write_text("[runner\n", encoding="utf-8")
        manifest.chmod(0o600)
    elif manifest_kind == "public":
        manifest.write_text("[runner]\nname = 'runner'\n", encoding="utf-8")
        manifest.chmod(0o644)
    elif manifest_kind == "symlink":
        target = tmp_path / "target.toml"
        target.write_text("[runner]\nname = 'runner'\n", encoding="utf-8")
        target.chmod(0o600)
        manifest.symlink_to(target)
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("managed runner must not start")

    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite", runner=runner)
    authority = DeliveryAuthority(
        dispatch.managed_delivery_authority_url,
        dispatch.delivery_authority_token,
    )

    result = adapter.execute(
        dispatch,
        runner_config=manifest,
        run_dir=tmp_path / "run",
        authority=authority,
    )

    assert result.outcome == "REJECTED_POLICY"
    assert result.code == "hosted_admission_refused"
    assert result.detail.startswith("prestart_")
    assert result.started is False
    assert result.uncertain_delivery is False
    assert calls == 0


@pytest.mark.parametrize(
    "runner_host",
    [
        None,
        "http://cloud.example",
        "https://cloud.example/",
        "https://different.example",
    ],
)
def test_protected_runner_host_binds_delivery_authority_origin(
    monkeypatch, tmp_path, config, sealed, runner_host
) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("managed runner must not start")

    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite", runner=runner)
    local_config = replace(config, host=runner_host)
    monkeypatch.setattr(
        hosted, "load_runner_config", lambda *_args, **_kwargs: local_config
    )
    authority = DeliveryAuthority(
        dispatch.managed_delivery_authority_url,
        dispatch.delivery_authority_token,
    )

    result = adapter.execute(
        dispatch,
        runner_config=tmp_path / "runner.toml",
        run_dir=tmp_path / "run",
        authority=authority,
    )

    assert result.outcome == "REJECTED_POLICY"
    assert result.started is False
    assert result.detail == "prestart_ValueError"
    assert calls == 0


def test_protected_profile_mutation_refuses_before_managed_runner(
    monkeypatch, tmp_path, config, sealed
) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    config = replace(config, host="https://cloud.example")
    profile_path = config.profiles["default"]
    profile_path.chmod(0o600)
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("managed runner must not start")

    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite", runner=runner)
    monkeypatch.setattr(hosted, "load_runner_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(adapter, "_verify_product_release", lambda *_: None)
    monkeypatch.setattr(adapter, "_load_evidence_private_key", lambda *_: object())

    def verify_profile(*_args, **_kwargs):
        raw = adapter._read_private_bytes(
            profile_path,
            maximum_bytes=1024 * 1024,
            label="hosted deployment profile",
        )
        return {}, raw

    monkeypatch.setattr(adapter, "_verify_workflow_admission", verify_profile)
    real_read = os.read
    changed = False

    def mutating_read(descriptor, count):
        nonlocal changed
        chunk = real_read(descriptor, count)
        if not changed:
            changed = True
            profile_path.write_bytes(chunk + b"# changed\n")
            profile_path.chmod(0o600)
        return chunk

    monkeypatch.setattr(hosted.os, "read", mutating_read)
    authority = DeliveryAuthority(
        dispatch.managed_delivery_authority_url,
        dispatch.delivery_authority_token,
    )

    result = adapter.execute(
        dispatch,
        runner_config=tmp_path / "runner.toml",
        run_dir=tmp_path / "run",
        authority=authority,
    )

    assert result.outcome == "REJECTED_POLICY"
    assert result.started is False
    assert result.detail == "prestart_ValueError"
    assert calls == 0


def test_parsed_refusal_callback_contains_closed_terminal(tmp_path, sealed) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    refusal = adapter._refusal(dispatch, "hosted_admission_refused", "refused")

    callback = adapter.callback_request(dispatch, refusal)

    terminal = callback.events[-1]
    assert terminal["schema_version"] == "openadapt.hosted-runner-terminal/v1"
    assert terminal["outcome"] == "REJECTED_POLICY"
    assert terminal["started"] is False
    assert terminal["uncertain_delivery"] is False


def test_recovery_callback_retains_exact_terminal_v2_envelope(tmp_path, sealed) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    proof = sign_production_terminal_verification(_payload(), _private_key())
    binding = adapter.recovery_binding(dispatch).model_copy(
        update={"run_id": proof.payload.run_id}
    )
    result = HostedRunResult(
        dispatch_id=dispatch.dispatch_id,
        run_id=binding.run_id,
        outcome=TransactionOutcome.VERIFIED,
        evidence_batch=(),
        terminal_verification=proof,
        started=True,
        uncertain_delivery=False,
        report_sha256=proof.payload.run_report_sha256,
    )

    callback = adapter.callback_request(binding, result)
    terminal = HostedTerminalEvent.model_validate(callback.events[-1])
    assert terminal.terminal_verification_artifact_bytes_base64 is not None
    raw = b64decode(terminal.terminal_verification_artifact_bytes_base64, validate=True)
    assert hashlib.sha256(raw).hexdigest() == (
        terminal.terminal_verification_artifact_sha256
    )
    decoded = json.loads(raw)
    assert decoded["payload"]["schema_version"] == (
        "openadapt.production-terminal-verification/v2"
    )
    assert "params" not in decoded["payload"]
    assert "report" not in decoded["payload"]
    assert callback.runner_session_id == dispatch.runner_session_id
    assert callback.workflow_admission_sha256 == (
        dispatch.workflow_admission.artifact_sha256
    )


def test_callback_refuses_terminal_proof_for_a_different_run(tmp_path, sealed) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    proof = sign_production_terminal_verification(_payload(), _private_key())
    result = HostedRunResult.model_construct(
        dispatch_id=dispatch.dispatch_id,
        run_id=dispatch.run_id,
        outcome=TransactionOutcome.VERIFIED,
        evidence_batch=(),
        terminal_verification=proof,
        started=True,
        uncertain_delivery=False,
        report_sha256=proof.payload.run_report_sha256,
    )

    with pytest.raises(ValueError, match="different run report"):
        adapter.callback_request(adapter.recovery_binding(dispatch), result)
