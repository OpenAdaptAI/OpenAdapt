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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import pytest

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


def test_production_delivery_requires_and_validates_remote_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def transport(_url: str, headers: dict[str, str], body: bytes) -> bytes:
        request = json.loads(body)
        seen.update(headers=headers, request=request)
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
        return json.dumps(response).encode()

    manifest, authority, owner = _remote_ready_authority(
        tmp_path, monkeypatch, transport
    )
    authority.before_delivery(
        manifest,
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
    monkeypatch.delenv(REMOTE_AUTHORITY_URL_ENV)
    with pytest.raises(DurableAuthorityBusy, match="requires configured remote"):
        authority.before_delivery(
            manifest,
            attempt_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            owner_nonce_sha256=owner,
        )
    assert not called
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

    def transport(_url: str, _headers: dict[str, str], body: bytes) -> bytes:
        request = json.loads(body)
        requests.append(request)
        return json.dumps(
            {
                "schema_version": 1,
                "status": "issued",
                "authority_id": "22222222-2222-4222-8222-222222222222",
                "permit_id": f"permit-{len(requests)}",
                "request_sha256": hashlib.sha256(body).hexdigest(),
                "next_permit_cursor": hashlib.sha256(
                    f"cursor-{len(requests)}".encode()
                ).hexdigest(),
                "delivery_sequence": request["delivery_sequence"],
                "issued_at": "2026-07-29T00:00:00+00:00",
            }
        ).encode()

    _run_dir, _store, manifest, authority = _fresh_run(
        tmp_path, execution_profile="standard"
    )
    monkeypatch.setenv(REMOTE_AUTHORITY_URL_ENV, "https://control.example/permit")
    monkeypatch.setenv(REMOTE_AUTHORITY_TOKEN_ENV, "token")
    authority = DurableAuthority(
        authority.run_dir, authority.store, remote_transport=transport
    )
    authority.before_initial_delivery(manifest)
    authority.before_initial_delivery(manifest)
    assert [request["delivery_sequence"] for request in requests] == [0, 1]
    assert [request["remote_delivery_sequence"] for request in requests] == [0, 1]
    assert requests[0]["permit_cursor"] is None
    assert requests[1]["permit_cursor"] is not None
    assert authority.validate(manifest).delivery_sequence == 2


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

    def transport(_url: str, _headers: dict[str, str], body: bytes) -> bytes:
        request = json.loads(body)
        key = (
            request["run_id"],
            request["pause_binding_sha256"],
            request["delivery_sequence"],
        )
        if key in consumed:
            raise OSError("409 duplicate delivery sequence")
        consumed.add(key)
        return json.dumps(
            {
                "schema_version": 1,
                "status": "issued",
                "authority_id": "22222222-2222-4222-8222-222222222222",
                "permit_id": f"permit-{len(consumed)}",
                "request_sha256": hashlib.sha256(body).hexdigest(),
                "next_permit_cursor": hashlib.sha256(
                    ("cursor:" + (request["permit_cursor"] or "genesis")).encode()
                ).hexdigest(),
                "delivery_sequence": request["delivery_sequence"],
                "issued_at": "2026-07-29T00:00:00+00:00",
            }
        ).encode()

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

    def transport(_url: str, _headers: dict[str, str], body: bytes) -> bytes:
        nonlocal expected_sequence
        request = json.loads(body)
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
        return json.dumps(
            {
                "schema_version": 1,
                "status": "issued",
                "authority_id": "22222222-2222-4222-8222-222222222222",
                "permit_id": f"permit-{expected_sequence}",
                "request_sha256": hashlib.sha256(body).hexdigest(),
                "next_permit_cursor": hashlib.sha256(
                    ("cursor:" + (request["permit_cursor"] or "genesis")).encode()
                ).hexdigest(),
                "delivery_sequence": request["delivery_sequence"],
                "issued_at": "2026-07-29T00:00:00+00:00",
            }
        ).encode()

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

    authority.before_delivery(
        manifest,
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
    authority.before_delivery(
        manifest,
        attempt_id="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        owner_nonce_sha256=owner,
    )

    assert [sequence for sequence, _digest, _remote, _cursor in observed] == [0, 1]
    assert [remote for _sequence, _digest, remote, _cursor in observed] == [0, 1]
    assert observed[0][3] is None
    assert isinstance(observed[1][3], str) and len(observed[1][3]) == 64
    assert observed[0][1] != observed[1][1]


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
