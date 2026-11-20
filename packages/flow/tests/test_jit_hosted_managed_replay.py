"""A managed run of an admitted program delivers through Cloud's v3 permits.

The run authority of a hosted admitted program (``openadapt wrap publish``)
is its signed admission, not a qualification admission
(:mod:`openadapt_flow.jit.hosted_authority`). ``openadapt-flow run`` verifies
that admission once, before the replayer starts. These tests drive the real
runtime :class:`~openadapt_flow.runtime.replayer.Replayer` that ``run`` builds
for a managed dispatch: the Standard profile, a durable run whose delivery
authority is the cloud runner, the managed dispatch binding, and Cloud's v3
delivery permits and receipts. The permits come from an in-memory issuer
signed with a throwaway key, injected as the durable authority's remote
transport. The backend, vision and record system are the scripted ones the
other governed-run tests use. No network.
"""

from __future__ import annotations

import hashlib
import json
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.execution_profiles import ExecutionProfile
from openadapt_flow.jit import hosted_authority as ha
from openadapt_flow.run_gate import build_runtime_authorization
from openadapt_flow.runner.dispatch_envelope import (
    ManagedDispatchBinding,
)
from openadapt_flow.runner.protocol import dispatch_binding_sha256
from openadapt_flow.runtime.durable import authority as durable_authority
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.terminal_verification_v2 import (
    ProductionDeliveryPermitV3Payload,
    ProductionDeliveryReceiptPayload,
    sign_production_delivery_permit_v3,
    sign_production_delivery_receipt,
)
from tests.test_api_step_governed import PARAMS, _workflow
from tests.test_approval_mode_replayer import RecordSystem
from tests.test_execution_profiles import _KEY, _gate, _ReadyVision, _sealed
from tests.test_replayer import FakeBackend

RUN_ID = "00000000-0000-4000-8000-0000000000b7"
ADMISSION = "c" * 64
AUTHORITY_ID = "22222222-2222-4222-8222-222222222222"
SESSION_ID = "30000000-0000-4000-8000-000000000001"


def _utc(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Issuer:
    """Cloud's permit and acknowledgment routes, in memory."""

    def __init__(self) -> None:
        self.admission_digest = ADMISSION
        self.deadline_seconds = 30
        self.uncertain_ack = False
        self.uncertainty_bytes: list[bytes] = []
        self.key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        self.retained: dict[str, tuple[Any, bytes]] = {}
        self.permits = 0
        self.receipts = 0
        self.on_permit: Callable[[int], None] = lambda _n: None

    def __call__(self, url: str, _headers: dict[str, str], body: bytes) -> bytes:
        request = json.loads(body)
        now = datetime.now(timezone.utc)
        if url.endswith("/api/internal/managed-delivery-acknowledgment"):
            artifact, permit_bytes = self.retained[request["one_use_claim_id"]]
            if self.uncertain_ack:
                import time

                time.sleep(4)
                received = _utc(datetime.now(timezone.utc))
                observation = {
                    "schema_version": "openadapt.production-delivery-uncertainty-observation/v1",
                    "status": "uncertain",
                    "observation_id": "50000000-0000-4000-8000-000000000001",
                    "run_id": request["run_id"],
                    "dispatch_session_id": request["dispatch_session_id"],
                    "permit_id": artifact.payload.permit_id,
                    "one_use_claim_id": request["one_use_claim_id"],
                    "runtime_delivery_sequence": request["runtime_delivery_sequence"],
                    "controller_delivered_at": received,
                    "received_at": received,
                }
                raw = json.dumps(observation).encode()
                self.uncertainty_bytes.append(raw)
                from io import BytesIO
                from urllib.error import HTTPError

                raise HTTPError(url, 409, "Conflict", {}, BytesIO(raw))
            delivered = _utc(now)
            receipt = sign_production_delivery_receipt(
                ProductionDeliveryReceiptPayload(
                    execution_authority_id=artifact.payload.execution_authority_id,
                    permit_id=artifact.payload.permit_id,
                    permit_artifact_sha256=hashlib.sha256(permit_bytes).hexdigest(),
                    authenticated_runner_id_sha256="8" * 64,
                    authenticated_session_id_sha256="9" * 64,
                    one_use_claim_id=request["one_use_claim_id"],
                    runtime_delivery_sequence=request["runtime_delivery_sequence"],
                    delivered_at=delivered,
                ),
                self.key,
            )
            receipt_bytes = receipt.canonical_bytes()
            self.receipts += 1
            return json.dumps(
                {
                    "schema_version": (
                        "openadapt.production-delivery-acknowledgment-result/v3"
                    ),
                    "status": "acknowledged",
                    "receipt_artifact_bytes_base64": b64encode(receipt_bytes).decode(),
                    "receipt_artifact_sha256": hashlib.sha256(
                        receipt_bytes
                    ).hexdigest(),
                    "runtime_delivery_sequence": request["runtime_delivery_sequence"],
                    "delivered_at": delivered,
                }
            ).encode()
        sequence = request["remote_delivery_sequence"]
        permit_id = f"20000000-0000-4000-8000-{sequence + 1:012d}"
        claim_id = f"40000000-0000-4000-8000-{sequence + 1:012d}"
        artifact = sign_production_delivery_permit_v3(
            ProductionDeliveryPermitV3Payload(
                execution_authority_id=AUTHORITY_ID,
                execution_authority_sha256="1" * 64,
                permit_id=permit_id,
                run_id=request["run_id"],
                flow_run_id_sha256=hashlib.sha256(
                    request["run_id"].encode()
                ).hexdigest(),
                run_request_sha256="2" * 64,
                action_request_sha256=hashlib.sha256(body).hexdigest(),
                admission_artifact_sha256=self.admission_digest,
                evidence_identity_sha256="4" * 64,
                environment_digest="5" * 64,
                qualification_signer_registry_sha256="6" * 64,
                qualification_signer_registry_revision=1,
                qualification_signer_registry_checked_at=_utc(now),
                qualification_signer_registry_expires_at=_utc(now + timedelta(hours=1)),
                input_edge_sequence=sequence + 1,
                authority_sequence=sequence,
                issued_at=_utc(now),
                delivery_not_after=_utc(now + timedelta(seconds=self.deadline_seconds)),
            ),
            self.key,
        )
        permit_bytes = artifact.canonical_bytes()
        self.retained[claim_id] = (artifact, permit_bytes)
        self.permits += 1
        self.on_permit(self.permits)
        return json.dumps(
            {
                "schema_version": "openadapt.production-delivery-permit-issue/v3",
                "status": "issued",
                "execution_authority_id": AUTHORITY_ID,
                "permit_id": permit_id,
                "dispatch_session_id": SESSION_ID,
                "one_use_claim_id": claim_id,
                "permit_artifact_bytes_base64": b64encode(permit_bytes).decode(),
                "permit_artifact_sha256": hashlib.sha256(permit_bytes).hexdigest(),
                "next_permit_cursor": hashlib.sha256(
                    f"cursor:{sequence}".encode()
                ).hexdigest(),
                "input_edge_sequence": sequence + 1,
                "authority_sequence": sequence,
                "runtime_delivery_sequence": sequence,
            }
        ).encode()


def _make_managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, workflow: Any = None
) -> dict[str, Any]:
    """A real signed program admission, run gate and dispatch; external I/O is scripted."""
    from openadapt_flow import crypto
    from tests.test_jit_hosted_authority import CHECKS, _admit, _host, _keys

    monkeypatch.setenv(crypto.ENV_KEY, _KEY)
    monkeypatch.setenv("OPENADAPT_DURABLE_DISPATCH_SESSION_ID", SESSION_ID)
    monkeypatch.setenv(
        "OPENADAPT_DURABLE_AUTHORITY_URL",
        "https://cloud.test/api/internal/managed-delivery-permit",
    )
    workflow, bundle = _sealed(
        tmp_path.resolve(),
        _workflow() if workflow is None else workflow,
        encrypted=True,
    )
    readback = bundle / ha.READBACK_PATH
    readback.parent.mkdir(parents=True)
    readback.write_text(CHECKS)
    keys = _keys(tmp_path / "keys")
    monkeypatch.setenv("OPENADAPT_JIT_KEY_DIR", str(keys))
    _admit(
        bundle,
        keys,
        params=sorted(workflow.params),
        canary={"target_origin": "https://app.example.com"},
    )
    _host(bundle, keys)
    digest = ha.admission_sha256(bundle)
    verifier = RecordSystem()
    report = _gate(
        workflow, bundle, ExecutionProfile.STANDARD, verifier=verifier, durable=True
    )
    assert report.passed, report.render()
    authorization = build_runtime_authorization(
        workflow, report, params=PARAMS
    ).model_copy(
        update={"approval_source": ha.approval_source(digest), "api_steps": ()}
    )
    return {
        "workflow": workflow,
        "bundle": bundle,
        "keys": keys,
        "digest": digest,
        "verifier": verifier,
        "authorization": authorization,
        "run_dir": tmp_path.resolve() / "run",
        "backends": [],
    }


@pytest.fixture
def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return _make_managed(tmp_path, monkeypatch)


@pytest.fixture
def issuer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, managed: dict[str, Any]
) -> Issuer:
    issuer = Issuer()
    issuer.admission_digest = managed["digest"]
    real_init = durable_authority.DurableAuthority.__init__

    def init(self: Any, run_dir: Any, store: Any, *, remote_transport: Any = None):
        real_init(self, run_dir, store, remote_transport=remote_transport or issuer)

    monkeypatch.setattr(durable_authority.DurableAuthority, "__init__", init)
    real_ack = durable_authority.DurableAuthority.acknowledge_remote_delivery
    issuer.ack_errors = []

    def ack(self, *args, **kwargs):
        try:
            return real_ack(self, *args, **kwargs)
        except Exception as exc:
            issuer.ack_errors.append(str(exc))
            raise

    monkeypatch.setattr(
        durable_authority.DurableAuthority, "acknowledge_remote_delivery", ack
    )
    monkeypatch.setenv(durable_authority.REMOTE_AUTHORITY_TOKEN_ENV, "run-token")
    monkeypatch.setenv(
        durable_authority.AUTHORITY_DB_ENV,
        str(tmp_path.resolve() / "authority" / "db.sqlite"),
    )
    return issuer


def _binding(
    managed: dict[str, Any], *, verified: bool = True, **updates: Any
) -> ManagedDispatchBinding:
    from openadapt_flow.jit.dispatch_authority import verify_program_dispatch

    authorization = managed["authorization"].model_copy(update=updates)
    from uuid import uuid4

    from openadapt_flow.runner.dispatch_envelope import (
        ManagedDispatchEnvelope,
        read_managed_dispatch_envelope,
        write_managed_dispatch_envelope_value,
    )

    envelope = ManagedDispatchEnvelope(
        run_id=RUN_ID,
        authorization=authorization,
        bundle_content_digest=authorization.bundle_content_digest,
        runtime_inputs_digest=authorization.runtime_inputs_digest,
        dispatch_binding_sha256=dispatch_binding_sha256(RUN_ID, authorization),
    )
    path = managed["run_dir"].parent / f"dispatch-{uuid4().hex}.json"
    binding = read_managed_dispatch_envelope(
        write_managed_dispatch_envelope_value(path, envelope)
    )
    if not verified:
        return binding
    return verify_program_dispatch(
        binding,
        bundle_dir=managed["bundle"],
        workflow=managed["workflow"],
        params=PARAMS,
        worklists={},
        policy_source="permissive",
        effect_verifier=managed["verifier"],
        key_dir=managed["keys"],
    )


def _replay(
    managed: dict[str, Any],
    binding: ManagedDispatchBinding,
    *,
    entry: str = "runtime",
    params: Any = None,
) -> Any:
    from openadapt_flow.deployment import build_replayer

    authorization = binding.authorization
    backend = FakeBackend()
    managed["backends"].append(backend)
    options = dict(
        effect_verifier=managed["verifier"],
        governed_authorization=authorization,
        durable=True,
        checkpoint_key=_KEY,
        delivery_authority_kind="cloud_runner",
        remote_delivery_run_id=binding.run_id,
        managed_dispatch_binding=binding,
    )
    if entry == "runtime":
        replayer = Replayer(
            backend,
            vision=_ReadyVision(),
            require_settled=True,
            web_form_actuator=managed.get("web_form_actuator"),
            **options,
        )
    else:
        replayer = build_replayer(
            backend,
            allow_egress=False,
            api_actuator=None,
            use_structural=True,
            **options,
        )
        replayer.vision = _ReadyVision()
    return replayer.run(
        managed["workflow"],
        params=PARAMS if params is None else params,
        bundle_dir=managed["bundle"],
        run_dir=managed["run_dir"],
        execution_origin="https://app.example.com",
    )


@pytest.mark.parametrize("entry", ["runtime", "deployment"])
def test_signed_admission_runs_with_a_verified_dispatch(
    managed: dict[str, Any], issuer: Issuer, entry: str
) -> None:
    binding = _binding(managed)
    report = _replay(managed, binding, entry=entry)
    assert report.success, [(r.step_id, r.error) for r in report.results]
    assert issuer.permits >= 2 and issuer.receipts == issuer.permits


@pytest.mark.parametrize("entry", ["runtime", "deployment"])
def test_a_prior_verification_never_authorizes_another_dispatch(
    managed: dict[str, Any], issuer: Issuer, entry: str
) -> None:
    _binding(
        managed
    )  # A legitimate verification in the same process grants no ambient authority.
    from uuid import uuid4

    other = _binding(
        managed,
        verified=False,
        authorization_id=uuid4().hex,
        admitted_policy_contract_sha256="a" * 64,
    )
    with pytest.raises(ValueError, match="Production qualification"):
        _replay(managed, other, entry=entry)
    assert issuer.permits == 0
    assert all(not backend.actions for backend in managed["backends"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("admitted_policy_contract_sha256", "a" * 64),
        ("runtime_inputs_digest", "b" * 64),
        ("bundle_content_digest", "c" * 64),
        ("execution_profile", "regulated"),
        ("approval_source", ha.approval_source("d" * 64)),
    ],
)
def test_verification_rejects_a_dispatch_that_does_not_match_the_local_gate(
    managed: dict[str, Any], field: str, value: Any
) -> None:
    from openadapt_flow.jit.hosted_authority import HostedAuthorityError

    with pytest.raises(HostedAuthorityError):
        _binding(managed, **{field: value})


@pytest.mark.parametrize("entry", ["runtime", "deployment"])
@pytest.mark.parametrize(
    "damage",
    [
        "admission",
        "readback",
        "registry",
        "session",
        "signer_registry",
        "workflow",
        "params",
    ],
)
def test_changed_authority_refuses_before_any_permit(
    managed: dict[str, Any],
    issuer: Issuer,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    damage: str,
) -> None:
    binding = _binding(managed)
    params = PARAMS
    if damage == "admission":
        (managed["bundle"] / "admission.json").unlink()
    elif damage == "readback":
        (managed["bundle"] / ha.READBACK_PATH).write_text("verifier: {kind: none}")
    elif damage == "registry":
        (managed["keys"] / "principals.json").write_text(
            '{"schema":"openadapt-flow/jit-principals/v1","admitter_keys":{"other":"'
            + "1" * 64
            + '"}}'
        )
    elif damage == "session":
        monkeypatch.setenv(
            "OPENADAPT_DURABLE_DISPATCH_SESSION_ID",
            "30000000-0000-4000-8000-000000000002",
        )
    elif damage == "signer_registry":
        monkeypatch.setenv(
            "OPENADAPT_QUALIFICATION_SIGNERS_JSON", "changed trust input"
        )
    elif damage == "workflow":
        managed["workflow"].steps[0].intent = "changed after verification"
    else:
        params = {"lname": "Another"}
    with pytest.raises(ValueError):
        _replay(managed, binding, entry=entry, params=params)
    assert issuer.permits == 0
    assert all(not backend.actions for backend in managed["backends"])


def test_the_verified_handoff_is_single_use(
    managed: dict[str, Any], issuer: Issuer
) -> None:
    binding = _binding(managed)
    first = _replay(managed, binding)
    assert first.success
    count = issuer.permits
    with pytest.raises(ValueError, match="already consumed"):
        _replay(managed, binding)
    assert issuer.permits == count


def test_an_input_edge_rechecks_trust_after_the_first_input(
    managed: dict[str, Any], issuer: Issuer
) -> None:
    binding = _binding(managed)
    issuer.on_permit = lambda n: (
        (managed["bundle"] / "admission.json").unlink() if n == 1 else None
    )
    report = _replay(managed, binding)
    assert not report.success
    assert issuer.permits == 1


@pytest.mark.parametrize("verified", [True, False])
def test_cli_replay_consumes_the_same_verified_dispatch_authority(
    managed: dict[str, Any],
    issuer: Issuer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verified: bool,
) -> None:
    import playwright.sync_api as playwright

    import openadapt_flow._browser_setup as browser_setup
    import openadapt_flow.backends.playwright_backend as browser_backend
    from openadapt_flow import __main__ as cli
    from openadapt_flow.backends import factory
    from openadapt_flow.deployment import (
        DeploymentConfig,
        PolicySection,
        RuntimeSection,
    )
    from tests.test_cli_new_commands import _FakePlaywright

    binding = _binding(managed, verified=verified)
    params = tmp_path / "params.json"
    params.write_text(json.dumps(PARAMS))
    args = cli.build_parser().parse_args(
        [
            "replay",
            str(managed["bundle"]),
            "--run-dir",
            str(managed["run_dir"]),
            "--params-file",
            str(params),
            "--backend",
            "web",
            "--url",
            "https://app.example.com/",
        ]
    )
    args._governed_run_authorization = binding.authorization
    args._managed_dispatch_binding = binding
    args._delivery_authority_kind = "cloud_runner"
    args._remote_delivery_run_id = binding.run_id
    cfg = DeploymentConfig(
        policy=PolicySection(policy="permissive"),
        runtime=RuntimeSection(durable=True, require_settled=True),
    )
    monkeypatch.setattr(
        cli,
        "_deployment_runtime",
        lambda *_a, **_k: (cfg, managed["verifier"], None, True, False),
    )
    monkeypatch.setattr(playwright, "sync_playwright", _FakePlaywright)
    monkeypatch.setattr(
        browser_setup,
        "launch_chromium",
        lambda *_a, **_k: _FakePlaywright().chromium.launch(),
    )
    monkeypatch.setattr(browser_backend, "prepare_ws_guard", lambda _page: None)

    def backend_for_cli(*_a: Any, **_k: Any) -> FakeBackend:
        backend = FakeBackend()
        managed["backends"].append(backend)
        return backend

    monkeypatch.setattr(factory, "build_backend", backend_for_cli)
    configured = cli._configured_replayer

    def configure(*a: Any, **kw: Any) -> Replayer:
        replayer = configured(*a, **kw)
        replayer.vision = _ReadyVision()
        return replayer

    monkeypatch.setattr(cli, "_configured_replayer", configure)
    if not verified:
        with pytest.raises(ValueError, match="Production qualification"):
            cli._cmd_replay(args)
        assert issuer.permits == 0
        assert all(not backend.actions for backend in managed["backends"])
    else:
        assert cli._cmd_replay(args) == 0
        assert issuer.permits >= 2 and issuer.receipts == issuer.permits


def test_a_late_non_authorizing_ack_is_kept_and_no_next_input_runs(
    managed: dict[str, Any],
    issuer: Issuer,
) -> None:
    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore

    issuer.deadline_seconds = 3
    issuer.uncertain_ack = True
    result = _replay(managed, _binding(managed))
    assert not result.success
    assert result.execution_outcome != "VERIFIED"
    assert result.transaction_outcome == "RECONCILIATION_REQUIRED"
    assert issuer.permits == 1 and issuer.receipts == 0
    assert sum(len(backend.actions) for backend in managed["backends"]) == 1
    store = CheckpointStore(managed["run_dir"], key=_KEY)
    authority = durable_authority.DurableAuthority(managed["run_dir"], store)
    assert authority.production_delivery_uncertainty_observations() == tuple(
        issuer.uncertainty_bytes
    )


def test_permission_expiring_after_the_precheck_never_reaches_backend_input(
    managed: dict[str, Any],
    issuer: Issuer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    issuer.deadline_seconds = 3
    original = Replayer._deliver_backend_call

    def delayed(self, result, call):
        # The response was valid when received. Slow preparation is not a
        # license to use it after its signed start deadline.
        time.sleep(4)
        return original(self, result, call)

    monkeypatch.setattr(Replayer, "_deliver_backend_call", delayed)
    result = _replay(managed, _binding(managed))
    assert not result.success and result.execution_outcome != "VERIFIED"
    assert issuer.permits == 1 and issuer.receipts == 0
    assert all(not backend.actions for backend in managed["backends"])


@pytest.mark.parametrize("delay_at", [None, "structural", "qualification", "form"])
def test_the_api_input_boundary_uses_the_same_signed_start_deadline(
    managed: dict[str, Any],
    issuer: Issuer,
    monkeypatch: pytest.MonkeyPatch,
    delay_at: str | None,
) -> None:
    import time

    from openadapt_flow.runtime.actuators.web_form import WebFormActuator
    from tests.test_api_step_governed import Transport

    gate = _gate(
        managed["workflow"],
        managed["bundle"],
        ExecutionProfile.STANDARD,
        verifier=managed["verifier"],
        durable=True,
    )
    managed["authorization"] = build_runtime_authorization(
        managed["workflow"],
        gate,
        params=PARAMS,
    ).model_copy(update={"approval_source": ha.approval_source(managed["digest"])})
    transport = Transport()
    managed["web_form_actuator"] = WebFormActuator(transport)
    if delay_at:
        issuer.deadline_seconds = 3
        if delay_at == "form":
            original_get = transport.get

            def delayed_get(path):
                time.sleep(4)
                return original_get(path)

            monkeypatch.setattr(transport, "get", delayed_get)
        else:
            method = (
                "_structural_state"
                if delay_at == "structural"
                else "_require_qualification_environment_current"
            )
            original = getattr(Replayer, method)

            def delayed(self):
                if self._active_delivery_acknowledgers:
                    time.sleep(4)
                return original(self)

            monkeypatch.setattr(Replayer, method, delayed)
    result = _replay(managed, _binding(managed))
    assert all(not backend.actions for backend in managed["backends"])
    if delay_at in {"structural", "qualification"}:
        assert not result.success and result.execution_outcome != "VERIFIED"
        assert transport.posts == [] and issuer.permits == 1 and issuer.receipts == 0
    else:
        assert result.success, (
            issuer.ack_errors,
            [(r.error, r.effect_results) for r in result.results],
        )
        assert (
            len(transport.posts) == 1 and issuer.permits == 1 and issuer.receipts == 1
        )


@pytest.mark.parametrize("changed", ["registry", "workflow", "session"])
def test_scope_is_rechecked_after_a_permit_and_before_the_backend_call(
    managed: dict[str, Any],
    issuer: Issuer,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    original = Replayer._deliver_backend_call

    def between_check_and_input(self, result, call):
        if changed == "registry":
            (managed["keys"] / "principals.json").write_text(
                '{"schema":"openadapt-flow/jit-principals/v1","admitter_keys":{"other":"'
                + "1" * 64
                + '"}}'
            )
        elif changed == "workflow":
            managed["workflow"].steps[0].intent = "another task after the check"
        else:
            monkeypatch.setenv(
                durable_authority.REMOTE_DISPATCH_SESSION_ID_ENV,
                "30000000-0000-4000-8000-000000000002",
            )
        return original(self, result, call)

    monkeypatch.setattr(Replayer, "_deliver_backend_call", between_check_and_input)
    result = _replay(managed, _binding(managed))
    assert not result.success and result.execution_outcome != "VERIFIED"
    assert issuer.permits == 1 and issuer.receipts == 0
    assert all(not backend.actions for backend in managed["backends"])


@pytest.mark.parametrize("crash_after", [None, "grant", 1, 2])
def test_each_api_request_has_protected_write_ahead_and_cannot_repeat_after_crash(
    tmp_path: Path,
    managed: dict[str, Any],
    issuer: Issuer,
    monkeypatch: pytest.MonkeyPatch,
    crash_after: str | int | None,
) -> None:
    from openadapt_flow.compiler.api_proposal import admit_api_step
    from openadapt_flow.ir import WebFormWrite
    from openadapt_flow.runtime.actuators.form_eval import FormEvaluation
    from openadapt_flow.runtime.actuators.web_form import (
        WebFormActuator,
        folded_checks_digest,
        page_scripts,
    )
    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
    from tests.test_api_step_governed import FORM, Transport, _Resp

    class ProcessStopped(BaseException):
        pass

    workflow = _workflow(admitted=False)
    sequence_form = FORM + "<script>fetch('/draft.php', {method: 'POST'});</script>"
    typing, save = workflow.steps
    typing = typing.model_copy(
        update={
            "effects": list(save.effects),
            "anchor": save.anchor,
            "identity_armed": True,
        }
    )
    spec = save.api_binding.web_form.model_copy(
        update={
            "writes": [
                WebFormWrite(
                    role="step",
                    method="POST",
                    path="/draft.php",
                    from_step=typing.id,
                    fields=list(save.api_binding.web_form.fields),
                    expected_status=[200],
                    evidence="synthetic recorded step request",
                )
            ],
            "folded_checks_digest": folded_checks_digest([typing, save]),
            "client_scripts": page_scripts(
                sequence_form, "/save.php", ["csrf", "lname"], {}
            ),
        }
    )
    save = save.model_copy(
        update={
            "api_binding": save.api_binding.model_copy(
                update={
                    "web_form": spec,
                    "effects": list(typing.effects) + list(save.effects),
                }
            )
        }
    )
    workflow, _ = admit_api_step(
        workflow.model_copy(update={"steps": [typing, save]}),
        admitted_by="reviewer",
        identity={"subject_name": "lname"},
    )
    managed = _make_managed(tmp_path / "sequence", monkeypatch, workflow=workflow)
    issuer.admission_digest = managed["digest"]
    gate = _gate(
        managed["workflow"],
        managed["bundle"],
        ExecutionProfile.STANDARD,
        verifier=managed["verifier"],
        durable=True,
    )
    managed["authorization"] = build_runtime_authorization(
        managed["workflow"], gate, params=PARAMS
    ).model_copy(update={"approval_source": ha.approval_source(managed["digest"])})

    class Sequence(Transport):
        def get(self, _path):
            return _Resp(200, sequence_form)

        def evaluate_form(self, _form, _action, fields, **_kwargs):
            body = [("csrf", "session-1"), *fields]
            return FormEvaluation(
                status="valid",
                serialized=body,
                via="submit",
                writes=[("POST /draft.php", body)],
            )

        def post_form(self, path, body, *, referer):
            # The durable grant and all previous exact receipts must exist
            # before this transport receives any request bytes.
            store = CheckpointStore(managed["run_dir"], key=_KEY)
            authority = durable_authority.DurableAuthority(managed["run_dir"], store)
            with authority._transaction() as connection:
                pending = authority._pending_remote_delivery(connection)
                assert pending is not None
                assert pending.runtime_delivery_sequence == len(self.posts)
                assert issuer.receipts == len(self.posts)
                assert issuer.permits == len(self.posts) + 1
            response = super().post_form(path, body, referer=referer)
            if crash_after == len(self.posts):
                raise ProcessStopped()
            return response

    transport = Sequence()
    managed["web_form_actuator"] = WebFormActuator(transport)
    if crash_after == "grant":
        # Stop after the protected grant is retained, before its input call.
        def stop_before_input(self, result, call):
            assert issuer.permits == 1 and issuer.receipts == 0
            raise ProcessStopped()

        monkeypatch.setattr(Replayer, "_deliver_backend_call", stop_before_input)
    if crash_after is None:
        report = _replay(managed, _binding(managed))
        assert report.success, [(r.error, r.effect_results) for r in report.results]
        assert report.results[0].api_fallback is None, report.results[0].api_fallback
        assert [path for path, _ in transport.posts] == ["/draft.php", "/save.php"]
        assert issuer.permits == issuer.receipts == 2
    else:
        with pytest.raises(ProcessStopped):
            _replay(managed, _binding(managed))
        count = 0 if crash_after == "grant" else crash_after
        assert len(transport.posts) == count
        assert issuer.permits == (1 if crash_after == "grant" else count)
        assert issuer.receipts == max(0, count - 1)
        store = CheckpointStore(managed["run_dir"], key=_KEY)
        authority = durable_authority.DurableAuthority(managed["run_dir"], store)
        with pytest.raises(
            durable_authority.DurableAuthorityBusy, match="prior.*acknowledgment"
        ):
            authority.before_initial_delivery(store.read_manifest())
        # Public replay cannot adopt the interrupted durable identity either.
        from openadapt_flow.runtime.durable.approval import StateDiverged

        with pytest.raises(
            (ValueError, StateDiverged, durable_authority.DurableAuthorityBusy)
        ):
            _replay(managed, _binding(managed))
        assert len(transport.posts) == count


def test_a_deployment_subclass_cannot_accidentally_skip_the_managed_request_guard(
    managed: dict[str, Any],
    issuer: Issuer,
) -> None:
    from openadapt_flow.runtime.actuators.api import ActuationStatus, ApiActuationResult
    from openadapt_flow.runtime.actuators.web_form import WebFormActuator
    from tests.test_api_step_governed import Transport

    sent = []

    class DeploymentAdapter(WebFormActuator):
        def send(self, binding, prepared, **_kwargs):
            sent.append(prepared)
            return ApiActuationResult(status=ActuationStatus.ACTUATED)

    gate = _gate(
        managed["workflow"],
        managed["bundle"],
        ExecutionProfile.STANDARD,
        verifier=managed["verifier"],
        durable=True,
    )
    managed["authorization"] = build_runtime_authorization(
        managed["workflow"],
        gate,
        params=PARAMS,
    ).model_copy(update={"approval_source": ha.approval_source(managed["digest"])})
    transport = Transport()
    managed["web_form_actuator"] = DeploymentAdapter(transport)
    report = _replay(managed, _binding(managed))
    assert not report.success and report.execution_outcome != "VERIFIED"
    assert sent == [] and transport.posts == []
    assert issuer.permits == 1 and issuer.receipts == 0
    assert all(not backend.actions for backend in managed["backends"])
