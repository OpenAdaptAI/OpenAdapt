"""Edge contracts for the external durable authority.

These tests cover restart and filesystem boundaries that must fail closed.  A
crash before input delivery can be recovered for the exact run.  A crash after
the delivery fence can never make the same pause eligible for automatic retry.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.runtime.durable import authority as authority_module
from openadapt_flow.runtime.durable.approval import approval_pause_digest
from openadapt_flow.runtime.durable.authority import (
    AUTHORITY_DB_ENV,
    DurableAuthority,
    DurableAuthorityBusy,
)
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    PendingEscalation,
    RunManifest,
)


def _manifest(tmp_path: Path) -> tuple[Path, CheckpointStore, RunManifest]:
    run_dir = tmp_path / "run"
    bundle_dir = tmp_path / "bundle"
    Workflow(
        name="authority-edge-contract",
        steps=[
            Step(
                id="submit-case",
                intent="submit the case",
                action=ActionKind.KEY,
                key="ENTER",
            )
        ],
    ).save(bundle_dir)
    store = CheckpointStore(run_dir)
    manifest = RunManifest(
        run_id="run-authority-edge-v13",
        namespace_id="namespace-authority-edge-v13",
        canonical_run_dir=str(run_dir.resolve()),
        workflow_name="authority-edge-contract",
        bundle_dir=str(bundle_dir.resolve()),
        params={"case": "A-100"},
    )
    return run_dir, store, manifest


def _active_pause(
    tmp_path: Path,
) -> tuple[CheckpointStore, RunManifest, DurableAuthority, PendingEscalation]:
    run_dir, store, manifest = _manifest(tmp_path)
    store.write_fresh_manifest(manifest)
    authority = DurableAuthority(run_dir, store)
    record = authority.validate(manifest)
    pending = PendingEscalation(
        run_id=manifest.run_id,
        workflow_name=manifest.workflow_name,
        step_index=0,
        step_id="submit-case",
        intent="submit the case",
        category="human_required",
        reason="operator confirmation is required",
        proposed_options=["continue", "reject"],
        resume_from_index=0,
        params=dict(manifest.params),
    )
    store.write_pending(pending)
    authority.advance(
        manifest,
        expected_progress_digest=record.progress_digest,
        phase="paused",
        pause_binding_sha256=approval_pause_digest(pending),
    )
    return store, manifest, authority, pending


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation needs privilege")
def test_configured_authority_database_symlink_is_never_followed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "external" / "authority.sqlite3"
    configured.parent.mkdir()
    target = tmp_path / "attacker-controlled.sqlite3"
    configured.symlink_to(target)
    monkeypatch.setenv(AUTHORITY_DB_ENV, str(configured))
    run_dir, store, manifest = _manifest(tmp_path)

    with pytest.raises(DurableAuthorityBusy, match="symlink|unavailable"):
        DurableAuthority(run_dir, store).claim(manifest)

    assert not target.exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation needs privilege")
def test_configured_authority_ancestor_symlink_cannot_reenter_run_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, store, manifest = _manifest(tmp_path)
    authority_target = run_dir / "rollback-capable-authority"
    authority_target.mkdir(parents=True)
    external_alias = tmp_path / "external-alias"
    external_alias.symlink_to(run_dir, target_is_directory=True)
    configured = external_alias / authority_target.name / "authority.sqlite3"
    monkeypatch.setenv(AUTHORITY_DB_ENV, str(configured))

    with pytest.raises(
        DurableAuthorityBusy, match="outside the run directory|must not traverse a link"
    ):
        DurableAuthority(run_dir, store).claim(manifest)

    assert not (authority_target / "authority.sqlite3").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation needs privilege")
def test_authority_rejects_ancestor_retargeted_after_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "external" / "ancestor" / "parent" / "authority.sqlite3"
    configured.parent.mkdir(parents=True)
    monkeypatch.setenv(AUTHORITY_DB_ENV, str(configured))
    run_dir, store, manifest = _manifest(tmp_path)
    authority = DurableAuthority(run_dir, store)

    admitted_ancestor = tmp_path / "external" / "ancestor"
    admitted_ancestor.rename(tmp_path / "external" / "admitted-ancestor")
    redirected_parent = run_dir / "redirect" / "parent"
    redirected_parent.mkdir(parents=True)
    admitted_ancestor.symlink_to(run_dir / "redirect", target_is_directory=True)

    with pytest.raises(
        DurableAuthorityBusy, match="ancestor changed|must not traverse a link"
    ):
        authority.claim(manifest)

    assert not (redirected_parent / "authority.sqlite3").exists()


def test_windows_authority_guard_pins_each_path_without_delete_sharing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Win32 guard retains typed, non-reparse handles through SQLite use."""

    configured = tmp_path / "external" / "authority.sqlite3"
    monkeypatch.setenv(AUTHORITY_DB_ENV, str(configured))
    run_dir, store, _manifest_value = _manifest(tmp_path)
    authority = DurableAuthority(run_dir, store)

    class FakeKernel32:
        def __init__(self) -> None:
            self.opened: list[tuple[str, int, int, int, int, int]] = []
            self.closed: list[int] = []
            self.paths: dict[int, str] = {}

        def CreateFileW(
            self,
            path: str,
            access: int,
            share: int,
            _security: object,
            creation: int,
            flags: int,
            _template: object,
        ) -> int:
            handle = (1 << 33) + len(self.opened) + 1
            self.opened.append((path, access, share, creation, flags, handle))
            self.paths[handle] = path
            return handle

        def CloseHandle(self, handle: int) -> bool:
            self.closed.append(handle)
            return True

    fake = FakeKernel32()
    monkeypatch.setattr(authority_module, "_is_windows", lambda: True)
    monkeypatch.setattr(authority_module, "_windows_kernel32", lambda: fake)
    monkeypatch.setattr(
        authority_module,
        "_windows_handle_attributes",
        lambda _kernel32, handle: (
            0 if fake.paths[handle] == str(authority.db_path) else 0x00000010
        ),
    )

    with authority._windows_active_race_handles():
        assert fake.closed == []

    assert fake.opened
    assert all(share == 0x00000003 for _, _, share, _, _, _ in fake.opened)
    assert all(not (share & 0x00000004) for _, _, share, _, _, _ in fake.opened)
    database = fake.opened[-1]
    assert database[0] == str(authority.db_path)
    assert database[1] == 0xC0000000
    assert database[3] == 4
    assert database[4] == 0x00200000
    assert fake.closed == [entry[-1] for entry in reversed(fake.opened)]


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor-relative fences")
def test_replaced_authority_database_cannot_reuse_a_delivery_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final-file swap during SQLite open cannot authorize re-dispatch."""

    configured = tmp_path / "external" / "authority.sqlite3"
    monkeypatch.setenv(AUTHORITY_DB_ENV, str(configured))
    store, manifest, authority, pending = _active_pause(tmp_path)
    binding = approval_pause_digest(pending)
    authority.acquire(
        manifest,
        pause_binding_sha256=binding,
        attempt_id="replace-race",
        operation="continue",
        owner_nonce_sha256="sha256:replace-race",
        acquired_at="2026-07-29T00:00:00+00:00",
        expires_at="2026-07-29T00:10:00+00:00",
        now=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )
    authority.bind_approval(
        manifest,
        attempt_id="replace-race",
        owner_nonce_sha256="sha256:replace-race",
        approval_digest="sha256:replace-race",
    )
    stale = tmp_path / "stale-authority.sqlite3"
    shutil.copy2(authority.db_path, stale)

    authority.before_delivery(
        manifest,
        attempt_id="replace-race",
        owner_nonce_sha256="sha256:replace-race",
    )

    original_connect = sqlite3.connect
    replaced = False

    def replace_between_check_and_open(
        *args: object, **kwargs: object
    ) -> sqlite3.Connection:
        nonlocal replaced
        if not replaced:
            replacement = tmp_path / "replacement-authority.sqlite3"
            shutil.copy2(stale, replacement)
            replacement.replace(authority.db_path)
            replaced = True
        return original_connect(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(
        authority_module.sqlite3, "connect", replace_between_check_and_open
    )

    with pytest.raises(
        DurableAuthorityBusy, match="delivery fence was already consumed"
    ):
        authority.before_delivery(
            manifest,
            attempt_id="replace-race",
            owner_nonce_sha256="sha256:replace-race",
        )
    assert replaced
    restarted = DurableAuthority(store.run_dir, store)
    with pytest.raises(
        DurableAuthorityBusy, match="delivery fence was already consumed"
    ):
        restarted.before_delivery(
            manifest,
            attempt_id="replace-race",
            owner_nonce_sha256="sha256:replace-race",
        )


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor-relative fences")
def test_replaced_authority_ancestor_cannot_reuse_a_delivery_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ancestor swap during SQLite open still uses the admitted fence."""

    configured = tmp_path / "external" / "authority.sqlite3"
    monkeypatch.setenv(AUTHORITY_DB_ENV, str(configured))
    store, manifest, authority, pending = _active_pause(tmp_path)
    binding = approval_pause_digest(pending)
    authority.acquire(
        manifest,
        pause_binding_sha256=binding,
        attempt_id="ancestor-race",
        operation="continue",
        owner_nonce_sha256="sha256:ancestor-race",
        acquired_at="2026-07-29T00:00:00+00:00",
        expires_at="2026-07-29T00:10:00+00:00",
        now=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )
    authority.bind_approval(
        manifest,
        attempt_id="ancestor-race",
        owner_nonce_sha256="sha256:ancestor-race",
        approval_digest="sha256:ancestor-race",
    )
    stale = tmp_path / "stale-ancestor-authority.sqlite3"
    shutil.copy2(authority.db_path, stale)
    authority.before_delivery(
        manifest,
        attempt_id="ancestor-race",
        owner_nonce_sha256="sha256:ancestor-race",
    )

    original_connect = sqlite3.connect
    replaced = False

    def replace_ancestor_between_check_and_open(
        *args: object, **kwargs: object
    ) -> sqlite3.Connection:
        nonlocal replaced
        if not replaced:
            admitted_parent = tmp_path / "admitted-authority"
            redirected_parent = tmp_path / "redirected-authority"
            authority.db_path.parent.rename(admitted_parent)
            redirected_parent.mkdir()
            shutil.copy2(stale, redirected_parent / authority.db_path.name)
            authority.db_path.parent.symlink_to(
                redirected_parent, target_is_directory=True
            )
            replaced = True
        return original_connect(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(
        authority_module.sqlite3,
        "connect",
        replace_ancestor_between_check_and_open,
    )

    with pytest.raises(
        DurableAuthorityBusy, match="delivery fence was already consumed"
    ):
        authority.before_delivery(
            manifest,
            attempt_id="ancestor-race",
            owner_nonce_sha256="sha256:ancestor-race",
        )
    assert replaced
    with pytest.raises(DurableAuthorityBusy, match="must not traverse a link"):
        DurableAuthority(store.run_dir, store)


def test_unexpected_existing_sqlite_schema_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "external" / "authority.sqlite3"
    configured.parent.mkdir()
    with sqlite3.connect(configured) as connection:
        connection.execute("CREATE TABLE durable_authority (unexpected TEXT)")
    if os.name != "nt":
        configured.chmod(0o600)
    monkeypatch.setenv(AUTHORITY_DB_ENV, str(configured))
    run_dir, store, manifest = _manifest(tmp_path)

    with pytest.raises(DurableAuthorityBusy, match="schema is incompatible"):
        DurableAuthority(run_dir, store).claim(manifest)


def test_exact_startup_claim_can_complete_after_a_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "external" / "authority.sqlite3"
    monkeypatch.setenv(AUTHORITY_DB_ENV, str(configured))
    run_dir, store, manifest = _manifest(tmp_path)
    DurableAuthority(run_dir, store).claim(manifest)

    # Simulate a process restart after the external claim commits but before the
    # local namespace and manifest commit.  Only the exact run may finish it.
    store.write_fresh_manifest(manifest)

    record = DurableAuthority(run_dir, store).validate(manifest)
    assert record.phase == "active"
    assert record.run_id == manifest.run_id
    assert record.namespace_id == manifest.namespace_id


def test_crashed_validating_attempt_recovers_only_before_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "external" / "authority.sqlite3"
    monkeypatch.setenv(AUTHORITY_DB_ENV, str(configured))
    store, manifest, authority, pending = _active_pause(tmp_path)
    binding = approval_pause_digest(pending)
    started = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    authority.acquire(
        manifest,
        pause_binding_sha256=binding,
        attempt_id="crashed-before-delivery",
        operation="continue",
        owner_nonce_sha256="sha256:owner-before",
        acquired_at=started.isoformat(),
        expires_at=(started + timedelta(seconds=1)).isoformat(),
        now=started,
    )

    restarted = DurableAuthority(store.run_dir, store)
    restarted.acquire(
        manifest,
        pause_binding_sha256=binding,
        attempt_id="replacement",
        operation="continue",
        owner_nonce_sha256="sha256:owner-replacement",
        acquired_at=(started + timedelta(seconds=2)).isoformat(),
        expires_at=(started + timedelta(seconds=62)).isoformat(),
        now=started + timedelta(seconds=2),
    )
    restarted.before_delivery(
        manifest,
        attempt_id="replacement",
        owner_nonce_sha256="sha256:owner-replacement",
    )

    # A process exit after this fence has an uncertain external effect.  Release
    # must preserve that fact, and no later attempt may retry the same pause.
    restarted.release(
        manifest,
        attempt_id="replacement",
        owner_nonce_sha256="sha256:owner-replacement",
    )
    with pytest.raises(DurableAuthorityBusy, match="reconciliation_required"):
        restarted.validate(manifest)
    with pytest.raises(DurableAuthorityBusy, match="reconciliation_required"):
        restarted.acquire(
            manifest,
            pause_binding_sha256=binding,
            attempt_id="unsafe-retry",
            operation="continue",
            owner_nonce_sha256="sha256:owner-unsafe",
            acquired_at=(started + timedelta(seconds=63)).isoformat(),
            expires_at=(started + timedelta(seconds=123)).isoformat(),
            now=started + timedelta(seconds=63),
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are required")
def test_existing_configured_parent_keeps_its_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "operator-owned-authority"
    parent.mkdir(mode=0o750)
    parent.chmod(0o750)
    configured = parent / "authority.sqlite3"
    monkeypatch.setenv(AUTHORITY_DB_ENV, str(configured))
    _run_dir, store, manifest = _manifest(tmp_path)

    store.write_fresh_manifest(manifest)

    assert stat.S_IMODE(parent.stat().st_mode) == 0o750
