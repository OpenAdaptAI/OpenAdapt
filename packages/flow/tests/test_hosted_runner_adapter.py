from __future__ import annotations

import hashlib
import json
import os
from base64 import b64decode, b64encode, urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

import openadapt_flow.runner.hosted_adapter as hosted
from openadapt_flow.__main__ import _replay_params
from openadapt_flow.ir import ParamKind, ParamSpec, Workflow
from openadapt_flow.runner.config import LocalRuntimeRelease
from openadapt_flow.runner.flow_release_receipt import (
    FlowReleaseVerificationReceiptArtifactBytes,
    HostedFlowReleaseIdentity,
)
from openadapt_flow.runner.hosted_adapter import (
    RUNNER_RENEWAL_HEADER,
    AdmissionArtifactBytes,
    CallbackRequest,
    CallbackRequestV1,
    CallbackRequestV2,
    CallbackResponseV1,
    CallbackResponseV2,
    DeliveryAuthority,
    HostedDispatch,
    HostedDispatchV1,
    HostedDispatchV2,
    HostedRecoveryBindingV1,
    HostedRecoveryBindingV2,
    HostedRunnerAdapter,
    HostedRunResult,
    HostedTerminalEvent,
    HostedTerminalEventV2,
    LocalRuntimeReleaseBinding,
    ManagedExecution,
    PollRequest,
    RegisterCapabilities,
    RegisterRequest,
    RegisterRequestV1,
    RegisterRequestV2,
    RegisterResponseV1,
    RegisterResponseV2,
    parse_callback_request,
    parse_callback_response,
    parse_hosted_dispatch,
    parse_hosted_terminal_event,
    parse_register_request,
    parse_register_response,
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
    ProductionDeliveryResultLossClosurePayload,
    ProductionPendingDeliveryPermit,
    delivery_authority_signer_sha256,
    evidence_runner_signer_sha256,
    sign_production_delivery_permit,
    sign_production_delivery_receipt,
    sign_production_delivery_result_loss_closure,
    sign_production_terminal_verification,
)
from openadapt_flow.transaction import TransactionOutcome
from tests.test_run_receipt import _report as _production_report
from tests.test_runner_client_lib import dispatch_payload
from tests.test_terminal_verification_v2 import (
    IDS,
    _acknowledged_reconciliation_payload,
    _halted_payload,
    _managed_result_loss_acknowledged_payload,
    _managed_result_loss_payload,
    _payload,
    _private_key,
    _reconciliation_payload,
)

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
    flow_receipt_raw = (
        Path(__file__).parent
        / "fixtures"
        / "remote-safe-synthetic-flow-release-verification.json"
    ).read_bytes()
    flow_receipt = FlowReleaseVerificationReceiptArtifactBytes(
        artifact_bytes_base64=b64encode(flow_receipt_raw).decode("ascii"),
        artifact_sha256="sha256:" + hashlib.sha256(flow_receipt_raw).hexdigest(),
    )
    authority_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))
    authority_public_key = authority_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return HostedDispatch(
        schema_version="openadapt.hosted-runner/v2",
        dispatch_id="11111111-1111-4111-8111-111111111111",
        dispatch_session_id="12111111-1111-4111-8111-111111111111",
        tenant_id="22222222-2222-4222-8222-222222222222",
        runner_id="55555555-5555-4555-8555-555555555555",
        runner_session_id="66666666-6666-4666-8666-666666666666",
        run_id=payload.run_id,
        workflow_id=workflow_id,
        workflow_version_id=version_id,
        execution_authority_id="00000000-0000-4000-8000-000000000008",
        execution_authority_sha256="1" * 64,
        execution_authority_signer_sha256=delivery_authority_signer_sha256(
            authority_public_key
        ),
        idempotency_key="hosted-dispatch-0001",
        lease_token="oal_" + "a" * 64,
        lease_expires_at="2099-01-01T00:00:00Z",
        flow_release_verification_receipt=flow_receipt,
        product_release_admission=artifact,
        workflow_admission=artifact,
        managed_delivery_authority_url=(
            "https://cloud.example/api/internal/managed-delivery-permit"
        ),
        delivery_authority_token="b" * 64,
        payload=payload,
    )


def _local_flow_release() -> HostedFlowReleaseIdentity:
    raw = (
        Path(__file__).parent
        / "fixtures"
        / "remote-safe-synthetic-flow-release-verification.json"
    ).read_bytes()
    artifact = FlowReleaseVerificationReceiptArtifactBytes(
        artifact_bytes_base64=b64encode(raw).decode("ascii"),
        artifact_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
    )
    return artifact.identity(now=datetime(2026, 8, 28, tzinfo=timezone.utc))


def _registration_fields() -> dict[str, object]:
    releases = {
        target: LocalRuntimeReleaseBinding(
            target=target,
            admission_id=f"00000000-0000-4000-8000-{index:012d}",
            admission_sha256=f"{index:x}" * 64,
            release_version="1.35.0" if target == "flow" else "1.0.0",
            release_artifact_sha256=f"{index + 3:x}" * 64,
        )
        for index, target in enumerate(("flow", "desktop", "capture"), start=1)
    }
    return {
        "name": "runner",
        "platform": "linux",
        "agent_version": "1.0.0",
        "engine_version": "1.35.0",
        "mode": "service",
        "capabilities": RegisterCapabilities(
            backends=("linux",),
            attended=False,
            effects_substrates=("linux",),
        ),
        "local_runtime_release": releases,
    }


def test_hosted_registration_v1_34_and_v2_shapes_are_disjoint() -> None:
    common = _registration_fields()
    legacy_raw = {
        "schema_version": "openadapt.hosted-runner-registration/v1",
        **common,
    }
    current_raw = {
        "schema_version": "openadapt.hosted-runner-registration/v2",
        **common,
        "local_flow_release": _local_flow_release(),
    }

    legacy = parse_register_request(legacy_raw)
    current = parse_register_request(current_raw)

    assert isinstance(legacy, RegisterRequestV1)
    assert isinstance(current, RegisterRequestV2)
    assert set(legacy.model_dump(mode="json")) == {
        "schema_version",
        "name",
        "platform",
        "agent_version",
        "engine_version",
        "mode",
        "capabilities",
        "local_runtime_release",
    }
    assert set(current.model_dump(mode="json")) == {
        *legacy.model_dump(mode="json"),
        "local_flow_release",
    }
    with pytest.raises(ValidationError):
        RegisterRequestV1.model_validate(current_raw)
    with pytest.raises(ValidationError):
        RegisterRequestV2.model_validate(legacy_raw)


@pytest.mark.parametrize("version", ("v1", "v2"))
def test_hosted_response_versions_parse_exact_wire_shapes(version: str) -> None:
    registration_raw = {
        "schema_version": f"openadapt.hosted-runner-registration-result/{version}",
        "runner_id": "11111111-1111-4111-8111-111111111111",
        "tenant_id": "22222222-2222-4222-8222-222222222222",
        "runner_session_id": "33333333-3333-4333-8333-333333333333",
        "runner_token": "oar_" + "a" * 64,
        "token_expires_at": "2099-01-01T00:00:00Z",
    }
    callback_raw = {
        "schema_version": f"openadapt.hosted-runner-callback-result/{version}",
        "status": "accepted",
        "run_id": "44444444-4444-4444-8444-444444444444",
        "outcome": "VERIFIED",
        "dispatch_state": "closed",
        "accepted_events": 2,
    }

    registration = parse_register_response(registration_raw)
    callback = parse_callback_response(callback_raw)

    expected_registration_type = (
        RegisterResponseV1 if version == "v1" else RegisterResponseV2
    )
    expected_callback_type = (
        CallbackResponseV1 if version == "v1" else CallbackResponseV2
    )
    assert isinstance(registration, expected_registration_type)
    assert isinstance(callback, expected_callback_type)
    assert set(registration.model_dump(mode="json")) == set(registration_raw)
    assert set(callback.model_dump(mode="json")) == set(callback_raw)


@pytest.mark.parametrize(
    "parser",
    (
        parse_register_request,
        parse_register_response,
        parse_hosted_dispatch,
        parse_hosted_terminal_event,
        parse_callback_request,
        parse_callback_response,
    ),
)
@pytest.mark.parametrize(
    "raw",
    ({}, {"schema_version": "openadapt.unsupported/v99"}),
)
def test_hosted_version_parsers_refuse_missing_or_unknown_schema(parser, raw) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        parser(raw)


def test_hosted_dispatch_v1_34_shape_parses_and_builds_v1_callback(
    tmp_path, sealed
) -> None:
    workflow, _ = sealed
    current = _hosted_dispatch(workflow)
    legacy_raw = current.model_dump(mode="python")
    legacy_raw["schema_version"] = "openadapt.hosted-runner/v1"
    for field in (
        "flow_release_verification_receipt",
        "execution_authority_id",
        "execution_authority_sha256",
        "execution_authority_signer_sha256",
    ):
        legacy_raw.pop(field)

    legacy = parse_hosted_dispatch(legacy_raw)

    assert isinstance(legacy, HostedDispatchV1)
    assert HostedDispatchV1.model_fields["schema_version"].is_required()
    assert set(legacy.model_dump(mode="json")) == {
        "schema_version",
        "dispatch_id",
        "tenant_id",
        "runner_id",
        "runner_session_id",
        "dispatch_session_id",
        "run_id",
        "workflow_id",
        "workflow_version_id",
        "idempotency_key",
        "lease_token",
        "lease_expires_at",
        "product_release_admission",
        "workflow_admission",
        "managed_delivery_authority_url",
        "delivery_authority_token",
        "payload",
    }
    with pytest.raises(ValidationError):
        HostedDispatchV2.model_validate(legacy_raw)
    with pytest.raises(ValidationError):
        HostedDispatchV1.model_validate(current.model_dump(mode="python"))

    runner_calls = 0

    def runner(*_args, **_kwargs):
        nonlocal runner_calls
        runner_calls += 1
        raise AssertionError("legacy dispatch must not reach the managed runner")

    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite", runner=runner)
    run_dir = tmp_path / "run"
    refusal = adapter.execute(
        legacy,
        runner_config=tmp_path / "runner.toml",
        run_dir=run_dir,
        authority=DeliveryAuthority(
            legacy.managed_delivery_authority_url,
            legacy.delivery_authority_token,
        ),
    )
    callback = adapter.callback_request(legacy, refusal)

    assert refusal.code == "hosted_protocol_upgrade_required"
    assert runner_calls == 0
    assert not run_dir.exists()
    assert (
        adapter._ledger.lookup(f"{legacy.tenant_id}:{legacy.idempotency_key}") is None
    )
    assert isinstance(callback, CallbackRequestV1)
    assert set(callback.model_dump(mode="json")) == {
        "schema_version",
        "dispatch_id",
        "runner_session_id",
        "idempotency_key",
        "lease_token",
        "product_release_admission_sha256",
        "workflow_admission_sha256",
        "events",
    }
    assert callback.events[-1]["schema_version"] == (
        "openadapt.hosted-runner-terminal/v1"
    )
    assert parse_callback_request(callback.model_dump(mode="python")) == callback

    recovery = HostedRecoveryBindingV1(
        dispatch_id=legacy.dispatch_id,
        runner_session_id=legacy.runner_session_id,
        dispatch_session_id=legacy.dispatch_session_id,
        run_id=legacy.run_id,
        workflow_id=legacy.workflow_id,
        idempotency_key=legacy.idempotency_key,
        lease_token=legacy.lease_token,
        product_release_admission_sha256=(
            legacy.product_release_admission.artifact_sha256
        ),
        workflow_admission_sha256=legacy.workflow_admission.artifact_sha256,
        bundle_content_digest=legacy.payload.bundle.content_digest,
        authorization_id=legacy.payload.authorization.authorization_id,
    )
    recovery_callback = adapter.callback_request(
        recovery.model_dump(mode="python"), refusal
    )
    assert isinstance(recovery_callback, CallbackRequestV1)
    assert set(recovery.model_dump(mode="json")) == {
        "schema_version",
        "dispatch_id",
        "runner_session_id",
        "dispatch_session_id",
        "run_id",
        "workflow_id",
        "idempotency_key",
        "lease_token",
        "product_release_admission_sha256",
        "workflow_admission_sha256",
        "bundle_content_digest",
        "authorization_id",
    }


def test_hosted_dispatch_v2_recovery_binds_flow_digest_and_refusal_is_non_closing(
    tmp_path, sealed
) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    binding = adapter.recovery_binding(dispatch)
    refusal = adapter._refusal(dispatch, "hosted_admission_refused", "refused")

    dispatch_fields = set(dispatch.model_dump(mode="json"))
    assert dispatch_fields == {
        "schema_version",
        "dispatch_id",
        "tenant_id",
        "runner_id",
        "runner_session_id",
        "dispatch_session_id",
        "run_id",
        "workflow_id",
        "workflow_version_id",
        "idempotency_key",
        "lease_token",
        "lease_expires_at",
        "flow_release_verification_receipt",
        "product_release_admission",
        "workflow_admission",
        "execution_authority_id",
        "execution_authority_sha256",
        "execution_authority_signer_sha256",
        "managed_delivery_authority_url",
        "delivery_authority_token",
        "payload",
    }
    assert not dispatch.execution_authority_sha256.startswith("sha256:")
    assert not dispatch.execution_authority_signer_sha256.startswith("sha256:")
    assert isinstance(binding, HostedRecoveryBindingV2)
    assert set(binding.model_dump(mode="json")) == {
        "schema_version",
        "dispatch_id",
        "runner_session_id",
        "dispatch_session_id",
        "run_id",
        "workflow_id",
        "idempotency_key",
        "lease_token",
        "flow_release_verification_receipt_object_sha256",
        "workflow_admission_sha256",
        "bundle_content_digest",
        "authorization_id",
    }
    assert binding.flow_release_verification_receipt_object_sha256 == (
        dispatch.flow_release_verification_receipt.artifact_sha256
    )
    assert binding.flow_release_verification_receipt_object_sha256.startswith("sha256:")
    with pytest.raises(ValueError, match="cannot close a proofless"):
        adapter.callback_request(binding.model_dump(mode="python"), refusal)


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


def test_registration_v2_binds_engine_and_installed_flow_versions(
    monkeypatch, tmp_path, config
) -> None:
    fields = _registration_fields()
    release_bindings = fields["local_runtime_release"]
    capabilities = fields["capabilities"]
    assert isinstance(release_bindings, dict)
    assert isinstance(capabilities, RegisterCapabilities)
    local_config = replace(
        config,
        host="https://cloud.example",
        local_runtime_release=tuple(
            LocalRuntimeRelease(**item.model_dump(mode="python"))
            for item in release_bindings.values()
        ),
        local_flow_release=_local_flow_release(),
    )
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    monkeypatch.setattr(
        hosted, "load_runner_config", lambda *_args, **_kwargs: local_config
    )

    request = adapter.registration_request(
        runner_config=tmp_path / "runner.toml",
        name="runner",
        platform="linux",
        agent_version="1.0.0",
        engine_version="1.35.0",
        mode="service",
        capabilities=capabilities,
    )

    assert isinstance(request, RegisterRequestV2)
    assert request.schema_version == "openadapt.hosted-runner-registration/v2"
    assert request.local_flow_release == _local_flow_release()
    assert request.local_runtime_release["flow"].release_version == "1.35.0"

    with pytest.raises(ValueError, match="differs from its verification receipt"):
        adapter.registration_request(
            runner_config=tmp_path / "runner.toml",
            name="runner",
            platform="linux",
            agent_version="1.0.0",
            engine_version="1.35.1",
            mode="service",
            capabilities=capabilities,
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


def _result_loss_closure_result(
    dispatch: HostedDispatch,
    marker: hosted.ManagedChildStartEvidence,
    request: hosted.ProductionDeliveryResultLossClosureRequest,
    chain: ProductionDeliveryPermitChain,
) -> hosted.ProductionDeliveryResultLossClosureResult:
    all_permits = (*chain.entries, *((chain.pending,) if chain.pending else ()))
    assert all_permits
    first = all_permits[0]
    final = all_permits[-1]
    pending = chain.pending
    acknowledged = chain.entries[-1] if chain.entries else None
    payload = ProductionDeliveryResultLossClosurePayload(
        closure_id="00000000-0000-4000-8000-000000000011",
        closure_request_sha256=request.request_sha256(),
        closed_at=request.result_loss_observed_at,
        result_loss_observed_at=request.result_loss_observed_at,
        receipt_absence_observed_at=(
            pending.receipt_absence_observed_at if pending is not None else None
        ),
        tenant_id=dispatch.tenant_id,
        run_id=dispatch.run_id,
        flow_run_id_sha256=hashlib.sha256(dispatch.run_id.encode("utf-8")).hexdigest(),
        dispatch_id=dispatch.dispatch_id,
        dispatch_session_id=dispatch.dispatch_session_id,
        managed_dispatch_binding_sha256=dispatch.payload.dispatch_binding_sha256,
        idempotency_key_sha256=hosted.managed_result_loss_idempotency_sha256(
            dispatch.idempotency_key
        ),
        authenticated_runner_id_sha256=hashlib.sha256(
            dispatch.runner_id.encode("utf-8")
        ).hexdigest(),
        authenticated_session_id_sha256=hashlib.sha256(
            dispatch.runner_session_id.encode("utf-8")
        ).hexdigest(),
        execution_authority_id=dispatch.execution_authority_id,
        execution_authority_sha256=dispatch.execution_authority_sha256,
        execution_authority_signer_sha256=(dispatch.execution_authority_signer_sha256),
        child_started_at=marker.started_at,
        child_start_evidence_sha256=marker.marker_sha256,
        run_store_identity_sha256=marker.run_store_identity_sha256,
        permit_chain_sha256=chain.permit_chain_sha256,
        permit_count=len(all_permits),
        acknowledged_permit_count=len(chain.entries),
        pending_permit_count=1 if pending else 0,
        pending_permit_artifact_sha256=(
            pending.permit_artifact_sha256 if pending else None
        ),
        run_request_sha256=first.run_request_sha256,
        pending_action_request_sha256=(
            pending.action_request_sha256 if pending else None
        ),
        final_input_edge_sequence=final.input_edge_sequence,
        final_authority_sequence=final.authority_sequence,
        final_runtime_delivery_sequence=(
            acknowledged.runtime_delivery_sequence if acknowledged else 0
        ),
    )
    artifact = sign_production_delivery_result_loss_closure(
        payload,
        Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64))),
    )
    artifact_raw = hosted.canonical_json(artifact)
    chain_raw = hosted.canonical_json(chain)
    return hosted.ProductionDeliveryResultLossClosureResult(
        closure_artifact_bytes_base64=b64encode(artifact_raw).decode("ascii"),
        closure_artifact_sha256=artifact.artifact_sha256(),
        permit_chain_bytes_base64=b64encode(chain_raw).decode("ascii"),
        permit_chain_sha256=chain.permit_chain_sha256,
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

        def production_delivery_permit_chain(self, **_kwargs):
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
    # The one-use terminal CAS remains authoritative after a component reread
    # fault. Removing it would let a late result replace a sealed outcome.
    assert (storage_failure_dir / "production-terminal-state.json").is_file()
    assert (storage_failure_dir / "production-terminal-report.json").is_file()
    assert (storage_failure_dir / "production-terminal-verification.json").is_file()
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

    replayed_proof, replayed_report_sha256 = adapter._produce_terminal_verification(
        dispatch=dispatch,
        report=report,
        run_dir=run_dir,
        qualification=qualification,
        private_key=private_key,
        verified_params=verified_params,
        dispatch_binding_sha256=binding,
    )
    assert hosted.canonical_json(replayed_proof) == hosted.canonical_json(proof)
    assert replayed_report_sha256 == report_sha256

    late_report = report.model_copy(update={"total_ms": report.total_ms + 1})
    with pytest.raises(ValueError, match="different terminal outcome"):
        adapter._produce_terminal_verification(
            dispatch=dispatch,
            report=late_report,
            run_dir=run_dir,
            qualification=qualification,
            private_key=private_key,
            verified_params=verified_params,
            dispatch_binding_sha256=binding,
        )
    assert proof_path.read_bytes() == hosted.canonical_json(proof)

    race_dir = tmp_path / "terminal-race"
    race_dir.mkdir(mode=0o700)

    def terminalize_once(_index):
        return adapter._produce_terminal_verification(
            dispatch=dispatch,
            report=report,
            run_dir=race_dir,
            qualification=qualification,
            private_key=private_key,
            verified_params=verified_params,
            dispatch_binding_sha256=binding,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        raced = list(pool.map(terminalize_once, range(4)))
    assert {hosted.canonical_json(item[0]) for item in raced} == {
        hosted.canonical_json(proof)
    }
    assert {item[1] for item in raced} == {report_sha256}

    advancing_race_dir = tmp_path / "advancing-terminal-race"
    advancing_race_dir.mkdir(mode=0o700)

    class AdvancingDatetime(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            value = fixed_now.replace(second=2 + cls.calls)
            return value if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr(hosted, "datetime", AdvancingDatetime)

    def advancing_terminalize_once(_index):
        return adapter._produce_terminal_verification(
            dispatch=dispatch,
            report=report,
            run_dir=advancing_race_dir,
            qualification=qualification,
            private_key=private_key,
            verified_params=verified_params,
            dispatch_binding_sha256=binding,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        advancing_raced = list(pool.map(advancing_terminalize_once, range(4)))
    assert len({hosted.canonical_json(item[0]) for item in advancing_raced}) == 1
    assert {item[1] for item in advancing_raced} == {report_sha256}


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

    with pytest.raises(RuntimeError, match="signed terminal reconciliation"):
        adapter.execute(
            dispatch,
            runner_config=tmp_path / "runner.toml",
            run_dir=tmp_path / "run",
            authority=authority,
        )
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
    with pytest.raises(RuntimeError, match="signed terminal reconciliation"):
        adapter.execute(
            dispatch,
            runner_config=tmp_path / "runner.toml",
            run_dir=tmp_path / "run",
            authority=authority,
        )
    with pytest.raises(RuntimeError, match="signed terminal reconciliation"):
        adapter.execute(
            dispatch,
            runner_config=tmp_path / "runner.toml",
            run_dir=tmp_path / "run-again",
            authority=authority,
        )
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


@pytest.mark.parametrize(
    ("execution", "expected_loss_code"),
    [
        (RuntimeError("child exit result lost"), "runner_exception"),
        (OSError("child transport result lost"), "runner_exception"),
        (
            ManagedExecution(returncode=1, report_bytes=None),
            "report_missing",
        ),
        (
            ManagedExecution(returncode=1, report_bytes=b"{"),
            "report_invalid",
        ),
    ],
    ids=(
        "runtime-exception",
        "transport-exception",
        "missing-report",
        "malformed-report",
    ),
)
def test_expected_result_loss_returns_one_signed_reconciliation_without_retry(
    monkeypatch,
    tmp_path,
    config,
    sealed,
    execution,
    expected_loss_code,
) -> None:
    workflow, _ = sealed
    calls = 0

    def runner(_argv, _run_dir, _child_env):
        nonlocal calls
        calls += 1
        if isinstance(execution, Exception):
            raise execution
        return execution

    adapter, dispatch = _prepared_adapter(
        monkeypatch, tmp_path, config, workflow, runner
    )
    payload = dispatch.payload.model_copy(
        update={
            "run_id": IDS["run_id"],
            "workflow_id": IDS["workflow_id"],
        }
    )
    payload = payload.model_copy(
        update={
            "dispatch_binding_sha256": dispatch_binding_sha256(
                IDS["run_id"], payload.authorization
            )
        }
    )
    dispatch = HostedDispatch.model_validate(
        {
            **dispatch.model_dump(mode="json"),
            "run_id": IDS["run_id"],
            "workflow_id": IDS["workflow_id"],
            "payload": payload.model_dump(mode="json"),
        }
    )
    proof = sign_production_terminal_verification(
        _managed_result_loss_payload(), _private_key()
    )
    report = _production_report()
    seen_loss_codes: list[str] = []
    seen_fence_codes: list[str] = []

    def retain_loss_closure(**kwargs):
        seen_fence_codes.append(kwargs["loss_code"])
        return object()

    def produce_loss(**kwargs):
        seen_loss_codes.append(kwargs["loss_code"])
        return proof, proof.payload.run_report_sha256, report

    monkeypatch.setattr(
        adapter,
        "_retain_managed_result_loss_closure",
        retain_loss_closure,
    )
    monkeypatch.setattr(adapter, "_produce_managed_result_loss", produce_loss)
    authority = DeliveryAuthority(
        dispatch.managed_delivery_authority_url,
        dispatch.delivery_authority_token,
    )

    result = adapter.execute(
        dispatch,
        runner_config=tmp_path / "runner.toml",
        run_dir=tmp_path / "run",
        authority=authority,
        closure_authority=SimpleNamespace(),
    )
    callback = adapter.callback_request(dispatch, result)

    assert calls == 1
    assert seen_fence_codes == [expected_loss_code]
    assert seen_loss_codes == [expected_loss_code]
    assert result.outcome is TransactionOutcome.RECONCILIATION_REQUIRED
    assert result.uncertain_delivery is True
    assert result.terminal_verification == proof
    assert (
        len(
            [
                event
                for event in callback.events
                if event.get("schema_version") == "openadapt.hosted-runner-terminal/v2"
            ]
        )
        == 1
    )
    assert callback.events[-1]["outcome"] == "RECONCILIATION_REQUIRED"
    assert callback.events[-1]["uncertain_delivery"] is True
    with pytest.raises(RuntimeError, match="signed terminal reconciliation"):
        adapter.execute(
            dispatch,
            runner_config=tmp_path / "runner.toml",
            run_dir=tmp_path / "run-again",
            authority=authority,
        )
    assert calls == 1


@pytest.mark.parametrize(
    ("result_mode", "expected_loss_code"),
    [
        ("runner-exception", "runner_exception"),
        ("missing-report", "report_missing"),
        ("malformed-report", "report_invalid"),
        ("conflicting-child-proof", "report_invalid"),
    ],
)
def test_result_loss_fences_before_terminal_trust_revalidation(
    monkeypatch,
    tmp_path,
    config,
    sealed,
    result_mode,
    expected_loss_code,
) -> None:
    workflow, _ = sealed

    def runner(_argv, _run_dir, _child_env):
        if result_mode == "runner-exception":
            raise RuntimeError("child result lost")
        if result_mode == "missing-report":
            return ManagedExecution(returncode=1, report_bytes=None)
        if result_mode == "malformed-report":
            return ManagedExecution(returncode=1, report_bytes=b"{")
        return ManagedExecution(
            returncode=0,
            report_bytes=_production_report().model_dump_json().encode("utf-8"),
            terminal_verification=sign_production_terminal_verification(
                _managed_result_loss_payload(),
                _private_key(),
            ),
        )

    adapter, dispatch = _prepared_adapter(
        monkeypatch,
        tmp_path,
        config,
        workflow,
        runner,
    )
    fence_codes: list[str] = []

    def retain_fence(**kwargs):
        fence_codes.append(kwargs["loss_code"])
        return object()

    monkeypatch.setattr(
        adapter,
        "_retain_managed_result_loss_closure",
        retain_fence,
    )
    monkeypatch.setattr(
        adapter,
        "_revalidate_terminal_authority",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("authority revoked")),
    )
    authority = DeliveryAuthority(
        dispatch.managed_delivery_authority_url,
        dispatch.delivery_authority_token,
    )

    with pytest.raises(RuntimeError, match="signed terminal reconciliation"):
        adapter.execute(
            dispatch,
            runner_config=tmp_path / "runner.toml",
            run_dir=tmp_path / "run",
            authority=authority,
            closure_authority=SimpleNamespace(),
        )

    assert fence_codes == [expected_loss_code]
    reservation = adapter._ledger.lookup(
        f"{dispatch.tenant_id}:{dispatch.idempotency_key}"
    )
    assert reservation is not None
    assert reservation["outcome"] == "RECONCILIATION_REQUIRED"


def test_result_loss_fences_before_missing_child_manifest_is_read(
    monkeypatch, tmp_path, config, sealed
) -> None:
    workflow, _ = sealed

    def runner(_argv, _run_dir, _child_env):
        raise RuntimeError("child result lost")

    adapter, dispatch = _prepared_adapter(
        monkeypatch,
        tmp_path,
        config,
        workflow,
        runner,
    )
    fenced = object()
    monkeypatch.setattr(
        adapter,
        "_retain_managed_result_loss_closure",
        lambda **_kwargs: fenced,
    )
    monkeypatch.setattr(
        adapter,
        "_revalidate_terminal_authority",
        lambda **_kwargs: (object(), object()),
    )
    authority = DeliveryAuthority(
        dispatch.managed_delivery_authority_url,
        dispatch.delivery_authority_token,
    )

    with pytest.raises(RuntimeError, match="signed terminal reconciliation") as error:
        adapter.execute(
            dispatch,
            runner_config=tmp_path / "runner.toml",
            run_dir=tmp_path / "run",
            authority=authority,
            closure_authority=SimpleNamespace(),
        )

    assert isinstance(error.value.__cause__, ValueError)
    assert "retained run manifest" in str(error.value.__cause__)


def test_restart_recovery_fences_before_current_runner_trust_is_loaded(
    monkeypatch, tmp_path, sealed
) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    adapter._write_child_start_evidence(
        dispatch=dispatch,
        run_dir=run_dir,
        dispatch_binding_sha256=dispatch.payload.dispatch_binding_sha256,
    )
    adapter._ledger.reserve(
        f"{dispatch.tenant_id}:{dispatch.idempotency_key}",
        run_id=dispatch.run_id,
    )
    fence_codes: list[str] = []

    def retain_fence(**kwargs):
        fence_codes.append(kwargs["loss_code"])
        return object()

    monkeypatch.setattr(
        adapter,
        "_retain_managed_result_loss_closure",
        retain_fence,
    )
    monkeypatch.setattr(
        hosted,
        "load_runner_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("runner trust unavailable")
        ),
    )

    with pytest.raises(ValueError, match="runner trust unavailable"):
        adapter.recover_uncertain_run(
            dispatch,
            runner_config=tmp_path / "runner.toml",
            run_dir=run_dir,
            closure_authority=SimpleNamespace(),
        )

    assert fence_codes == ["recovered_after_restart"]


def test_managed_result_loss_report_is_outer_recovery_only(tmp_path, sealed) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    marker = hosted.ManagedChildStartEvidence.create(
        started_at="2026-08-26T12:00:00Z",
        dispatch_id=dispatch.dispatch_id,
        dispatch_session_id=dispatch.dispatch_session_id,
        run_id=dispatch.run_id,
        managed_dispatch_binding_sha256=dispatch.payload.dispatch_binding_sha256,
        authenticated_runner_id_sha256=hashlib.sha256(
            dispatch.runner_id.encode("utf-8")
        ).hexdigest(),
        authenticated_session_id_sha256=hashlib.sha256(
            dispatch.runner_session_id.encode("utf-8")
        ).hexdigest(),
        execution_authority_id=dispatch.execution_authority_id,
        execution_authority_sha256=dispatch.execution_authority_sha256,
        execution_authority_signer_sha256=(dispatch.execution_authority_signer_sha256),
        run_store_identity_sha256=adapter._run_store_identity_sha256(run_dir),
    )
    loss = hosted.ManagedResultLossEvidence.create(
        loss_code="report_missing",
        child_started_at=marker.started_at,
        child_start_evidence_sha256=marker.marker_sha256,
        run_store_identity_sha256=marker.run_store_identity_sha256,
        observed_at="2026-08-26T12:00:02Z",
        run_id=dispatch.run_id,
        flow_run_id_sha256=hashlib.sha256(dispatch.run_id.encode("utf-8")).hexdigest(),
        dispatch_id=dispatch.dispatch_id,
        dispatch_session_id=dispatch.dispatch_session_id,
        managed_dispatch_binding_sha256=dispatch.payload.dispatch_binding_sha256,
        idempotency_key_sha256=hosted.managed_result_loss_idempotency_sha256(
            dispatch.idempotency_key
        ),
        authenticated_runner_id_sha256=marker.authenticated_runner_id_sha256,
        authenticated_session_id_sha256=(marker.authenticated_session_id_sha256),
        execution_authority_id=dispatch.execution_authority_id,
        execution_authority_sha256=dispatch.execution_authority_sha256,
        execution_authority_signer_sha256=(dispatch.execution_authority_signer_sha256),
        delivery_result_loss_closure_artifact_sha256="d" * 64,
        pending_permit_artifact_sha256="a" * 64,
        run_request_sha256="b" * 64,
        pending_action_request_sha256="c" * 64,
    )
    manifest = SimpleNamespace(
        governed_authorization=dispatch.payload.authorization.model_copy(
            update={"execution_profile": "standard"}
        ),
        model_calls=0,
    )

    report = adapter._managed_result_loss_report(
        dispatch=dispatch,
        workflow=workflow,
        manifest=manifest,
        marker=marker,
        evidence=loss,
        verified_params=dict(dispatch.payload.params.values),
        runtime_substrate="web",
    )

    assert report.managed_result_loss is not None
    assert report.managed_result_loss.report_provenance == "outer_runner_recovery"
    assert report.execution_outcome == "HALTED"
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"
    assert report.transaction_billable is False
    assert report.effect_journal == []
    assert report.outcome_envelope is not None
    assert report.outcome_envelope.required_contracts.model_dump() == {
        "authorization": 1,
        "identity": 0,
        "postcondition": 0,
        "effect": 0,
    }
    invalid = report.model_dump(mode="json")
    invalid["success"] = True
    with pytest.raises(ValueError, match="managed result loss report contract"):
        hosted.RunReport.model_validate(invalid)


def test_concurrent_managed_result_loss_terminalizers_reuse_one_exact_record(
    monkeypatch, tmp_path, sealed
) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    binding = dispatch.payload.dispatch_binding_sha256
    marker = hosted.ManagedChildStartEvidence.create(
        started_at="2026-08-26T11:59:59Z",
        dispatch_id=dispatch.dispatch_id,
        dispatch_session_id=dispatch.dispatch_session_id,
        run_id=dispatch.run_id,
        managed_dispatch_binding_sha256=binding,
        authenticated_runner_id_sha256=hashlib.sha256(
            dispatch.runner_id.encode("utf-8")
        ).hexdigest(),
        authenticated_session_id_sha256=hashlib.sha256(
            dispatch.runner_session_id.encode("utf-8")
        ).hexdigest(),
        execution_authority_id=dispatch.execution_authority_id,
        execution_authority_sha256=dispatch.execution_authority_sha256,
        execution_authority_signer_sha256=(dispatch.execution_authority_signer_sha256),
        run_store_identity_sha256=adapter._run_store_identity_sha256(run_dir),
    )
    adapter._write_private_bytes_atomic_exclusive(
        run_dir / "managed-child-started.json", hosted.canonical_json(marker)
    )
    acknowledged = _terminal_delivery_chain(
        dispatch,
        admission_sha256="d" * 64,
        evidence_identity_sha256="e" * 64,
        environment_digest="a" * 64,
        registry_sha256="e" * 64,
    )
    assert acknowledged.entries
    permit = acknowledged.entries[0].permit_artifact

    class Store:
        def __init__(self, _run_dir):
            pass

        def read_manifest(self):
            return SimpleNamespace()

    class ClosureAuthority:
        requests: list[hosted.ProductionDeliveryResultLossClosureRequest] = []

        def close_result_loss(self, run_id, lease_token, request):
            assert run_id == dispatch.run_id
            assert lease_token == dispatch.lease_token
            assert "oal_" not in request.canonical_bytes().decode("utf-8")
            self.requests.append(request)
            pending = ProductionPendingDeliveryPermit.build(
                permit,
                receipt_absence_observed_at=request.result_loss_observed_at,
            )
            chain = ProductionDeliveryPermitChain.build((), pending=pending)
            return _result_loss_closure_result(dispatch, marker, request, chain)

    closure_authority = ClosureAuthority()

    class RacingDatetime(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            second = 1 + cls.calls
            value = datetime(2026, 8, 26, 12, 0, second, tzinfo=timezone.utc)
            return value if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr(hosted, "CheckpointStore", Store)
    monkeypatch.setattr(hosted, "datetime", RacingDatetime)
    monkeypatch.setattr(
        adapter,
        "_managed_result_loss_report",
        lambda **kwargs: kwargs["evidence"],
    )
    monkeypatch.setattr(
        adapter,
        "_produce_terminal_verification",
        lambda **kwargs: (
            kwargs["report"],
            kwargs["report"].evidence_sha256,
        ),
    )
    qualification = SimpleNamespace(
        expected=SimpleNamespace(
            runtime_build_identity=SimpleNamespace(substrate="web")
        )
    )

    def terminalize(loss_code, closer=closure_authority):
        return adapter._produce_managed_result_loss(
            dispatch=dispatch,
            workflow=workflow,
            run_dir=run_dir,
            qualification=qualification,
            private_key=object(),
            verified_params=dict(dispatch.payload.params.values),
            dispatch_binding_sha256=binding,
            closure_authority=closer,
            loss_code=loss_code,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(
                terminalize,
                ("runner_exception", "report_missing", "recovered_after_restart"),
            )
        )

    retained = (run_dir / "managed-result-loss.json").read_bytes()
    snapshot = hosted.ManagedResultLossSnapshot.model_validate_json(retained)
    evidence_bytes = hosted.canonical_json(snapshot.evidence)
    assert hosted.canonical_json(snapshot) == retained
    assert {hosted.canonical_json(item[0]) for item in results} == {evidence_bytes}
    assert {hosted.canonical_json(item[2]) for item in results} == {evidence_bytes}
    assert {item[1] for item in results} == {snapshot.evidence.evidence_sha256}
    assert closure_authority.requests
    assert len({item.request_sha256() for item in closure_authority.requests}) == 1

    class RefuseClosureReplay:
        def close_result_loss(self, *_args, **_kwargs):
            raise AssertionError("retained result loss requested a second closure")

    replayed = terminalize("recovered_after_restart", RefuseClosureReplay())
    assert hosted.canonical_json(replayed[0]) == evidence_bytes
    assert hosted.canonical_json(replayed[2]) == evidence_bytes
    if os.name != "nt":
        assert (run_dir / "managed-result-loss.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("pending", [True, False], ids=("pending", "ack-won"))
def test_result_loss_closure_response_binds_exact_request_and_chain(
    tmp_path, sealed, pending
) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    marker = hosted.ManagedChildStartEvidence.create(
        started_at="2026-08-26T11:59:59Z",
        dispatch_id=dispatch.dispatch_id,
        dispatch_session_id=dispatch.dispatch_session_id,
        run_id=dispatch.run_id,
        managed_dispatch_binding_sha256=dispatch.payload.dispatch_binding_sha256,
        authenticated_runner_id_sha256=hashlib.sha256(
            dispatch.runner_id.encode("utf-8")
        ).hexdigest(),
        authenticated_session_id_sha256=hashlib.sha256(
            dispatch.runner_session_id.encode("utf-8")
        ).hexdigest(),
        execution_authority_id=dispatch.execution_authority_id,
        execution_authority_sha256=dispatch.execution_authority_sha256,
        execution_authority_signer_sha256=(dispatch.execution_authority_signer_sha256),
        run_store_identity_sha256=adapter._run_store_identity_sha256(run_dir),
    )
    request = hosted.ProductionDeliveryResultLossClosureRequest(
        child_start_evidence=marker,
        result_loss_observed_at="2026-08-26T12:00:02Z",
    )
    acknowledged = _terminal_delivery_chain(
        dispatch,
        admission_sha256="d" * 64,
        evidence_identity_sha256="e" * 64,
        environment_digest="a" * 64,
        registry_sha256="e" * 64,
    )
    if pending:
        permit = acknowledged.entries[0].permit_artifact
        chain = ProductionDeliveryPermitChain.build(
            (),
            pending=ProductionPendingDeliveryPermit.build(
                permit,
                receipt_absence_observed_at=request.result_loss_observed_at,
            ),
        )
    else:
        chain = acknowledged

    result = _result_loss_closure_result(dispatch, marker, request, chain)
    artifact, retained_chain = result.artifacts()

    assert retained_chain == chain
    assert artifact.payload.closure_request_sha256 == request.request_sha256()
    assert artifact.payload.pending_permit_count == (1 if pending else 0)
    assert dispatch.lease_token not in request.canonical_bytes().decode("utf-8")
    assert dispatch.delivery_authority_token not in request.canonical_bytes().decode(
        "utf-8"
    )
    invalid = result.model_dump(mode="json")
    invalid["permit_chain_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="closure response"):
        hosted.ProductionDeliveryResultLossClosureResult.model_validate(invalid)


def test_unavailable_result_loss_fence_never_returns_a_callback_or_retries(
    monkeypatch, tmp_path, config, sealed
) -> None:
    workflow, _ = sealed
    calls = 0

    def runner(_argv, _run_dir, _child_env):
        nonlocal calls
        calls += 1
        raise RuntimeError("child result lost")

    adapter, dispatch = _prepared_adapter(
        monkeypatch, tmp_path, config, workflow, runner
    )
    monkeypatch.setattr(
        adapter,
        "_revalidate_terminal_authority",
        lambda **_kwargs: (object(), object()),
    )

    def refuse_unsigned_result_loss(**_kwargs):
        raise ValueError("hosted result-loss closure is unavailable")

    monkeypatch.setattr(
        adapter,
        "_produce_managed_result_loss",
        refuse_unsigned_result_loss,
    )
    authority = DeliveryAuthority(
        dispatch.managed_delivery_authority_url,
        dispatch.delivery_authority_token,
    )
    with pytest.raises(RuntimeError, match="signed terminal reconciliation"):
        adapter.execute(
            dispatch,
            runner_config=tmp_path / "runner.toml",
            run_dir=tmp_path / "run",
            authority=authority,
            closure_authority=SimpleNamespace(),
        )

    reservation = adapter._ledger.lookup(
        f"{dispatch.tenant_id}:{dispatch.idempotency_key}"
    )
    assert reservation is not None
    assert reservation["outcome"] == "RECONCILIATION_REQUIRED"
    with pytest.raises(RuntimeError, match="signed terminal reconciliation"):
        adapter.execute(
            dispatch,
            runner_config=tmp_path / "runner.toml",
            run_dir=tmp_path / "run-again",
            authority=authority,
            closure_authority=SimpleNamespace(),
        )
    assert calls == 1


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
    assert (
        adapter._ledger.lookup(f"{dispatch.tenant_id}:{dispatch.idempotency_key}")
        is None
    )
    with pytest.raises(ValueError, match="cannot close a proofless"):
        adapter.callback_request(dispatch, result)


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


def test_missing_evidence_runner_key_refuses_before_managed_runner(
    monkeypatch, tmp_path, config, sealed
) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("managed runner must not start")

    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite", runner=runner)
    local_config = replace(
        config,
        host="https://cloud.example",
        evidence_runner_private_key=None,
    )
    monkeypatch.setattr(
        hosted, "load_runner_config", lambda *_args, **_kwargs: local_config
    )
    monkeypatch.setattr(adapter, "_verify_product_release", lambda *_: None)
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
    assert result.code == "hosted_admission_refused"
    assert result.detail == "prestart_ValueError"
    assert result.started is False
    assert result.uncertain_delivery is False
    assert calls == 0
    assert (
        adapter._ledger.lookup(f"{dispatch.tenant_id}:{dispatch.idempotency_key}")
        is None
    )
    with pytest.raises(ValueError, match="cannot close a proofless"):
        adapter.callback_request(dispatch, result)


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


def test_v2_refusal_cannot_form_a_closing_callback(tmp_path, sealed) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    refusal = adapter._refusal(dispatch, "hosted_admission_refused", "refused")

    with pytest.raises(ValueError, match="cannot close a proofless"):
        adapter.callback_request(dispatch, refusal)
    with pytest.raises(ValueError, match="cannot close a proofless"):
        adapter.callback_request(adapter.recovery_binding(dispatch), refusal)


@pytest.mark.parametrize(
    "outcome",
    [
        "FAILED_PLATFORM",
        "CANCELED",
        "REJECTED_POLICY",
        "COMPLETED_UNVERIFIED",
        "ROLLED_BACK",
    ],
)
def test_callback_v2_rejects_every_proofless_non_governed_terminal(
    tmp_path, sealed, outcome
) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    refusal = adapter._refusal(dispatch, "hosted_admission_refused", "refused")
    terminal = HostedTerminalEventV2(
        run_id=dispatch.run_id,
        outcome=outcome,
        report_sha256=refusal.report_sha256,
        started=False,
        uncertain_delivery=False,
    )
    with pytest.raises(ValidationError, match="requires a signed governed terminal"):
        CallbackRequestV2(
            dispatch_id=dispatch.dispatch_id,
            runner_session_id=dispatch.runner_session_id,
            idempotency_key=dispatch.idempotency_key,
            lease_token=dispatch.lease_token,
            flow_release_verification_receipt_object_sha256=(
                dispatch.flow_release_verification_receipt.artifact_sha256
            ),
            workflow_admission_sha256=dispatch.workflow_admission.artifact_sha256,
            events=refusal.evidence_batch + (terminal.model_dump(mode="json"),),
        )


def test_recovery_callback_retains_v2_envelope_and_rejects_embedded_v1_terminal(
    tmp_path, sealed
) -> None:
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
        evidence_batch=tuple(
            hosted.report_events(
                _production_report(),
                run_id=binding.run_id,
                workflow_id=proof.payload.workflow_id,
                bundle_digest=proof.payload.bundle_content_digest,
                authorization_id="test-authorization",
                consequential_steps=1,
                effect_covered_consequential_steps=1,
                terminal_outcome=TransactionOutcome.VERIFIED.value,
            )
        ),
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
        "openadapt.production-terminal-verification/v3"
    )
    assert "params" not in decoded["payload"]
    assert "report" not in decoded["payload"]
    assert callback.runner_session_id == dispatch.runner_session_id
    assert callback.workflow_admission_sha256 == (
        dispatch.workflow_admission.artifact_sha256
    )
    assert callback.flow_release_verification_receipt_object_sha256 == (
        dispatch.flow_release_verification_receipt.artifact_sha256
    )

    mixed = callback.model_dump(mode="python")
    mixed_events = list(mixed["events"])
    mixed_events.insert(
        -1,
        {
            "schema_version": "openadapt.hosted-runner-terminal/v1",
            "run_id": binding.run_id,
            "outcome": "REJECTED_POLICY",
            "report_sha256": proof.payload.run_report_sha256,
            "started": False,
            "uncertain_delivery": False,
            "terminal_verification_artifact_bytes_base64": None,
            "terminal_verification_artifact_sha256": None,
        },
    )
    mixed["events"] = tuple(mixed_events)
    with pytest.raises(ValidationError, match="exactly one terminal"):
        CallbackRequestV2.model_validate(mixed)


@pytest.mark.parametrize(
    ("outcome", "payload_factory", "uncertain_delivery"),
    [
        (
            TransactionOutcome.HALTED_BEFORE_EFFECT,
            _halted_payload,
            False,
        ),
        (
            TransactionOutcome.RECONCILIATION_REQUIRED,
            _reconciliation_payload,
            True,
        ),
    ],
)
def test_recovery_callback_retains_signed_non_success_terminal_v2(
    tmp_path,
    sealed,
    outcome,
    payload_factory,
    uncertain_delivery,
) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    proof = sign_production_terminal_verification(payload_factory(), _private_key())
    binding = adapter.recovery_binding(dispatch).model_copy(
        update={"run_id": proof.payload.run_id}
    )
    result = HostedRunResult(
        dispatch_id=dispatch.dispatch_id,
        run_id=binding.run_id,
        outcome=outcome,
        evidence_batch=tuple(
            hosted.report_events(
                _production_report(),
                run_id=binding.run_id,
                workflow_id=proof.payload.workflow_id,
                bundle_digest=proof.payload.bundle_content_digest,
                authorization_id="test-authorization",
                consequential_steps=1,
                effect_covered_consequential_steps=1,
                terminal_outcome=outcome.value,
            )
        ),
        terminal_verification=proof,
        started=True,
        uncertain_delivery=uncertain_delivery,
        report_sha256=proof.payload.run_report_sha256,
    )

    callback = adapter.callback_request(binding, result)
    terminal = HostedTerminalEvent.model_validate(callback.events[-1])

    assert terminal.outcome == outcome.value
    assert terminal.uncertain_delivery is uncertain_delivery
    assert terminal.terminal_verification_artifact_bytes_base64 is not None
    assert terminal.terminal_verification_artifact_sha256 == proof.artifact_sha256()
    if outcome is TransactionOutcome.HALTED_BEFORE_EFFECT:
        assert proof.payload.permit_count == 0
        assert proof.payload.acknowledged_permit_count == 0
        assert proof.payload.pending_permit_count == 0
        assert proof.payload.permit_chain.entries == ()
        assert all(
            record.absence_basis in {"not_actuated", "verifier_refuted"}
            for record in proof.payload.evidence_manifests.effect.records
        )


def test_safe_halt_callback_requires_signed_terminal_proof() -> None:
    with pytest.raises(ValueError, match="requires exact terminal verification"):
        HostedTerminalEvent(
            run_id=_halted_payload().run_id,
            outcome="HALTED_BEFORE_EFFECT",
            report_sha256="b" * 64,
            started=True,
            uncertain_delivery=False,
        )


def test_terminal_v2_callback_rejects_a_canonical_fake_signature() -> None:
    proof = sign_production_terminal_verification(_payload(), _private_key())
    fake = proof.model_copy(
        update={"signature": urlsafe_b64encode(bytes(64)).decode("ascii").rstrip("=")}
    )
    raw = hosted.canonical_json(fake)

    with pytest.raises(ValueError, match="terminal verification artifact is invalid"):
        HostedTerminalEventV2(
            run_id=fake.payload.run_id,
            outcome="VERIFIED",
            report_sha256=fake.payload.run_report_sha256,
            started=True,
            uncertain_delivery=False,
            terminal_verification_artifact_bytes_base64=b64encode(raw).decode("ascii"),
            terminal_verification_artifact_sha256=fake.artifact_sha256(),
        )


@pytest.mark.parametrize(
    ("payload_factory", "uncertain_delivery"),
    [
        (_reconciliation_payload, False),
        (_acknowledged_reconciliation_payload, True),
    ],
)
def test_terminal_callback_refuses_conflicting_uncertainty_state(
    payload_factory, uncertain_delivery
) -> None:
    proof = sign_production_terminal_verification(payload_factory(), _private_key())
    with pytest.raises(ValueError, match="uncertainty conflicts"):
        HostedTerminalEvent(
            run_id=proof.payload.run_id,
            outcome="RECONCILIATION_REQUIRED",
            report_sha256=proof.payload.run_report_sha256,
            started=True,
            uncertain_delivery=uncertain_delivery,
            terminal_verification_artifact_bytes_base64=b64encode(
                hosted.canonical_json(proof)
            ).decode("ascii"),
            terminal_verification_artifact_sha256=proof.artifact_sha256(),
        )


@pytest.mark.parametrize(
    ("payload_factory", "uncertain_delivery"),
    [
        (_reconciliation_payload, True),
        (_acknowledged_reconciliation_payload, False),
        (_managed_result_loss_payload, True),
        (_managed_result_loss_acknowledged_payload, False),
    ],
)
def test_terminal_callback_derives_uncertainty_from_pending_permit_count(
    payload_factory, uncertain_delivery
) -> None:
    proof = sign_production_terminal_verification(payload_factory(), _private_key())
    terminal = HostedTerminalEvent(
        run_id=proof.payload.run_id,
        outcome="RECONCILIATION_REQUIRED",
        report_sha256=proof.payload.run_report_sha256,
        started=True,
        uncertain_delivery=uncertain_delivery,
        terminal_verification_artifact_bytes_base64=b64encode(
            hosted.canonical_json(proof)
        ).decode("ascii"),
        terminal_verification_artifact_sha256=proof.artifact_sha256(),
    )
    assert terminal.uncertain_delivery is uncertain_delivery


def test_callback_rejects_confirmed_summary_for_reconciliation() -> None:
    proof = sign_production_terminal_verification(
        _reconciliation_payload(), _private_key()
    )
    terminal = HostedTerminalEvent(
        run_id=proof.payload.run_id,
        outcome="RECONCILIATION_REQUIRED",
        report_sha256=proof.payload.run_report_sha256,
        started=True,
        uncertain_delivery=True,
        terminal_verification_artifact_bytes_base64=b64encode(
            hosted.canonical_json(proof)
        ).decode("ascii"),
        terminal_verification_artifact_sha256=proof.artifact_sha256(),
    )
    summary = hosted.report_events(
        _production_report(),
        run_id=proof.payload.run_id,
        workflow_id=proof.payload.workflow_id,
        bundle_digest=proof.payload.bundle_content_digest,
        authorization_id="test-authorization",
        consequential_steps=1,
        effect_covered_consequential_steps=1,
        terminal_outcome="VERIFIED",
    )[-1]

    with pytest.raises(ValidationError, match="conflicts with terminal outcome"):
        CallbackRequest(
            dispatch_id="11111111-1111-4111-8111-111111111111",
            runner_session_id="22222222-2222-4222-8222-222222222222",
            idempotency_key="callback-test-key",
            lease_token="oal_" + "a" * 64,
            flow_release_verification_receipt_object_sha256="sha256:" + "b" * 64,
            workflow_admission_sha256="c" * 64,
            events=(summary, terminal.model_dump(mode="json")),
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
