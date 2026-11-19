"""A managed run of an admitted program delivers through Cloud's v2 permits.

The run authority of a hosted admitted program (``openadapt wrap publish``)
is its signed admission, not a qualification admission
(:mod:`openadapt_flow.jit.hosted_authority`). ``openadapt-flow run`` verifies
that admission once, before the replayer starts. These tests drive the real
runtime :class:`~openadapt_flow.runtime.replayer.Replayer` that ``run`` builds
for a managed dispatch: the Standard profile, a durable run whose delivery
authority is the cloud runner, the managed dispatch binding, and Cloud's v2
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
from openadapt_flow.production_qualification import (
    managed_qualification_edge_refusal,
)
from openadapt_flow.run_gate import build_runtime_authorization
from openadapt_flow.runner.dispatch_envelope import (
    _BINDING_FACTORY,
    ManagedDispatchBinding,
)
from openadapt_flow.runner.protocol import dispatch_binding_sha256
from openadapt_flow.runtime.durable import authority as durable_authority
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.terminal_verification_v2 import (
    ProductionDeliveryPermitPayload,
    ProductionDeliveryReceiptPayload,
    sign_production_delivery_permit,
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
                        "openadapt.production-delivery-acknowledgment-result/v2"
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
        artifact = sign_production_delivery_permit(
            ProductionDeliveryPermitPayload(
                execution_authority_id=AUTHORITY_ID,
                execution_authority_sha256="1" * 64,
                permit_id=permit_id,
                run_id=request["run_id"],
                flow_run_id_sha256=hashlib.sha256(
                    request["run_id"].encode()
                ).hexdigest(),
                run_request_sha256="2" * 64,
                action_request_sha256=hashlib.sha256(body).hexdigest(),
                admission_artifact_sha256=ADMISSION,
                evidence_identity_sha256="4" * 64,
                environment_digest="5" * 64,
                qualification_signer_registry_sha256="6" * 64,
                qualification_signer_registry_revision=1,
                qualification_signer_registry_checked_at=_utc(now),
                qualification_signer_registry_expires_at=_utc(now + timedelta(hours=1)),
                input_edge_sequence=sequence + 1,
                authority_sequence=sequence,
                issued_at=_utc(now),
            ),
            self.key,
        )
        permit_bytes = artifact.canonical_bytes()
        self.retained[claim_id] = (artifact, permit_bytes)
        self.permits += 1
        self.on_permit(self.permits)
        return json.dumps(
            {
                "schema_version": "openadapt.production-delivery-permit-issue/v2",
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


@pytest.fixture(autouse=True)
def _clean_verified_admissions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ha, "_VERIFIED_ADMISSIONS", set())


@pytest.fixture
def issuer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Issuer:
    issuer = Issuer()
    real_init = durable_authority.DurableAuthority.__init__

    def init(self: Any, run_dir: Any, store: Any, *, remote_transport: Any = None):
        real_init(self, run_dir, store, remote_transport=remote_transport or issuer)

    monkeypatch.setattr(durable_authority.DurableAuthority, "__init__", init)
    root = tmp_path.resolve()
    monkeypatch.setenv(
        durable_authority.REMOTE_AUTHORITY_URL_ENV,
        "http://cloud.test/api/internal/managed-delivery-permit",
    )
    monkeypatch.setenv(durable_authority.REMOTE_AUTHORITY_TOKEN_ENV, "run-token")
    monkeypatch.setenv(
        durable_authority.AUTHORITY_DB_ENV, str(root / "authority" / "db.sqlite")
    )
    return issuer


@pytest.fixture
def managed(tmp_path: Path) -> dict[str, Any]:
    """A Standard governed program and its program-admission dispatch."""

    workflow, bundle = _sealed(tmp_path.resolve(), _workflow(), encrypted=True)
    verifier = RecordSystem()
    report = _gate(
        workflow, bundle, ExecutionProfile.STANDARD, verifier=verifier, durable=True
    )
    assert report.passed, report.render()
    authorization = build_runtime_authorization(
        workflow, report, params=PARAMS
    ).model_copy(
        update={"approval_source": ha.approval_source(ADMISSION), "api_steps": ()}
    )
    return {
        "workflow": workflow,
        "bundle": bundle,
        "verifier": verifier,
        "authorization": authorization,
        "run_dir": tmp_path.resolve() / "run",
    }


def _replay(managed: dict[str, Any]) -> Any:
    authorization = managed["authorization"]
    binding = ManagedDispatchBinding(
        _BINDING_FACTORY,
        run_id=RUN_ID,
        authorization=authorization,
        binding_sha256=dispatch_binding_sha256(RUN_ID, authorization),
    )
    replayer = Replayer(
        FakeBackend(),
        vision=_ReadyVision(),
        effect_verifier=managed["verifier"],
        governed_authorization=authorization,
        durable=True,
        checkpoint_key=_KEY,
        require_settled=True,
        delivery_authority_kind="cloud_runner",
        remote_delivery_run_id=RUN_ID,
        managed_dispatch_binding=binding,
    )
    return replayer.run(
        managed["workflow"],
        params=PARAMS,
        bundle_dir=managed["bundle"],
        run_dir=managed["run_dir"],
    )


def _verify(managed: dict[str, Any], content_digest: str | None = None) -> None:
    """What a successful ``managed_admission_refusal`` records."""

    digest = content_digest or managed["workflow"].manifest.content_digest
    ha._VERIFIED_ADMISSIONS.add((ADMISSION, digest))


def test_a_verified_program_admission_delivers_every_input_through_permits(
    managed: dict[str, Any], issuer: Issuer
) -> None:
    _verify(managed)
    report = _replay(managed)
    assert report.success is True, [(r.step_id, r.error) for r in report.results]
    # Every input crossed a Cloud permit and was acknowledged with a receipt.
    assert issuer.permits >= 2
    assert issuer.receipts == issuer.permits


def test_an_unverified_program_admission_refuses_before_any_input(
    managed: dict[str, Any], issuer: Issuer
) -> None:
    with pytest.raises(ValueError, match="Production qualification"):
        _replay(managed)
    assert issuer.permits == 0


def test_an_admission_verified_for_another_bundle_refuses(
    managed: dict[str, Any], issuer: Issuer
) -> None:
    _verify(managed, content_digest="d" * 64)
    with pytest.raises(ValueError, match="Production qualification"):
        _replay(managed)
    assert issuer.permits == 0


def test_an_input_edge_refuses_once_the_verified_admission_is_gone(
    managed: dict[str, Any], issuer: Issuer
) -> None:
    _verify(managed)
    # After the first permit, nothing vouches for the admission any more: the
    # next input edge must refuse instead of trusting the run's start.
    issuer.on_permit = lambda n: ha._VERIFIED_ADMISSIONS.clear() if n == 1 else None
    report = _replay(managed)
    assert report.success is False
    assert issuer.permits == 1


@pytest.mark.parametrize(
    "update",
    [
        {"approval_source": ha.approval_source("e" * 64)},
        {"approval_source": "hosted:managed:00000000"},
        {"execution_profile": "regulated"},
    ],
)
def test_the_edge_check_refuses_an_authorization_it_did_not_verify(
    managed: dict[str, Any], update: dict[str, Any]
) -> None:
    _verify(managed)
    assert (
        managed_qualification_edge_refusal(
            managed["authorization"], managed["workflow"]
        )
        is None
    )
    changed = managed["authorization"].model_copy(update=update)
    assert managed_qualification_edge_refusal(changed, managed["workflow"])


def test_the_edge_check_refuses_a_verified_digest_for_another_bundle(
    managed: dict[str, Any],
) -> None:
    _verify(managed)
    other = managed["authorization"].model_copy(
        update={"bundle_content_digest": "f" * 64}
    )
    assert managed_qualification_edge_refusal(other, managed["workflow"])
