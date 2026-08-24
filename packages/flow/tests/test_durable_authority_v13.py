"""External monotonic authority contracts for durable continuation v13.

These tests keep the SQLite authority outside the copyable run directory.  A
restore of local evidence must therefore never restore permission to continue.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from base64 import b64encode
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import openadapt_flow.runtime.durable.authority as durable_authority_module
from openadapt_flow.ir import ActionKind, RunReport, Step, StepResult, Workflow
from openadapt_flow.runtime.authorization import GovernedRunAuthorization
from openadapt_flow.runtime.durable.approval import (
    StateDiverged,
    approval_pause_digest,
)
from openadapt_flow.runtime.durable.attended import (
    AttendedActionStore,
    AttendedDecision,
    AttendedDecisionLog,
    issue_attended_capability,
)
from openadapt_flow.runtime.durable.authority import (
    AUTHORITY_DB_ENV,
    REMOTE_AUTHORITY_TOKEN_ENV,
    REMOTE_AUTHORITY_URL_ENV,
    SYNTHETIC_DELIVERY_MARKER_ENABLED_ENV,
    SYNTHETIC_DELIVERY_MARKER_RUN_ID_ENV,
    DurableAuthority,
    DurableAuthorityBusy,
)
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    PendingEscalation,
    RunCheckpoint,
    RunManifest,
)
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.terminal_verification_v2 import (
    ProductionDeliveryPermitPayload,
    ProductionDeliveryReceiptPayload,
    sign_production_delivery_permit,
    sign_production_delivery_receipt,
)


def _parse_permit_utc(value: str) -> datetime:
    """Parse a canonical ``...Z`` permit timestamp on every supported Python.

    ``datetime.fromisoformat`` accepts a ``Z`` suffix only from 3.11, and this
    package supports 3.10. Normalize the offset exactly as the product-side
    permit parser does.
    """

    return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def _isolated_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test its own external authority database."""

    monkeypatch.setenv(
        AUTHORITY_DB_ENV,
        str(tmp_path / "external-authority" / "authority.sqlite3"),
    )


def _fresh_run(
    tmp_path: Path,
    *,
    execution_profile: Literal["demo", "standard", "regulated"] | None = None,
    run_id: str = "11111111-1111-4111-8111-111111111111",
    namespace_id: str = "0123456789abcdef0123456789abcdef",
    delivery_authority_kind: Literal["customer_local", "cloud_runner"] | None = None,
) -> tuple[
    Path,
    CheckpointStore,
    RunManifest,
    DurableAuthority,
]:
    run_dir = tmp_path / "run"
    bundle_dir = tmp_path / "bundle"
    Workflow(
        name="authority-contract",
        steps=[
            Step(
                id="write-case",
                intent="write the case",
                action=ActionKind.KEY,
                key="ENTER",
            )
        ],
    ).save(bundle_dir)
    store = CheckpointStore(run_dir)
    authorization = (
        GovernedRunAuthorization(
            bundle_content_digest="a" * 64,
            runtime_inputs_digest="b" * 64,
            admitted_policy_name="test",
            execution_profile=execution_profile,
        )
        if execution_profile is not None
        else None
    )
    managed = (
        delivery_authority_kind
        if delivery_authority_kind is not None
        else (
            "cloud_runner"
            if execution_profile in {"standard", "regulated"}
            else "customer_local"
        )
    )
    manifest = RunManifest(
        run_id=run_id,
        namespace_id=namespace_id,
        canonical_run_dir=str(run_dir.resolve()),
        workflow_name="authority-contract",
        bundle_dir=str(bundle_dir.resolve()),
        params={"case": "A-100"},
        governed_authorization=authorization,
        delivery_authority_kind=managed,
        remote_delivery_run_id=(run_id if managed == "cloud_runner" else None),
        managed_dispatch_binding_sha256=(
            "sha256:" + "c" * 64 if managed == "cloud_runner" else None
        ),
    )
    store.write_fresh_manifest(manifest)
    authority = DurableAuthority(run_dir, store)
    assert authority.validate(manifest).phase == "active"
    return run_dir, store, manifest, authority


def _replace_pending_table_with_legacy_schema(
    authority: DurableAuthority,
    *,
    populated: bool,
) -> None:
    with sqlite3.connect(authority.db_path) as connection:
        connection.execute("DROP TABLE remote_delivery_pending")
        connection.execute(
            """
            CREATE TABLE remote_delivery_pending (
                path_key TEXT PRIMARY KEY,
                authority_id TEXT NOT NULL,
                authority_signer_sha256 TEXT NOT NULL,
                permit_id TEXT NOT NULL,
                dispatch_session_id TEXT NOT NULL,
                one_use_claim_id TEXT NOT NULL,
                next_sequence INTEGER NOT NULL,
                cursor_secret TEXT NOT NULL,
                input_edge_sequence INTEGER NOT NULL,
                authority_sequence INTEGER NOT NULL,
                runtime_delivery_sequence INTEGER NOT NULL,
                permit_artifact_sha256 TEXT NOT NULL,
                permit_artifact_bytes BLOB NOT NULL
            )
            """
        )
        if populated:
            connection.execute(
                """
                INSERT INTO remote_delivery_pending VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    authority.path_key,
                    "00000000-0000-4000-8000-000000000008",
                    "a" * 64,
                    "20000000-0000-4000-8000-000000000001",
                    "30000000-0000-4000-8000-000000000001",
                    "40000000-0000-4000-8000-000000000001",
                    1,
                    "b" * 64,
                    1,
                    0,
                    0,
                    "c" * 64,
                    b"{}",
                ),
            )


def _pause(
    store: CheckpointStore,
    manifest: RunManifest,
    authority: DurableAuthority,
) -> PendingEscalation:
    prior = authority.validate(manifest)
    pending = PendingEscalation(
        run_id=manifest.run_id,
        workflow_name=manifest.workflow_name,
        step_index=0,
        step_id="write-case",
        intent="write the case",
        category="human_required",
        reason="operator review is required",
        proposed_options=["continue", "reject"],
        resume_from_index=0,
        params=dict(manifest.params),
    )
    store.write_pending(pending)
    authority.advance(
        manifest,
        expected_progress_digest=prior.progress_digest,
        phase="paused",
        pause_binding_sha256=approval_pause_digest(pending),
    )
    assert authority.validate(manifest).phase == "paused"
    return pending


def _acquire(
    authority: DurableAuthority,
    manifest: RunManifest,
    pending: PendingEscalation,
    *,
    attempt: str,
    now: datetime,
    ttl_s: float = 60.0,
) -> str:
    owner = f"sha256:owner-{attempt}"
    authority.acquire(
        manifest,
        pause_binding_sha256=approval_pause_digest(pending),
        attempt_id=attempt,
        operation="continue",
        owner_nonce_sha256=owner,
        acquired_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_s)).isoformat(),
        now=now,
    )
    return owner


def _snapshot(run_dir: Path, target: Path) -> None:
    shutil.copytree(run_dir, target)


def _remote_ready_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: object
) -> tuple[RunManifest, DurableAuthority, str]:
    _run_dir, store, manifest, _authority = _fresh_run(
        tmp_path, execution_profile="standard"
    )
    pending = _pause(store, manifest, DurableAuthority(store.run_dir, store))
    authority = DurableAuthority(store.run_dir, store, remote_transport=transport)  # type: ignore[arg-type]
    owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        now=datetime.now(timezone.utc),
    )
    authority.bind_approval(
        manifest,
        attempt_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        owner_nonce_sha256=owner,
        approval_digest="sha256:" + "b" * 64,
    )
    monkeypatch.setenv(REMOTE_AUTHORITY_URL_ENV, "http://fake.test/permit")
    monkeypatch.setenv(REMOTE_AUTHORITY_TOKEN_ENV, "secret-token")
    return manifest, authority, owner


def _remote_initial_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: object
) -> tuple[RunManifest, DurableAuthority]:
    run_dir, store, manifest, _authority = _fresh_run(
        tmp_path, execution_profile="standard"
    )
    authority = DurableAuthority(run_dir, store, remote_transport=transport)  # type: ignore[arg-type]
    monkeypatch.setenv(REMOTE_AUTHORITY_URL_ENV, "http://fake.test/permit")
    monkeypatch.setenv(REMOTE_AUTHORITY_TOKEN_ENV, "secret-token")
    return manifest, authority


def _issued_permit_transport(
    permit_id: str, cursor: str
) -> Callable[[str, dict[str, str], bytes], bytes]:
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    retained: dict[str, tuple[object, bytes]] = {}
    permit_digests: list[str] = []

    def transport(url: str, _headers: dict[str, str], body: bytes) -> bytes:
        request = json.loads(body)
        if url.endswith("/api/internal/managed-delivery-acknowledgment"):
            claim_id = request["one_use_claim_id"]
            artifact, permit_bytes = retained[claim_id]
            assert request["permit_artifact_bytes_base64"] == b64encode(
                permit_bytes
            ).decode("ascii")
            delivered_at = (
                _parse_permit_utc(artifact.payload.issued_at) + timedelta(seconds=10)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            receipt = sign_production_delivery_receipt(
                ProductionDeliveryReceiptPayload(
                    execution_authority_id=artifact.payload.execution_authority_id,
                    permit_id=artifact.payload.permit_id,
                    permit_artifact_sha256=hashlib.sha256(permit_bytes).hexdigest(),
                    authenticated_runner_id_sha256="8" * 64,
                    authenticated_session_id_sha256="9" * 64,
                    one_use_claim_id=claim_id,
                    runtime_delivery_sequence=request["runtime_delivery_sequence"],
                    delivered_at=delivered_at,
                ),
                key,
            )
            receipt_bytes = receipt.canonical_bytes()
            return json.dumps(
                {
                    "schema_version": (
                        "openadapt.production-delivery-acknowledgment-result/v2"
                    ),
                    "status": "acknowledged",
                    "receipt_artifact_bytes_base64": b64encode(receipt_bytes).decode(
                        "ascii"
                    ),
                    "receipt_artifact_sha256": hashlib.sha256(
                        receipt_bytes
                    ).hexdigest(),
                    "runtime_delivery_sequence": request["runtime_delivery_sequence"],
                    "delivered_at": delivered_at,
                }
            ).encode()
        sequence = request["remote_delivery_sequence"]
        issued_at = (
            datetime(2026, 8, 20, tzinfo=timezone.utc)
            + timedelta(seconds=20 * sequence + 20)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        permit_uuid = f"20000000-0000-4000-8000-{sequence + 1:012d}"
        session_id = "30000000-0000-4000-8000-000000000001"
        claim_id = f"40000000-0000-4000-8000-{sequence + 1:012d}"
        artifact = sign_production_delivery_permit(
            ProductionDeliveryPermitPayload(
                execution_authority_id="22222222-2222-4222-8222-222222222222",
                execution_authority_sha256="1" * 64,
                permit_id=permit_uuid,
                run_id=request["run_id"],
                flow_run_id_sha256=hashlib.sha256(
                    request["run_id"].encode()
                ).hexdigest(),
                run_request_sha256="2" * 64,
                action_request_sha256=hashlib.sha256(body).hexdigest(),
                admission_artifact_sha256="3" * 64,
                evidence_identity_sha256="4" * 64,
                environment_digest="5" * 64,
                qualification_signer_registry_sha256="6" * 64,
                qualification_signer_registry_revision=1,
                qualification_signer_registry_checked_at="2026-08-20T00:00:00Z",
                qualification_signer_registry_expires_at="2026-08-20T01:00:00Z",
                input_edge_sequence=sequence + 1,
                authority_sequence=sequence,
                issued_at=issued_at,
            ),
            key,
        )
        permit_bytes = artifact.canonical_bytes()
        permit_sha256 = hashlib.sha256(permit_bytes).hexdigest()
        permit_digests.append("sha256:" + permit_sha256)
        retained[claim_id] = (artifact, permit_bytes)
        return json.dumps(
            {
                "schema_version": "openadapt.production-delivery-permit-issue/v2",
                "status": "issued",
                "execution_authority_id": ("22222222-2222-4222-8222-222222222222"),
                "permit_id": permit_uuid,
                "dispatch_session_id": session_id,
                "one_use_claim_id": claim_id,
                "permit_artifact_bytes_base64": b64encode(permit_bytes).decode("ascii"),
                "permit_artifact_sha256": permit_sha256,
                "next_permit_cursor": (
                    cursor
                    if sequence == 0
                    else hashlib.sha256(f"{cursor}:{sequence}".encode()).hexdigest()
                ),
                "input_edge_sequence": sequence + 1,
                "authority_sequence": sequence,
                "runtime_delivery_sequence": sequence,
            }
        ).encode()

    transport.permit_digests = permit_digests  # type: ignore[attr-defined]
    transport.test_permit_label = permit_id  # type: ignore[attr-defined]
    return transport


def _enable_synthetic_marker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str,
) -> None:
    monkeypatch.setenv(SYNTHETIC_DELIVERY_MARKER_ENABLED_ENV, "1")
    monkeypatch.setenv(SYNTHETIC_DELIVERY_MARKER_RUN_ID_ENV, run_id)


def test_demo_delivery_stays_local_without_remote_configuration(tmp_path: Path) -> None:
    _run_dir, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    owner = _acquire(
        authority, manifest, pending, attempt="demo", now=datetime.now(timezone.utc)
    )
    authority.before_delivery(manifest, attempt_id="demo", owner_nonce_sha256=owner)


def test_customer_controlled_standard_delivery_stays_local_without_cloud(
    tmp_path: Path,
) -> None:
    _run_dir, store, manifest, authority = _fresh_run(
        tmp_path,
        execution_profile="standard",
        delivery_authority_kind="customer_local",
    )
    pending = _pause(store, manifest, authority)
    owner = _acquire(
        authority, manifest, pending, attempt="local", now=datetime.now(timezone.utc)
    )
    authority.before_delivery(manifest, attempt_id="local", owner_nonce_sha256=owner)


def test_empty_legacy_pending_delivery_table_migrates_without_data_loss(
    tmp_path: Path,
) -> None:
    _run_dir, _store, manifest, authority = _fresh_run(tmp_path)
    _replace_pending_table_with_legacy_schema(authority, populated=False)

    assert authority.validate(manifest).phase == "active"
    with sqlite3.connect(authority.db_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(remote_delivery_pending)"
            ).fetchall()
        }
        pending_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM remote_delivery_pending"
            ).fetchone()[0]
        )
    assert "authority_origin" in columns
    assert pending_count == 0


def test_populated_legacy_pending_delivery_table_fails_closed(
    tmp_path: Path,
) -> None:
    _run_dir, _store, manifest, authority = _fresh_run(tmp_path)
    _replace_pending_table_with_legacy_schema(authority, populated=True)

    with pytest.raises(
        DurableAuthorityBusy,
        match="legacy pending remote delivery cannot be migrated",
    ):
        authority.validate(manifest)


def test_production_delivery_requires_and_validates_remote_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}
    issued = _issued_permit_transport("permit-1", "a" * 64)

    def transport(url: str, headers: dict[str, str], body: bytes) -> bytes:
        if not url.endswith("/managed-delivery-acknowledgment"):
            seen.update(headers=headers, request=json.loads(body))
        return issued(url, headers, body)

    manifest, authority, owner = _remote_ready_authority(
        tmp_path, monkeypatch, transport
    )
    permit = authority.before_delivery(
        manifest,
        attempt_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        owner_nonce_sha256=owner,
    )
    assert permit is not None
    authority.acknowledge_remote_delivery(
        manifest,
        permit,
        attempt_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        owner_nonce_sha256=owner,
    )
    assert seen["headers"] == {
        "Authorization": "Bearer secret-token",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    assert seen["request"] and seen["request"]["delivery_sequence"] == 0  # type: ignore[index]
    request = seen["request"]
    assert isinstance(request, dict)
    assert request["remote_delivery_sequence"] == 0
    assert request["permit_cursor"] is None
    record_dump = authority.validate(manifest).model_dump(mode="json")
    assert "remote_permit_cursor" not in record_dump
    assert "next_permit_cursor" not in record_dump


def test_v2_permit_remains_pending_until_signed_receipt_commits_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _issued_permit_transport("pending-edge", "4" * 64)
    manifest, authority = _remote_initial_authority(tmp_path, monkeypatch, transport)

    permit = authority.before_initial_delivery(manifest)
    assert permit is not None
    assert authority.validate(manifest).delivery_sequence == 0
    with pytest.raises(DurableAuthorityBusy, match="lacks an acknowledgment"):
        authority.before_initial_delivery(manifest)
    with pytest.raises(DurableAuthorityBusy, match="remains uncertain"):
        authority.production_delivery_permit_chain()

    entry = authority.acknowledge_remote_delivery(manifest, permit)

    assert authority.validate(manifest).delivery_sequence == 1
    chain = authority.production_delivery_permit_chain()
    assert chain.entries == (entry,)
    assert entry.input_edge_sequence == 1
    assert entry.authority_sequence == 0
    assert entry.runtime_delivery_sequence == 0


def test_receipt_digest_mismatch_keeps_delivery_uncertain_and_blocks_next_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issued = _issued_permit_transport("bad-receipt", "3" * 64)

    def transport(url: str, headers: dict[str, str], body: bytes) -> bytes:
        response = issued(url, headers, body)
        if url.endswith("/managed-delivery-acknowledgment"):
            value = json.loads(response)
            value["receipt_artifact_sha256"] = "0" * 64
            return json.dumps(value).encode()
        return response

    manifest, authority = _remote_initial_authority(tmp_path, monkeypatch, transport)
    permit = authority.before_initial_delivery(manifest)
    assert permit is not None

    with pytest.raises(DurableAuthorityBusy, match="digest does not match"):
        authority.acknowledge_remote_delivery(manifest, permit)

    assert authority.validate(manifest).delivery_sequence == 0
    with pytest.raises(DurableAuthorityBusy, match="lacks an acknowledgment"):
        authority.before_initial_delivery(manifest)


def test_acknowledgment_refuses_changed_authority_origin_before_token_forwarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issued = _issued_permit_transport("origin-bound", "2" * 64)
    acknowledgment_calls = 0

    def transport(url: str, headers: dict[str, str], body: bytes) -> bytes:
        nonlocal acknowledgment_calls
        if url.endswith("/managed-delivery-acknowledgment"):
            acknowledgment_calls += 1
        return issued(url, headers, body)

    manifest, authority = _remote_initial_authority(tmp_path, monkeypatch, transport)
    permit = authority.before_initial_delivery(manifest)
    assert permit is not None
    monkeypatch.setenv(
        REMOTE_AUTHORITY_URL_ENV,
        "https://other-control.example/api/internal/managed-delivery-permit",
    )

    with pytest.raises(DurableAuthorityBusy, match="origin changed"):
        authority.acknowledge_remote_delivery(manifest, permit)

    assert acknowledgment_calls == 0
    assert authority.validate(manifest).delivery_sequence == 0


def test_backend_success_requires_delivery_acknowledgment_before_return() -> None:
    events: list[str] = []

    class Guard:
        def acknowledge_delivery(self) -> None:
            events.append("acknowledged")

    replayer = object.__new__(Replayer)
    replayer._active_delivery_acknowledgers = (Guard(),)
    result = StepResult(step_id="edge", intent="deliver edge", ok=False)

    delivered = replayer._deliver_backend_call(
        result,
        lambda: events.append("delivered") or "receipt",
    )

    assert delivered == "receipt"
    assert events == ["delivered", "acknowledged"]
    assert result.delivery_attempted is True


def test_backend_acknowledgment_failure_is_an_uncertain_delivery() -> None:
    class Guard:
        def acknowledge_delivery(self) -> None:
            raise DurableAuthorityBusy("receipt unavailable")

    replayer = object.__new__(Replayer)
    replayer._active_delivery_acknowledgers = (Guard(),)
    result = StepResult(step_id="edge", intent="deliver edge", ok=False)

    with pytest.raises(DurableAuthorityBusy, match="receipt unavailable"):
        replayer._deliver_backend_call(result, lambda: None)

    assert result.delivery_attempted is True
    assert replayer._active_delivery_acknowledgers == ()


def test_synthetic_delivery_marker_is_canonical_and_never_contains_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    permit_id = "synthetic-permit-id-not-for-observers"
    permit_cursor = "d" * 64
    transport = _issued_permit_transport(permit_id, permit_cursor)
    manifest, authority = _remote_initial_authority(
        tmp_path,
        monkeypatch,
        transport,
    )
    _enable_synthetic_marker(monkeypatch, run_id=manifest.run_id)
    observed: list[bytes] = []

    def sink(payload: bytes) -> None:
        # This observer runs only after the durable transaction commits.
        assert authority.validate(manifest).delivery_sequence == 1
        observed.append(payload)

    authority._synthetic_delivery_marker_sink = sink

    permit = authority.before_initial_delivery(manifest)
    assert permit is not None
    authority.acknowledge_remote_delivery(manifest, permit)

    assert len(observed) == 1
    raw = observed[0]
    assert raw.endswith(b"\n")
    assert (
        raw
        == json.dumps(
            json.loads(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        + b"\n"
    )
    marker = json.loads(raw)
    assert marker == {
        "delivery_count": 1,
        "managed_dispatch_binding_sha256": manifest.managed_dispatch_binding_sha256,
        "permit_count": 1,
        "permit_digest": transport.permit_digests[0],  # type: ignore[attr-defined]
        "run_id": manifest.run_id,
        "schema_version": 1,
    }
    assert permit_id.encode() not in raw
    assert permit_cursor.encode() not in raw
    assert b"secret-token" not in raw


def test_synthetic_marker_sink_failure_never_changes_a_permitted_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _issued_permit_transport("permit-with-broken-observer", "c" * 64)
    manifest, authority = _remote_initial_authority(
        tmp_path,
        monkeypatch,
        transport,
    )
    _enable_synthetic_marker(monkeypatch, run_id=manifest.run_id)

    def broken_sink(_payload: bytes) -> None:
        raise OSError("observer pipe is full")

    authority._synthetic_delivery_marker_sink = broken_sink

    permit = authority.before_initial_delivery(manifest)
    assert permit is not None
    authority.acknowledge_remote_delivery(manifest, permit)

    assert authority.validate(manifest).delivery_sequence == 1


def test_synthetic_marker_short_pipe_write_never_changes_a_permitted_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial observer write is not part of the durable delivery contract."""

    transport = _issued_permit_transport("permit-with-short-observer-write", "b" * 64)
    manifest, authority = _remote_initial_authority(
        tmp_path,
        monkeypatch,
        transport,
    )
    _enable_synthetic_marker(monkeypatch, run_id=manifest.run_id)

    class PipeStatus:
        st_mode = stat.S_IFIFO

    monkeypatch.setattr(
        durable_authority_module.os,
        "fstat",
        lambda _fd: PipeStatus(),
    )
    monkeypatch.setattr(
        durable_authority_module.os, "set_blocking", lambda *_args: None
    )
    monkeypatch.setattr(
        durable_authority_module.os,
        "write",
        lambda _fd, _payload: 1,  # Simulate a short non-blocking pipe write.
    )
    authority._synthetic_delivery_marker_sink = (
        durable_authority_module._fixed_synthetic_delivery_marker_sink()
    )

    permit = authority.before_initial_delivery(manifest)
    assert permit is not None
    authority.acknowledge_remote_delivery(manifest, permit)

    # The exact permit transaction commits even when the observer drops a marker.
    assert authority.validate(manifest).delivery_sequence == 1


def test_synthetic_marker_uses_only_the_fixed_nonblocking_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PipeStatus:
        st_mode = stat.S_IFIFO

    calls: list[tuple[object, ...]] = []
    monkeypatch.setenv(SYNTHETIC_DELIVERY_MARKER_ENABLED_ENV, "1")
    # This legacy-shaped value must have no effect. Flow has no output-path
    # configuration for this observer.
    monkeypatch.setenv("OPENADAPT_SYNTHETIC_DELIVERY_MARKER_PATH", "/attacker/out")
    monkeypatch.setattr(
        durable_authority_module.os,
        "fstat",
        lambda fd: calls.append(("fstat", fd)) or PipeStatus(),
    )
    monkeypatch.setattr(
        durable_authority_module.os,
        "set_blocking",
        lambda fd, enabled: calls.append(("set_blocking", fd, enabled)),
    )
    monkeypatch.setattr(
        durable_authority_module.os,
        "write",
        lambda fd, payload: calls.append(("write", fd, payload)) or 1,
    )

    sink = durable_authority_module._fixed_synthetic_delivery_marker_sink()

    assert sink is not None
    sink(b"bounded marker")
    assert calls == [
        ("fstat", 3),
        ("set_blocking", 3, False),
        ("write", 3, b"bounded marker"),
    ]


def test_synthetic_delivery_marker_requires_exact_server_owned_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, authority, owner = _remote_ready_authority(
        tmp_path,
        monkeypatch,
        _issued_permit_transport("permit-for-wrong-run", "e" * 64),
    )
    _enable_synthetic_marker(monkeypatch, run_id="33333333-3333-4333-8333-333333333333")
    observed: list[bytes] = []
    authority._synthetic_delivery_marker_sink = observed.append

    permit = authority.before_delivery(
        manifest,
        attempt_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        owner_nonce_sha256=owner,
    )
    assert permit is not None
    authority.acknowledge_remote_delivery(
        manifest,
        permit,
        attempt_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        owner_nonce_sha256=owner,
    )

    assert not observed


def test_synthetic_delivery_marker_is_not_emitted_before_fence_and_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, authority, owner = _remote_ready_authority(
        tmp_path,
        monkeypatch,
        _issued_permit_transport("issued-but-not-consumed", "f" * 64),
    )
    _enable_synthetic_marker(monkeypatch, run_id=manifest.run_id)
    observed: list[bytes] = []
    authority._synthetic_delivery_marker_sink = observed.append
    monkeypatch.setattr(
        authority,
        "_consume_delivery_fence",
        lambda _record, _manifest: (_ for _ in ()).throw(
            DurableAuthorityBusy("synthetic fence failure")
        ),
    )

    with pytest.raises(DurableAuthorityBusy, match="synthetic fence failure"):
        authority.before_delivery(
            manifest,
            attempt_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            owner_nonce_sha256=owner,
        )

    assert not observed
    assert authority.validate(manifest).delivery_sequence == 0


def test_synthetic_delivery_marker_is_not_emitted_before_cursor_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, authority, owner = _remote_ready_authority(
        tmp_path,
        monkeypatch,
        _issued_permit_transport("issued-before-cursor-failure", "a" * 64),
    )
    _enable_synthetic_marker(monkeypatch, run_id=manifest.run_id)
    observed: list[bytes] = []
    authority._synthetic_delivery_marker_sink = observed.append
    monkeypatch.setattr(
        authority,
        "_advance_remote_cursor",
        lambda _connection, **_kwargs: (_ for _ in ()).throw(
            DurableAuthorityBusy("synthetic cursor failure")
        ),
    )

    permit = authority.before_delivery(
        manifest,
        attempt_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        owner_nonce_sha256=owner,
    )
    assert permit is not None
    with pytest.raises(DurableAuthorityBusy, match="synthetic cursor failure"):
        authority.acknowledge_remote_delivery(
            manifest,
            permit,
            attempt_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            owner_nonce_sha256=owner,
        )

    assert not observed
    assert authority.validate(manifest).delivery_sequence == 0


def test_production_delivery_requires_remote_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def transport(_url: str, _headers: dict[str, str], _body: bytes) -> bytes:
        nonlocal called
        called = True
        return b"{}"

    manifest, authority, owner = _remote_ready_authority(
        tmp_path, monkeypatch, transport
    )
    _enable_synthetic_marker(monkeypatch, run_id=manifest.run_id)
    observed: list[bytes] = []
    authority._synthetic_delivery_marker_sink = observed.append
    monkeypatch.delenv(REMOTE_AUTHORITY_URL_ENV)
    with pytest.raises(DurableAuthorityBusy, match="requires configured remote"):
        authority.before_delivery(
            manifest,
            attempt_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            owner_nonce_sha256=owner,
        )
    assert not called
    assert not observed
    assert authority.validate(manifest).delivery_sequence == 0


def test_remote_authority_refuses_redirect_before_forwarding_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, authority, owner = _remote_ready_authority(
        tmp_path, monkeypatch, lambda *_args: b"{}"
    )
    authority._remote_transport = None
    monkeypatch.setenv(
        REMOTE_AUTHORITY_URL_ENV,
        "https://control.example/api/internal/managed-delivery-permit",
    )
    attempted_urls: list[str] = []

    class RedirectingOpener:
        def __init__(self, handler: object) -> None:
            self.handler = handler

        def open(self, request: object, *, timeout: int) -> object:
            assert timeout == 10
            attempted_urls.append(request.full_url)  # type: ignore[attr-defined]
            assert request.get_header("Authorization") == "Bearer secret-token"  # type: ignore[attr-defined]
            self.handler.redirect_request(  # type: ignore[attr-defined]
                request,
                None,
                307,
                "Temporary Redirect",
                {},
                "https://attacker.invalid/permit",
            )
            raise AssertionError("redirect refusal must stop the request")

    def build_redirecting_opener(handler: object) -> RedirectingOpener:
        assert isinstance(
            handler, durable_authority_module._RefuseRemoteAuthorityRedirects
        )
        return RedirectingOpener(handler)

    monkeypatch.setattr(
        durable_authority_module,
        "build_opener",
        build_redirecting_opener,
    )
    with pytest.raises(DurableAuthorityBusy, match="unavailable or refused"):
        authority.before_delivery(
            manifest,
            attempt_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            owner_nonce_sha256=owner,
        )
    assert attempted_urls == [
        "https://control.example/api/internal/managed-delivery-permit"
    ]
    assert authority.validate(manifest).delivery_sequence == 0


def test_initial_customer_local_delivery_needs_no_cloud_credentials(
    tmp_path: Path,
) -> None:
    _run_dir, _store, manifest, authority = _fresh_run(
        tmp_path, execution_profile="standard", delivery_authority_kind="customer_local"
    )
    authority.before_initial_delivery(manifest)


def test_initial_managed_edges_advance_remote_and_local_sequences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, object]] = []
    issued = _issued_permit_transport("initial-sequence", "7" * 64)

    def transport(url: str, headers: dict[str, str], body: bytes) -> bytes:
        if not url.endswith("/managed-delivery-acknowledgment"):
            requests.append(json.loads(body))
        return issued(url, headers, body)

    _run_dir, _store, manifest, authority = _fresh_run(
        tmp_path, execution_profile="standard"
    )
    monkeypatch.setenv(REMOTE_AUTHORITY_URL_ENV, "https://control.example/permit")
    monkeypatch.setenv(REMOTE_AUTHORITY_TOKEN_ENV, "token")
    authority = DurableAuthority(
        authority.run_dir, authority.store, remote_transport=transport
    )
    first = authority.before_initial_delivery(manifest)
    assert first is not None
    authority.acknowledge_remote_delivery(manifest, first)
    second = authority.before_initial_delivery(manifest)
    assert second is not None
    authority.acknowledge_remote_delivery(manifest, second)
    assert [request["delivery_sequence"] for request in requests] == [0, 1]
    assert [request["remote_delivery_sequence"] for request in requests] == [0, 1]
    assert requests[0]["permit_cursor"] is None
    assert requests[1]["permit_cursor"] is not None
    assert authority.validate(manifest).delivery_sequence == 2
    chain = authority.production_delivery_permit_chain()
    assert [entry.input_edge_sequence for entry in chain.entries] == [1, 2]
    assert [entry.authority_sequence for entry in chain.entries] == [0, 1]
    assert [entry.runtime_delivery_sequence for entry in chain.entries] == [0, 1]


def test_replayer_refuses_public_cloud_runner_strings() -> None:
    with pytest.raises(ValueError, match="verified internal managed dispatch binding"):
        Replayer(
            object(),
            delivery_authority_kind="cloud_runner",
            remote_delivery_run_id="11111111-1111-4111-8111-111111111111",
        )


def test_lost_initial_permit_response_does_not_advance_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def transport(_url: str, _headers: dict[str, str], _body: bytes) -> bytes:
        raise TimeoutError("response lost after server commit")

    _run_dir, _store, manifest, authority = _fresh_run(
        tmp_path, execution_profile="standard"
    )
    monkeypatch.setenv(REMOTE_AUTHORITY_URL_ENV, "https://control.example/permit")
    monkeypatch.setenv(REMOTE_AUTHORITY_TOKEN_ENV, "token")
    authority = DurableAuthority(
        authority.run_dir, authority.store, remote_transport=transport
    )
    with pytest.raises(DurableAuthorityBusy, match="unavailable or refused"):
        authority.before_initial_delivery(manifest)
    assert authority.validate(manifest).delivery_sequence == 0


def test_cloud_manifest_refuses_free_text_before_remote_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def transport(_url: str, _headers: dict[str, str], _body: bytes) -> bytes:
        nonlocal called
        called = True
        return b"{}"

    with pytest.raises(ValueError, match="canonical Cloud run id"):
        _fresh_run(
            tmp_path,
            execution_profile="standard",
            run_id="Patient Jane Doe",
        )
    assert not called


def test_cloud_manifest_requires_a_production_authorization(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError,
        match="Standard or Regulated authorization",
    ):
        _fresh_run(
            tmp_path,
            execution_profile="demo",
            delivery_authority_kind="cloud_runner",
        )


def test_production_delivery_rejects_insecure_remote_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, authority, owner = _remote_ready_authority(tmp_path, monkeypatch, None)
    with pytest.raises(DurableAuthorityBusy, match="must use HTTPS"):
        authority.before_delivery(
            manifest,
            attempt_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            owner_nonce_sha256=owner,
        )
    assert authority.validate(manifest).delivery_sequence == 0


@pytest.mark.parametrize(
    "fault", ["malformed", "timeout", "duplicate", "sequence", "digest"]
)
def test_production_remote_refusal_never_crosses_local_delivery_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    def transport(_url: str, _headers: dict[str, str], body: bytes) -> bytes:
        if fault == "timeout":
            raise TimeoutError("response lost after remote commit")
        if fault == "malformed":
            return b"{"
        request = json.loads(body)
        response = {
            "schema_version": 1,
            "status": "issued",
            "authority_id": "22222222-2222-4222-8222-222222222222",
            "permit_id": "permit-1",
            "request_sha256": hashlib.sha256(body).hexdigest(),
            "next_permit_cursor": hashlib.sha256(
                ("cursor:" + (request["permit_cursor"] or "genesis")).encode()
            ).hexdigest(),
            "delivery_sequence": request["delivery_sequence"],
            "issued_at": "2026-07-29T00:00:00+00:00",
        }
        if fault == "duplicate":
            raise OSError("409 duplicate delivery sequence")
        if fault == "sequence":
            response["delivery_sequence"] += 1
        if fault == "digest":
            response["request_sha256"] = "0" * 64
        return json.dumps(response).encode()

    manifest, authority, owner = _remote_ready_authority(
        tmp_path, monkeypatch, transport
    )
    with pytest.raises(DurableAuthorityBusy):
        authority.before_delivery(
            manifest,
            attempt_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            owner_nonce_sha256=owner,
        )
    assert authority.validate(manifest).delivery_sequence == 0


def test_remote_sequence_survives_complete_local_state_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumed: set[tuple[str, str, int]] = set()
    issued = _issued_permit_transport("restore-sequence", "6" * 64)

    def transport(url: str, headers: dict[str, str], body: bytes) -> bytes:
        request = json.loads(body)
        key = (
            request["run_id"],
            request["pause_binding_sha256"],
            request["delivery_sequence"],
        )
        if key in consumed:
            raise OSError("409 duplicate delivery sequence")
        consumed.add(key)
        return issued(url, headers, body)

    run_dir, store, manifest, _authority = _fresh_run(
        tmp_path, execution_profile="standard"
    )
    authority = DurableAuthority(run_dir, store, remote_transport=transport)
    pending = _pause(store, manifest, authority)
    owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="cccccccccccccccccccccccccccccccc",
        now=datetime.now(timezone.utc),
    )
    authority.bind_approval(
        manifest,
        attempt_id="cccccccccccccccccccccccccccccccc",
        owner_nonce_sha256=owner,
        approval_digest="sha256:" + "d" * 64,
    )
    run_snapshot = tmp_path / "run-before-delivery"
    db_snapshot = tmp_path / "authority-before-delivery.sqlite3"
    _snapshot(run_dir, run_snapshot)
    shutil.copy2(authority.db_path, db_snapshot)
    monkeypatch.setenv(REMOTE_AUTHORITY_URL_ENV, "http://fake.test/permit")
    monkeypatch.setenv(REMOTE_AUTHORITY_TOKEN_ENV, "secret-token")

    authority.before_delivery(
        manifest,
        attempt_id="cccccccccccccccccccccccccccccccc",
        owner_nonce_sha256=owner,
    )

    _restore(run_dir, run_snapshot)
    shutil.copy2(db_snapshot, authority.db_path)
    restarted_store = CheckpointStore(run_dir)
    restarted_manifest = restarted_store.read_manifest()
    assert restarted_manifest is not None
    restarted = DurableAuthority(
        run_dir,
        restarted_store,
        remote_transport=transport,
    )
    with pytest.raises(DurableAuthorityBusy, match="unavailable or refused"):
        restarted.before_delivery(
            restarted_manifest,
            attempt_id="cccccccccccccccccccccccccccccccc",
            owner_nonce_sha256=owner,
        )
    assert len(consumed) == 1
    assert restarted.validate(restarted_manifest).delivery_sequence == 0


def test_remote_sequence_spans_verified_progress_in_one_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected_sequence = 0
    observed: list[tuple[int, str, int, str | None]] = []
    issued = _issued_permit_transport("progress-sequence", "5" * 64)

    def transport(url: str, headers: dict[str, str], body: bytes) -> bytes:
        nonlocal expected_sequence
        request = json.loads(body)
        if url.endswith("/managed-delivery-acknowledgment"):
            return issued(url, headers, body)
        assert request["delivery_sequence"] == expected_sequence
        observed.append(
            (
                expected_sequence,
                request["progress_digest"],
                request["remote_delivery_sequence"],
                request["permit_cursor"],
            )
        )
        expected_sequence += 1
        return issued(url, headers, body)

    run_dir, store, manifest, _authority = _fresh_run(
        tmp_path, execution_profile="standard"
    )
    authority = DurableAuthority(run_dir, store, remote_transport=transport)
    pending = _pause(store, manifest, authority)
    owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        now=datetime.now(timezone.utc),
    )
    authority.bind_approval(
        manifest,
        attempt_id="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        owner_nonce_sha256=owner,
        approval_digest="sha256:" + "f" * 64,
    )
    monkeypatch.setenv(REMOTE_AUTHORITY_URL_ENV, "http://fake.test/permit")
    monkeypatch.setenv(REMOTE_AUTHORITY_TOKEN_ENV, "secret-token")
    _enable_synthetic_marker(monkeypatch, run_id=manifest.run_id)
    observed_markers: list[dict[str, object]] = []

    def sink(payload: bytes) -> None:
        marker = json.loads(payload)
        # The observer runs after the authority transaction has committed.
        assert (
            authority.validate(manifest).delivery_sequence == marker["delivery_count"]
        )
        observed_markers.append(marker)

    authority._synthetic_delivery_marker_sink = sink

    first = authority.before_delivery(
        manifest,
        attempt_id="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        owner_nonce_sha256=owner,
    )
    assert first is not None
    authority.acknowledge_remote_delivery(
        manifest,
        first,
        attempt_id="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        owner_nonce_sha256=owner,
    )
    store.write_checkpoint(
        RunCheckpoint(
            run_id=manifest.run_id,
            workflow_name=manifest.workflow_name,
            bundle_version="sha256:bundle-v1",
            step_index=0,
            step_id="write-case",
            intent="write the case",
            next_step_index=1,
            params=dict(manifest.params),
            effect_verified=True,
            postconditions_ok=True,
        )
    )
    authority.acknowledge_progress(
        manifest,
        attempt_id="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        owner_nonce_sha256=owner,
        terminal_pause=False,
    )
    second = authority.before_delivery(
        manifest,
        attempt_id="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        owner_nonce_sha256=owner,
    )
    assert second is not None
    authority.acknowledge_remote_delivery(
        manifest,
        second,
        attempt_id="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        owner_nonce_sha256=owner,
    )

    assert [sequence for sequence, _digest, _remote, _cursor in observed] == [0, 1]
    assert [remote for _sequence, _digest, remote, _cursor in observed] == [0, 1]
    assert observed[0][3] is None
    assert isinstance(observed[1][3], str) and len(observed[1][3]) == 64
    assert observed[0][1] != observed[1][1]
    assert [marker["delivery_count"] for marker in observed_markers] == [1, 2]


def test_lost_remote_response_leaves_stale_private_cursor_and_halts_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server commit without a local response cannot be retried as input."""

    server_sequence = 0
    server_cursor: str | None = None
    calls = 0

    def transport(_url: str, _headers: dict[str, str], body: bytes) -> bytes:
        nonlocal server_sequence, server_cursor, calls
        calls += 1
        request = json.loads(body)
        if (
            request["remote_delivery_sequence"] != server_sequence
            or request["permit_cursor"] != server_cursor
        ):
            raise OSError("409 stale remote cursor")
        server_cursor = hashlib.sha256(
            ("cursor:" + (server_cursor or "genesis")).encode()
        ).hexdigest()
        server_sequence += 1
        if calls == 1:
            raise TimeoutError("response lost after server commit")
        raise OSError("the stale request must not receive a second permit")

    manifest, authority, owner = _remote_ready_authority(
        tmp_path, monkeypatch, transport
    )
    with pytest.raises(DurableAuthorityBusy, match="unavailable or refused"):
        authority.before_delivery(
            manifest,
            attempt_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            owner_nonce_sha256=owner,
        )
    assert authority.validate(manifest).delivery_sequence == 0
    with pytest.raises(DurableAuthorityBusy, match="unavailable or refused"):
        authority.before_delivery(
            manifest,
            attempt_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            owner_nonce_sha256=owner,
        )
    assert calls == 2
    assert authority.validate(manifest).delivery_sequence == 0


def _restore(run_dir: Path, snapshot: Path) -> None:
    shutil.rmtree(run_dir)
    shutil.copytree(snapshot, run_dir)


def _attended_context(
    tmp_path: Path,
) -> tuple[
    AttendedActionStore,
    DurableAuthority,
    RunManifest,
]:
    run_dir, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    workflow = Workflow.load(manifest.bundle_dir)
    issue_attended_capability(
        run_dir,
        store=store,
        pending=pending,
        workflow=workflow,
        result=StepResult(
            step_id=pending.step_id,
            intent=pending.intent,
            ok=False,
            error=pending.reason,
        ),
    )
    return AttendedActionStore(run_dir), authority, manifest


def _decision(index: int) -> AttendedDecision:
    return AttendedDecision(
        decision_id=f"decision-{index:04d}",
        pause_id="pause-v13-authority",
        capability_digest="sha256:" + (f"{index:064x}"[-64:]),
        request_digest="sha256:" + (f"{index + 100:064x}"[-64:]),
        idempotency_key=f"authority-request-{index:04d}",
        action="continue",
        operator="operator@example.com",
        status="completed",
        message=f"decision {index} completed",
        report_success=True,
    )


def test_same_path_restore_before_pause_cannot_erase_pause(tmp_path: Path) -> None:
    run_dir, store, manifest, authority = _fresh_run(tmp_path)
    snapshot = tmp_path / "active-snapshot"
    _snapshot(run_dir, snapshot)

    _pause(store, manifest, authority)
    _restore(run_dir, snapshot)

    with pytest.raises(
        DurableAuthorityBusy,
        match="does not match the external monotonic authority",
    ):
        CheckpointStore(run_dir).validate_namespace(manifest)


def test_same_path_restore_cannot_erase_acknowledged_progress(tmp_path: Path) -> None:
    run_dir, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    snapshot = tmp_path / "paused-snapshot"
    _snapshot(run_dir, snapshot)

    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="progress-attempt",
        now=now,
    )
    authority.before_delivery(
        manifest,
        attempt_id="progress-attempt",
        owner_nonce_sha256=owner,
    )
    store.write_checkpoint(
        RunCheckpoint(
            run_id=manifest.run_id,
            workflow_name=manifest.workflow_name,
            bundle_version="sha256:bundle-v1",
            step_index=0,
            step_id="write-case",
            intent="write the case",
            next_step_index=1,
            params=dict(manifest.params),
            effect_verified=True,
            postconditions_ok=True,
        )
    )
    authority.acknowledge_progress(
        manifest,
        attempt_id="progress-attempt",
        owner_nonce_sha256=owner,
        terminal_pause=False,
    )
    _restore(run_dir, snapshot)

    with pytest.raises(
        DurableAuthorityBusy,
        match="does not match the external monotonic authority",
    ):
        DurableAuthority(run_dir, CheckpointStore(run_dir)).validate(manifest)


def test_one_owned_continuation_can_fence_multiple_input_edges(
    tmp_path: Path,
) -> None:
    """Focus, typing, and later edges share one exact continuation owner."""

    _run_dir, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="multi-edge-attempt",
        now=now,
    )

    for _edge in range(3):
        authority.before_delivery(
            manifest,
            attempt_id="multi-edge-attempt",
            owner_nonce_sha256=owner,
        )

    with authority._transaction() as connection:  # noqa: SLF001
        record = authority._read(connection)  # noqa: SLF001
    assert record is not None
    assert record.attempt_phase == "delivery_started"
    assert record.delivery_sequence == 3


def test_observation_only_progress_does_not_require_a_delivery_edge(
    tmp_path: Path,
) -> None:
    run_dir, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="observation-attempt",
        now=now,
    )
    store.write_checkpoint(
        RunCheckpoint(
            run_id=manifest.run_id,
            workflow_name=manifest.workflow_name,
            bundle_version="sha256:bundle-v1",
            step_index=0,
            step_id="wait-for-ready",
            intent="wait for ready state",
            next_step_index=1,
            params=dict(manifest.params),
            postconditions_ok=True,
        )
    )
    store.clear_pending()

    authority.acknowledge_progress(
        manifest,
        attempt_id="observation-attempt",
        owner_nonce_sha256=owner,
        terminal_pause=False,
    )

    assert authority.validate(manifest).delivery_sequence == 0


def test_same_path_restore_cannot_reopen_completed_run(tmp_path: Path) -> None:
    run_dir, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    snapshot = tmp_path / "paused-snapshot"
    _snapshot(run_dir, snapshot)

    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="terminal-attempt",
        now=now,
    )
    report = run_dir / ".report.terminal-candidate.json"
    report.write_bytes(b'{"outcome":"VERIFIED"}\n')
    report_sha256 = "sha256:" + hashlib.sha256(report.read_bytes()).hexdigest()
    authority.prepare_terminal(
        manifest,
        attempt_id="terminal-attempt",
        owner_nonce_sha256=owner,
        report_sha256=report_sha256,
    )
    store.clear_pending()
    authority.finalize_terminal(
        manifest,
        attempt_id="terminal-attempt",
        owner_nonce_sha256=owner,
        report_sha256=report_sha256,
    )
    _restore(run_dir, snapshot)

    with pytest.raises(DurableAuthorityBusy, match="completed"):
        DurableAuthority(run_dir, CheckpointStore(run_dir)).validate(manifest)


def test_completed_terminal_is_not_attestable_until_projection_settles(
    tmp_path: Path,
) -> None:
    """A crash after durable completion but before ledger projection is safe."""

    run_dir, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    owner = _acquire(authority, manifest, pending, attempt="projection", now=now)
    report = run_dir / "report.json"
    report.write_bytes(b'{"outcome":"VERIFIED"}\n')
    report_sha256 = "sha256:" + hashlib.sha256(report.read_bytes()).hexdigest()
    authority.prepare_terminal(
        manifest,
        attempt_id="projection",
        owner_nonce_sha256=owner,
        report_sha256=report_sha256,
    )
    store.clear_pending()
    authority.finalize_terminal(
        manifest,
        attempt_id="projection",
        owner_nonce_sha256=owner,
        report_sha256=report_sha256,
    )

    # The authority has consumed the pause, but the terminal report cannot yet
    # prove completion until the idempotency projection commits.
    assert (
        authority.prove_executor_outcome(
            manifest,
            attempt_id="projection",
            owner_nonce_sha256=owner,
            source_pause_binding=approval_pause_digest(pending),
        )
        is None
    )

    authority.settle_terminal_projection(manifest, report_sha256=report_sha256)
    assert authority.prove_executor_outcome(
        manifest,
        attempt_id="projection",
        owner_nonce_sha256=owner,
        source_pause_binding=approval_pause_digest(pending),
    ) == ("completed", True)


def test_projection_failure_binds_only_one_fail_closed_report_correction(
    tmp_path: Path,
) -> None:
    """A replacement report is attestable only after its exact hash is bound."""

    run_dir, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    owner = _acquire(authority, manifest, pending, attempt="correction", now=now)
    report = run_dir / "report.json"
    report.write_bytes(b'{"outcome":"VERIFIED"}\n')
    original = "sha256:" + hashlib.sha256(report.read_bytes()).hexdigest()
    authority.prepare_terminal(
        manifest,
        attempt_id="correction",
        owner_nonce_sha256=owner,
        report_sha256=original,
    )
    store.clear_pending()
    authority.finalize_terminal(
        manifest,
        attempt_id="correction",
        owner_nonce_sha256=owner,
        report_sha256=original,
    )

    # A crash after replacement but before the authority correction makes the
    # terminal proof fail closed because its state remains pending and hashes
    # no longer agree.
    corrected_report = RunReport(
        workflow_name="authority-contract",
        started_at="2026-07-28T12:00:00+00:00",
        execution_profile="demo",
        execution_outcome="FAILED",
        transaction_outcome="RECONCILIATION_REQUIRED",
        success=False,
        run_id_sha256=hashlib.sha256(manifest.run_id.encode("utf-8")).hexdigest(),
        idempotency_key="projection-failure-key",
        results=[
            StepResult(
                step_id="<idempotency>",
                intent="persist terminal idempotency outcome",
                ok=False,
                failure_category="runtime_failure",
                error=(
                    "terminal idempotency outcome persistence failed "
                    "(OSError); retained action evidence requires reconciliation"
                ),
            )
        ],
    )
    report.write_text(corrected_report.model_dump_json(), encoding="utf-8")
    corrected = "sha256:" + hashlib.sha256(report.read_bytes()).hexdigest()
    assert (
        authority.prove_executor_outcome(
            manifest,
            attempt_id="correction",
            owner_nonce_sha256=owner,
            source_pause_binding=approval_pause_digest(pending),
        )
        is None
    )

    authority.correct_terminal_projection_failure(
        manifest,
        original_report_sha256=original,
        corrected_report_sha256=corrected,
        corrected_report_json=report.read_bytes(),
    )
    assert authority.prove_executor_outcome(
        manifest,
        attempt_id="correction",
        owner_nonce_sha256=owner,
        source_pause_binding=approval_pause_digest(pending),
    ) == ("halted", False)
    with pytest.raises(DurableAuthorityBusy, match="hash mismatch|cannot accept"):
        authority.correct_terminal_projection_failure(
            manifest,
            original_report_sha256=corrected,
            corrected_report_sha256=original,
            corrected_report_json=report.read_bytes(),
        )


def test_only_one_unexpired_continuation_attempt_can_own_pause(
    tmp_path: Path,
) -> None:
    _, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    first_owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="attempt-one",
        now=now,
    )

    with pytest.raises(DurableAuthorityBusy, match="already in progress"):
        _acquire(
            authority,
            manifest,
            pending,
            attempt="attempt-two",
            now=now,
        )

    authority.release(
        manifest,
        attempt_id="attempt-one",
        owner_nonce_sha256=first_owner,
    )
    _acquire(
        authority,
        manifest,
        pending,
        attempt="attempt-two",
        now=now,
    )


def test_expired_pre_delivery_attempt_can_be_replaced(tmp_path: Path) -> None:
    _, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    started = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    first_owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="expired-validating",
        now=started,
        ttl_s=1.0,
    )

    second_owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="replacement",
        now=started + timedelta(seconds=2),
    )
    with pytest.raises(DurableAuthorityBusy, match="no longer owned"):
        authority.before_delivery(
            manifest,
            attempt_id="expired-validating",
            owner_nonce_sha256=first_owner,
        )
    authority.before_delivery(
        manifest,
        attempt_id="replacement",
        owner_nonce_sha256=second_owner,
    )


def test_expired_post_delivery_attempt_requires_reconciliation(
    tmp_path: Path,
) -> None:
    _, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    started = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    first_owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="expired-delivery",
        now=started,
        ttl_s=1.0,
    )
    authority.before_delivery(
        manifest,
        attempt_id="expired-delivery",
        owner_nonce_sha256=first_owner,
    )

    with pytest.raises(DurableAuthorityBusy, match="reconciliation is required"):
        _acquire(
            authority,
            manifest,
            pending,
            attempt="unsafe-replacement",
            now=started + timedelta(seconds=2),
        )
    with pytest.raises(DurableAuthorityBusy, match="reconciliation_required"):
        authority.validate(manifest)


def test_reject_before_delivery_preempts_attempt(tmp_path: Path) -> None:
    _, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="pre-delivery-reject",
        now=now,
    )

    assert (
        authority.request_reject(
            expected_run_id=manifest.run_id,
            expected_pause_binding=approval_pause_digest(pending),
        )
        == "preempted"
    )
    with pytest.raises(DurableAuthorityBusy, match="rejected"):
        authority.before_delivery(
            manifest,
            attempt_id="pre-delivery-reject",
            owner_nonce_sha256=owner,
        )


def test_reject_after_delivery_requires_reconciliation(tmp_path: Path) -> None:
    _, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="post-delivery-reject",
        now=now,
    )
    authority.before_delivery(
        manifest,
        attempt_id="post-delivery-reject",
        owner_nonce_sha256=owner,
    )

    assert (
        authority.request_reject(
            expected_run_id=manifest.run_id,
            expected_pause_binding=approval_pause_digest(pending),
        )
        == "uncertain"
    )
    with pytest.raises(DurableAuthorityBusy, match="reconciliation_required"):
        authority.validate(manifest)


def test_missing_external_authority_fails_closed(tmp_path: Path) -> None:
    _, store, manifest, authority = _fresh_run(tmp_path)
    authority.db_path.unlink()

    with pytest.raises(DurableAuthorityBusy, match="missing"):
        store.validate_namespace(manifest)


def test_corrupt_external_authority_fails_closed(tmp_path: Path) -> None:
    _, store, manifest, authority = _fresh_run(tmp_path)
    authority.db_path.write_bytes(b"not a sqlite database")

    with pytest.raises(StateDiverged, match="authority is unavailable"):
        store.validate_namespace(manifest)


def test_attended_journal_hmac_tamper_fails_closed(tmp_path: Path) -> None:
    _actions, authority, manifest = _attended_context(tmp_path)
    snapshot = AttendedDecisionLog(decisions=[_decision(1)]).model_dump_json(indent=2)
    authority.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=authority.read_attended_snapshot(
            expected_run_id=manifest.run_id
        )[1],
        snapshot_json=snapshot,
    )
    with sqlite3.connect(authority.db_path) as connection:
        connection.execute(
            "UPDATE attended_journal SET record_mac = ? "
            "WHERE path_key = ? AND sequence = 1",
            ("hmac-sha256:" + ("0" * 64), authority.path_key),
        )

    with pytest.raises(DurableAuthorityBusy, match="HMAC does not verify"):
        authority.read_attended_snapshot(expected_run_id=manifest.run_id)


def test_attended_journal_hash_chain_tamper_fails_closed(tmp_path: Path) -> None:
    _actions, authority, manifest = _attended_context(tmp_path)
    first_decision = _decision(1)
    first = AttendedDecisionLog(decisions=[first_decision]).model_dump_json(indent=2)
    first_head = authority.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=authority.read_attended_snapshot(
            expected_run_id=manifest.run_id
        )[1],
        snapshot_json=first,
    )
    second = AttendedDecisionLog(
        decisions=[first_decision, _decision(2)]
    ).model_dump_json(indent=2)
    authority.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=first_head,
        snapshot_json=second,
    )
    with sqlite3.connect(authority.db_path) as connection:
        connection.execute(
            "UPDATE attended_journal SET previous_record_digest = ? "
            "WHERE path_key = ? AND sequence = 2",
            ("sha256:" + ("f" * 64), authority.path_key),
        )

    with pytest.raises(DurableAuthorityBusy, match="chain is invalid"):
        authority.read_attended_snapshot(expected_run_id=manifest.run_id)


def test_deleted_attended_journal_tail_cannot_restore_an_older_snapshot(
    tmp_path: Path,
) -> None:
    _actions, authority, manifest = _attended_context(tmp_path)
    first_decision = _decision(1)
    first = AttendedDecisionLog(decisions=[first_decision]).model_dump_json(indent=2)
    first_head = authority.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=authority.read_attended_snapshot(
            expected_run_id=manifest.run_id
        )[1],
        snapshot_json=first,
    )
    second = AttendedDecisionLog(
        decisions=[first_decision, _decision(2)]
    ).model_dump_json(indent=2)
    authority.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=first_head,
        snapshot_json=second,
    )
    with sqlite3.connect(authority.db_path) as connection:
        connection.execute(
            "DELETE FROM attended_journal WHERE path_key = ? AND sequence = 2",
            (authority.path_key,),
        )

    with pytest.raises(DurableAuthorityBusy, match="tail was restored"):
        authority.read_attended_snapshot(expected_run_id=manifest.run_id)


def test_attended_snapshot_must_extend_the_authenticated_log(tmp_path: Path) -> None:
    _actions, authority, manifest = _attended_context(tmp_path)
    first = AttendedDecisionLog(decisions=[_decision(1)]).model_dump_json(indent=2)
    head = authority.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=authority.read_attended_snapshot(
            expected_run_id=manifest.run_id
        )[1],
        snapshot_json=first,
    )

    with pytest.raises(DurableAuthorityBusy, match="not append-only"):
        authority.append_attended_snapshot(
            expected_run_id=manifest.run_id,
            expected_head_digest=head,
            snapshot_json=AttendedDecisionLog().model_dump_json(indent=2),
        )


def test_authority_database_cannot_be_restored_with_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    monkeypatch.setenv(AUTHORITY_DB_ENV, str(run_dir / "authority.sqlite3"))

    with pytest.raises(DurableAuthorityBusy, match="outside the run directory"):
        DurableAuthority(run_dir, CheckpointStore(run_dir))


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation needs privilege")
def test_attended_journal_key_must_not_be_a_symlink(tmp_path: Path) -> None:
    _actions, authority, manifest = _attended_context(tmp_path)
    target = tmp_path / "attacker-key"
    target.write_bytes(b"x" * 32)
    target.chmod(0o600)
    key_path = authority.db_path.with_name(authority.db_path.name + ".journal-key")
    key_path.symlink_to(target)
    snapshot = AttendedDecisionLog(decisions=[_decision(1)]).model_dump_json(indent=2)

    with pytest.raises(DurableAuthorityBusy, match="key is unavailable"):
        authority.append_attended_snapshot(
            expected_run_id=manifest.run_id,
            expected_head_digest=authority.read_attended_snapshot(
                expected_run_id=manifest.run_id
            )[1],
            snapshot_json=snapshot,
        )


def test_local_attended_journal_rollback_is_repaired_from_authority(
    tmp_path: Path,
) -> None:
    actions, _authority, _manifest = _attended_context(tmp_path)
    actions.append(_decision(1))
    first_projection = actions.decisions_path.read_bytes()
    actions.append(_decision(2))
    authoritative_projection = actions.decisions_path.read_bytes()
    assert first_projection != authoritative_projection

    actions.decisions_path.write_bytes(first_projection)
    repaired = actions._read_log()  # noqa: SLF001 - test the repair seam

    assert [entry.decision_id for entry in repaired.decisions] == [
        "decision-0001",
        "decision-0002",
    ]
    assert actions.decisions_path.read_bytes() == authoritative_projection


def test_deleted_local_attended_projection_is_repaired_from_authority(
    tmp_path: Path,
) -> None:
    actions, _authority, _manifest = _attended_context(tmp_path)
    decision = _decision(1)
    actions.append(decision)
    authoritative_projection = actions.decisions_path.read_bytes()
    actions.decisions_path.unlink()

    repaired = actions._read_log()  # noqa: SLF001 - test the repair seam

    assert repaired.decisions == [decision]
    assert actions.decisions_path.read_bytes() == authoritative_projection


def test_two_external_attended_writers_use_head_compare_and_swap(
    tmp_path: Path,
) -> None:
    actions, first_writer, manifest = _attended_context(tmp_path)
    second_writer = DurableAuthority(
        actions.run_dir,
        CheckpointStore(actions.run_dir),
    )
    _snapshot_one, shared_head = first_writer.read_attended_snapshot(
        expected_run_id=manifest.run_id
    )
    assert (
        second_writer.read_attended_snapshot(expected_run_id=manifest.run_id)[1]
        == shared_head
    )

    first_decision = _decision(1)
    first_snapshot = AttendedDecisionLog(decisions=[first_decision]).model_dump_json(
        indent=2
    )
    first_head = first_writer.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=shared_head,
        snapshot_json=first_snapshot,
    )
    stale_second_snapshot = AttendedDecisionLog(
        decisions=[_decision(2)]
    ).model_dump_json(indent=2)
    with pytest.raises(DurableAuthorityBusy, match="changed before append"):
        second_writer.append_attended_snapshot(
            expected_run_id=manifest.run_id,
            expected_head_digest=shared_head,
            snapshot_json=stale_second_snapshot,
        )

    merged_snapshot = AttendedDecisionLog(
        decisions=[first_decision, _decision(2)]
    ).model_dump_json(indent=2)
    second_head = second_writer.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=first_head,
        snapshot_json=merged_snapshot,
    )
    retained, retained_head = first_writer.read_attended_snapshot(
        expected_run_id=manifest.run_id
    )
    assert retained == merged_snapshot
    assert retained_head == second_head


def test_crash_after_external_append_repairs_missing_local_projection(
    tmp_path: Path,
) -> None:
    actions, authority, manifest = _attended_context(tmp_path)
    decision = _decision(1)
    snapshot = AttendedDecisionLog(decisions=[decision]).model_dump_json(indent=2)
    _prior_snapshot, prior_head = authority.read_attended_snapshot(
        expected_run_id=manifest.run_id
    )

    # This is the authority-first crash window: SQLite commits, then the process
    # exits before it can publish attended_decisions.json.
    authority.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=prior_head,
        snapshot_json=snapshot,
    )
    assert not actions.decisions_path.exists()

    repaired = actions._read_log()  # noqa: SLF001 - test restart recovery

    assert repaired.decisions == [decision]
    assert actions.decisions_path.read_text() == snapshot
