"""BYOC live authority crosses every process boundary without gaining scope.

All fixtures are public synthetic contracts with real throwaway signatures.
No production account, external service, GUI or live qualification is used.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from openadapt_flow.connector import executor
from openadapt_flow.connector.client import ConnectorClient
from openadapt_flow.connector.config import ConnectorSettings
from openadapt_flow.connector.protocol import (
    MANAGED_DELIVERY_AUTHORITY_CAPABILITY,
    ByocGovernanceError,
    parse_job,
)
from openadapt_flow.connector.storage import InMemoryCustomerStorage
from openadapt_flow.ir import Workflow
from openadapt_flow.jit.authority import PrincipalRegistry
from openadapt_flow.production_qualification import (
    ProductionQualificationGuard,
    load_production_qualification_authority,
)
from openadapt_flow.runner.dispatch_envelope import read_managed_dispatch_envelope
from openadapt_flow.runner.protocol import dispatch_binding_sha256
from openadapt_flow.runtime.authorization import GovernedRunAuthorization
from openadapt_flow.runtime.durable.authority import (
    REMOTE_AUTHORITY_TOKEN_ENV,
    REMOTE_AUTHORITY_URL_ENV,
    REMOTE_DISPATCH_SESSION_ID_ENV,
    DurableAuthority,
    DurableAuthorityBusy,
)
from tests.connector_v3_fixtures import (
    ADMITTER_ID,
    CONTROL_PLANE,
    RUN_ID,
    SESSION_ID,
    archive_tree,
    build_program_case,
    build_qualification_case,
)

OTHER_ID = "ac200000-0000-4000-8000-000000000001"


class CountingStorage(InMemoryCustomerStorage):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.fetches = 0

    def fetch_bundle_archive(self, ref, destination):
        self.fetches += 1
        return super().fetch_bundle_archive(ref, destination)


def _settings(case, **overrides):
    return ConnectorSettings(**{**case["settings_kwargs"], **overrides})


def _run(case, monkeypatch, *, payload=None, settings=None, runner=None):
    # Only the record system is outside this source-contract test. This read
    # helper would perform external I/O; no admission/authority check is stubbed.
    monkeypatch.setattr(executor, "_run_flow_tool", lambda *_args: 1)
    storage = CountingStorage(case["bundle_bytes"])
    seen = []
    failures = []

    def capture(argv, run_dir, env):
        __tracebackhide__ = True
        # Keep unrelated inherited process configuration out of test tracebacks.
        env = {
            key: env[key]
            for key in (
                REMOTE_AUTHORITY_URL_ENV,
                REMOTE_AUTHORITY_TOKEN_ENV,
                REMOTE_DISPATCH_SESSION_ID_ENV,
                "OPENADAPT_JIT_KEY_DIR",
                "OPENADAPT_QUALIFICATION_SIGNERS_JSON",
            )
            if key in env
        }
        seen.append((argv, env))
        if runner is not None:
            try:
                runner(argv, run_dir, env)
            except Exception as exc:
                failures.append(exc)
                raise
        return executor.RunOutcome(
            0,
            {
                "execution_target_kind": "web",
                "success": True,
                "execution_outcome": "VERIFIED",
                "results": [],
            },
        )

    result = executor.execute_job(
        parse_job(payload or case["payload"], lease_job_id="synthetic-lease"),
        settings or _settings(case),
        storage,
        runner=capture,
    )
    if failures:
        raise failures[0]
    return result, storage, seen


def _assert_handoff(case, argv, _run_dir, env):
    assert "--approval-mode" not in argv
    assert "synthetic-run-capability" not in " ".join(argv)
    assert (
        env[REMOTE_AUTHORITY_URL_ENV]
        == case["payload"]["managed_delivery_authority_url"]
    )
    assert env[REMOTE_AUTHORITY_TOKEN_ENV] == "synthetic-run-capability"
    assert env[REMOTE_DISPATCH_SESSION_ID_ENV] == SESSION_ID
    path = Path(argv[argv.index("--managed-dispatch-file") + 1])
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    binding = read_managed_dispatch_envelope(path)
    assert binding.run_id == RUN_ID
    assert (
        binding.authorization.bundle_content_digest
        == case["dispatch"]["bundle_content_digest"]
    )
    assert binding.binding_sha256 == case["dispatch"]["dispatch_binding_sha256"]
    # The consumed extension cannot masquerade as an inner v1 approval.
    assert binding.authorization.qualification_admission is None
    return binding


@pytest.mark.parametrize("mode", ["person", "admitter_key"])
def test_real_program_forms_cross_poll_payload_private_file_and_flow_parse(
    tmp_path, monkeypatch, mode
):
    case = build_program_case(tmp_path, mode)
    requests = []

    def control(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json={"job": {"id": "synthetic-lease", "payload": case["payload"]}}
        )

    with ConnectorClient(
        CONTROL_PLANE,
        token="synthetic-connector-capability",
        transport=httpx.MockTransport(control),
    ) as client:
        leased = client.poll(1)
    assert requests[0]["capabilities"] == [MANAGED_DELIVERY_AUTHORITY_CAPABILITY]
    assert MANAGED_DELIVERY_AUTHORITY_CAPABILITY == "managed_delivery_authority_v3"
    assert leased["payload"] == case["payload"]
    monkeypatch.setenv(REMOTE_AUTHORITY_TOKEN_ENV, "stale-inherited-token")
    monkeypatch.setenv(REMOTE_DISPATCH_SESSION_ID_ENV, OTHER_ID)
    result, storage, seen = _run(
        case,
        monkeypatch,
        payload=leased["payload"],
        runner=lambda *args: _assert_handoff(case, *args),
    )
    assert result.error_code is None
    assert storage.fetches == 1 and len(seen) == 1
    assert list(case["key_dir"].rglob("*.pem")) == []


@pytest.mark.parametrize(
    "fault",
    [
        "missing_session",
        "unsupported_teach_mode",
        "unsupported_unknown_mode",
        "noncanonical_session",
        "missing_authority",
        "wrong_run",
        "wrong_org",
        "foreign_origin",
        "wrong_path",
        "unknown_outer",
        "unknown_extension",
        "unknown_authorization",
        "unknown_nested_authorization",
        "remote_save_override",
        "mixed_basis",
    ],
)
def test_live_scope_faults_refuse_before_fetch_or_child(tmp_path, monkeypatch, fault):
    case = (
        build_qualification_case(tmp_path)
        if fault == "unknown_nested_authorization"
        else build_program_case(tmp_path)
    )
    raw = copy.deepcopy(case["payload"])
    if fault == "missing_session":
        raw.pop("managed_dispatch_session_id")
    elif fault in {"unsupported_teach_mode", "unsupported_unknown_mode"}:
        raw["mode"] = "teach" if fault == "unsupported_teach_mode" else "future-mode"
    elif fault == "noncanonical_session":
        raw["managed_dispatch_session_id"] = SESSION_ID.upper()
    elif fault == "missing_authority":
        raw["managed_dispatch"] = None
        raw["managed_delivery_authority_url"] = None
    elif fault == "wrong_run":
        raw["run_id"] = OTHER_ID
    elif fault == "wrong_org":
        raw["org_id"] = OTHER_ID
    elif fault == "foreign_origin":
        raw["managed_delivery_authority_url"] = (
            "https://other.example.test/api/internal/managed-delivery-permit"
        )
    elif fault == "wrong_path":
        raw["managed_delivery_authority_url"] = CONTROL_PLANE + "/other"
    elif fault == "unknown_outer":
        raw["stronger_authority"] = True
    elif fault == "unknown_extension":
        raw["managed_dispatch"]["stronger_authority"] = True
    elif fault == "unknown_authorization":
        raw["managed_dispatch"]["authorization"]["unbound_approval"] = True
    elif fault == "unknown_nested_authorization":
        requirements = raw["managed_dispatch"]["authorization"][
            "qualified_effect_requirements"
        ]
        assert requirements
        requirements[0]["unbound_requirement"] = True
    elif fault == "remote_save_override":
        raw["managed_dispatch"]["authorization"]["approval_mode"] = "not_gated"
    elif fault == "mixed_basis":
        raw["managed_dispatch"]["qualification_admission"] = {}
    result, storage, seen = _run(case, monkeypatch, payload=raw)
    assert result.status == "failed" and result.error_code == "governance_refused"
    assert storage.fetches == 0 and seen == []


def test_legacy_parse_never_inherits_live_authority_from_environment(
    tmp_path, monkeypatch
):
    case = build_program_case(tmp_path)
    legacy = copy.deepcopy(case["payload"])
    for key in (
        "managed_dispatch",
        "managed_delivery_authority_url",
        "managed_dispatch_session_id",
    ):
        legacy.pop(key)
    monkeypatch.setenv(
        REMOTE_AUTHORITY_URL_ENV, case["payload"]["managed_delivery_authority_url"]
    )
    monkeypatch.setenv(REMOTE_AUTHORITY_TOKEN_ENV, "stale-inherited-token")
    monkeypatch.setenv(REMOTE_DISPATCH_SESSION_ID_ENV, SESSION_ID)
    parsed = parse_job(legacy)
    with pytest.raises(ByocGovernanceError):
        parsed.ensure_governed(require_run_token=False)
    result, storage, seen = _run(case, monkeypatch, payload=legacy)
    assert (
        result.error_code == "governance_refused" and storage.fetches == 0 and not seen
    )


@pytest.mark.parametrize(
    "fault",
    [
        "principal",
        "admitter_key",
        "registry_removed",
        "registry_role",
        "downgrade",
        "primary_key",
        "admission_digest",
    ],
)
def test_admitter_reference_cannot_change_principal_key_role_or_form(
    tmp_path, monkeypatch, fault
):
    case = build_program_case(tmp_path, "admitter_key")
    raw = copy.deepcopy(case["payload"])
    reference = raw["managed_dispatch"]["program_admission"]
    if fault == "principal":
        reference["admitted_by_principal_id"] = "ci:other-admitter"
    elif fault == "admitter_key":
        reference["admitter_public_key_hex"] = "e" * 64
    elif fault == "registry_removed":
        PrincipalRegistry().save(case["key_dir"])
    elif fault == "registry_role":
        PrincipalRegistry(
            agents={ADMITTER_ID: reference["admitter_public_key_hex"]}
        ).save(case["key_dir"])
    elif fault == "downgrade":
        raw["managed_dispatch"]["program_admission"] = {
            k: reference[k]
            for k in ("schema_version", "admission_sha256", "public_key_hex")
        }
        raw["managed_dispatch"]["program_admission"]["schema_version"] = (
            "openadapt.program-admission-reference/v1"
        )
    elif fault == "primary_key":
        reference["public_key_hex"] = "e" * 64
    elif fault == "admission_digest":
        reference["admission_sha256"] = "e" * 64
        authorization = raw["managed_dispatch"]["authorization"]
        authorization["approval_source"] = "hosted:program-admission:" + "e" * 64
        raw["managed_dispatch"]["dispatch_binding_sha256"] = dispatch_binding_sha256(
            RUN_ID, GovernedRunAuthorization.model_validate(authorization)
        )
    settings = _settings(case, trusted_admission_keys=reference["public_key_hex"])
    result, _storage, seen = _run(case, monkeypatch, payload=raw, settings=settings)
    assert result.status == "failed" and not seen
    assert result.error_code == "program_admission_refused"


def test_outer_metadata_cannot_rescue_a_tampered_program_signature(
    tmp_path, monkeypatch
):
    case = build_program_case(tmp_path)
    admission_path = case["bundle"] / "admission.json"
    tampered = json.loads(admission_path.read_text())
    tampered["admitted_by_principal_id"] = "Another Operator"
    admission_path.write_text(json.dumps(tampered))
    case["bundle_bytes"] = archive_tree(case["bundle"])
    raw = copy.deepcopy(case["payload"])
    raw["bundle_sha256"] = hashlib.sha256(case["bundle_bytes"]).hexdigest()
    digest = hashlib.sha256(admission_path.read_bytes()).hexdigest()
    raw["managed_dispatch"]["program_admission"]["admission_sha256"] = digest
    raw["managed_dispatch"]["authorization"]["approval_source"] = (
        "hosted:program-admission:" + digest
    )
    raw["managed_dispatch"]["dispatch_binding_sha256"] = dispatch_binding_sha256(
        RUN_ID,
        GovernedRunAuthorization.model_validate(
            raw["managed_dispatch"]["authorization"]
        ),
    )
    result, storage, seen = _run(case, monkeypatch, payload=raw)
    assert storage.fetches == 1 and not seen
    assert result.error_code == "program_admission_refused"


def test_qualification_v2_uses_independent_private_wrapper_and_real_guard(
    tmp_path, monkeypatch
):
    case = build_qualification_case(tmp_path)
    parsed = parse_job(case["payload"])
    parsed.ensure_governed()
    assert (
        parsed.qualification_admission().payload.schema_version
        == "openadapt.qualification-admission/v2"
    )

    def inspect(argv, run_dir, env):
        _assert_handoff(case, argv, run_dir, env)
        path = Path(argv[argv.index("--qualification-authority-file") + 1])
        assert path != case["authority_file"]
        copied = load_production_qualification_authority(path)
        assert copied == case["operator"]
        ProductionQualificationGuard(path, remote_permit_revalidation=True).verify(
            Workflow.load(argv[argv.index("run") + 1]), for_actuation=False
        )

    result, storage, seen = _run(case, monkeypatch, runner=inspect)
    assert result.error_code is None and storage.fetches == 1 and len(seen) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "missing_context",
        "different_tenant",
        "bad_signature",
        pytest.param(
            "context_public",
            marks=pytest.mark.skipif(
                os.name == "nt",
                reason="POSIX mode refusal; Windows uses the descriptor ACL guard",
            ),
        ),
    ],
)
def test_qualification_cannot_self_assert_or_replace_operator_context(
    tmp_path, monkeypatch, fault
):
    case = build_qualification_case(tmp_path)
    settings = _settings(case)
    raw = copy.deepcopy(case["payload"])
    if fault == "missing_context":
        settings.qualification_authority_file = None
    else:
        operator = json.loads(case["authority_file"].read_text())
        if fault == "different_tenant":
            operator["expected"]["tenant_id"] = OTHER_ID
        elif fault == "bad_signature":
            operator["qualification_admission"]["signature"] = "A" * 86 + "=="
            from openadapt_flow.qualification_admission_v2 import (
                QualificationAdmissionEnvelope,
            )

            proof = QualificationAdmissionEnvelope.model_validate(
                operator["qualification_admission"]
            )
            operator["qualification_admission_sha256"] = proof.artifact_sha256()
            raw["managed_dispatch"]["qualification_admission"] = proof.model_dump(
                mode="json"
            )
            raw["managed_dispatch"]["qualification_admission_sha256"] = (
                proof.artifact_sha256()
            )
        case["authority_file"].write_text(json.dumps(operator))
        case["authority_file"].chmod(
            0o644 if fault == "context_public" and os.name != "nt" else 0o600
        )
    result, _storage, seen = _run(case, monkeypatch, payload=raw, settings=settings)
    assert result.status == "failed" and not seen
    assert result.error_code in {
        "qualification_authority_unavailable",
        "qualification_authority_refused",
    }


@pytest.mark.parametrize("fault", ["none", "different_session", "other_run", "v2"])
def test_source_v3_permission_is_bound_to_handoff_session_before_input(
    tmp_path, monkeypatch, fault
):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from openadapt_flow.terminal_verification_v2 import (
        sign_production_delivery_permit_v3,
    )
    from tests.test_delivery_permit_receiver_freshness import _response

    case = build_program_case(tmp_path)
    issuer_key = Ed25519PrivateKey.generate()
    inputs = []

    def guarded_input(argv, run_dir, env):
        binding = _assert_handoff(case, argv, run_dir, env)
        authority = object.__new__(DurableAuthority)

        def issue(_url, _headers, body):
            request = json.loads(body)
            issued = datetime.now(timezone.utc).replace(microsecond=0)
            if fault == "other_run":
                request["run_id"] = OTHER_ID
            response, retained = _response(
                request,
                checked=issued,
                issued=issued,
                expires=issued + timedelta(seconds=120),
            )
            artifact = sign_production_delivery_permit_v3(
                retained.permit_artifact.payload, issuer_key
            ).canonical_bytes()
            response["permit_artifact_bytes_base64"] = b64encode(artifact).decode()
            response["permit_artifact_sha256"] = hashlib.sha256(artifact).hexdigest()
            response["dispatch_session_id"] = (
                OTHER_ID if fault == "different_session" else SESSION_ID
            )
            if fault == "v2":
                response["schema_version"] = (
                    "openadapt.production-delivery-permit-issue/v2"
                )
            return json.dumps(response).encode()

        authority._remote_transport = issue
        manifest = SimpleNamespace(
            governed_authorization=binding.authorization,
            delivery_authority_kind="cloud_runner",
            remote_delivery_run_id=binding.run_id,
            namespace_id="a" * 32,
            managed_dispatch_binding_sha256=binding.binding_sha256,
        )
        record = SimpleNamespace(
            path_key="c" * 64,
            progress_digest="sha256:" + "d" * 64,
            operation="initial",
            delivery_sequence=0,
        )
        with monkeypatch.context() as context:
            for name in (
                REMOTE_AUTHORITY_URL_ENV,
                REMOTE_AUTHORITY_TOKEN_ENV,
                REMOTE_DISPATCH_SESSION_ID_ENV,
            ):
                context.setenv(name, env[name])
            if fault == "none":
                permit = authority._require_remote_delivery_permit(
                    manifest,
                    record,
                    remote_authority_id=None,
                    remote_delivery_sequence=0,
                    permit_cursor=None,
                )
                authority.require_remote_delivery_fresh(permit)
                inputs.append("synthetic-counted-input")
            else:
                with pytest.raises(DurableAuthorityBusy):
                    authority._require_remote_delivery_permit(
                        manifest,
                        record,
                        remote_authority_id=None,
                        remote_delivery_sequence=0,
                        permit_cursor=None,
                    )

    result, _storage, seen = _run(case, monkeypatch, runner=guarded_input)
    assert result.error_code is None and len(seen) == 1
    assert len(inputs) == (1 if fault == "none" else 0)


def test_local_secret_store_cannot_override_managed_session(tmp_path):
    from openadapt_flow.connector.secrets import LocalSecretStore, SecretStoreError

    local = tmp_path / "input.env"
    local.write_text("OPENADAPT_DURABLE_DISPATCH_SESSION_ID=" + OTHER_ID + "\n")
    local.chmod(0o600)
    with pytest.raises(SecretStoreError, match="invalid"):
        LocalSecretStore(str(tmp_path)).resolve("input")


@pytest.mark.parametrize(
    "field",
    [
        "org_id",
        "workflow_id",
        "bundle_version_id",
        "runtime_validation_id",
        "bundle_sha256",
    ],
)
def test_qualification_proof_binds_the_authenticated_job_tuple(
    tmp_path, monkeypatch, field
):
    case = build_qualification_case(tmp_path)
    raw = copy.deepcopy(case["payload"])
    raw[field] = "e" * 64 if field == "bundle_sha256" else OTHER_ID
    result, storage, seen = _run(case, monkeypatch, payload=raw)
    assert result.error_code == "governance_refused"
    assert storage.fetches == 0 and not seen


@pytest.mark.parametrize("mode", ["person", "admitter_key"])
def test_snapshot_key_fallback_does_not_replace_an_admitter_registry(
    tmp_path, monkeypatch, mode
):
    case = build_program_case(tmp_path, mode)

    def inspect(argv, _run_dir, env):
        trust = Path(env["OPENADAPT_JIT_KEY_DIR"])
        assert trust != case["key_dir"]
        assert sorted(
            p.relative_to(trust).as_posix() for p in trust.rglob("*") if p.is_file()
        ) == ["keys/ed25519.pub"]
        assert (trust / "keys/ed25519.pub").read_text().strip() == case[
            "public_key_hex"
        ]

    result, storage, seen = _run(
        case,
        monkeypatch,
        settings=_settings(case, admission_key_dir=None),
        runner=inspect,
    )
    if mode == "person":
        assert result.error_code is None and len(seen) == 1 and storage.fetches == 1
    else:
        assert result.error_code == "admission_registry_required"
        assert not seen and storage.fetches == 0


@pytest.mark.parametrize(
    "damage", ["principal_removed", "primary_key_removed", "primary_key_changed"]
)
def test_child_uses_live_public_trust_for_current_dispatch_revalidation(
    tmp_path, monkeypatch, damage
):
    from openadapt_flow.jit.dispatch_authority import verify_program_dispatch
    from tests.test_execution_profiles import _TieredVerifier

    case = build_program_case(tmp_path, "admitter_key")

    def inspect(argv, run_dir, env):
        binding = _assert_handoff(case, argv, run_dir, env)
        trust = Path(env["OPENADAPT_JIT_KEY_DIR"])
        assert trust == case["key_dir"]
        bundle = Path(argv[argv.index("run") + 1])
        workflow = Workflow.load(bundle)
        with monkeypatch.context() as child:
            for key in (REMOTE_AUTHORITY_URL_ENV, REMOTE_DISPATCH_SESSION_ID_ENV):
                child.setenv(key, env[key])
            verified = verify_program_dispatch(
                binding,
                bundle_dir=bundle,
                workflow=workflow,
                params={},
                worklists={},
                policy_source="clinical-write",
                effect_verifier=_TieredVerifier(),
                key_dir=trust,
            )
            capability = verified.program_admission_authority
            assert capability is not None
            assert capability.refusal(workflow, verified) is None
            if damage == "principal_removed":
                PrincipalRegistry().save(trust)
            elif damage == "primary_key_removed":
                (trust / "keys/ed25519.pub").unlink()
            else:
                (trust / "keys/ed25519.pub").write_text("e" * 64)
            assert capability.refusal(workflow, verified) is not None

    result, storage, seen = _run(case, monkeypatch, runner=inspect)
    assert result.error_code is None and len(seen) == 1 and storage.fetches == 1
