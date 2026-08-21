"""Single-flight coordination for one exact durable continuation."""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

from pydantic import BaseModel, ConfigDict

from openadapt_flow.runtime.durable.approval import (
    ApprovalRecord,
    StateDiverged,
    approval_pause_digest,
)
from openadapt_flow.runtime.durable.checkpoint import CheckpointStore

LEASE_FILENAME = ".attended_action.lease"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ContinuationBusy(StateDiverged):
    """A different process owns this exact durable continuation."""


class ContinuationLeaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    attempt_id: str
    run_id: str
    pause_binding_sha256: str
    operation: Literal[
        "resume", "continue", "skip", "reject", "teach", "escalate", "reconcile"
    ]
    owner_nonce_sha256: str
    phase: Literal[
        "validating",
        "delivery_started",
        "completed",
        "aborted_pre_delivery",
        "reconciliation_required",
        "terminal_prepared",
    ] = "validating"
    reject_requested: bool = False
    delivery_sequence: int = 0
    progress_digest: str = ""
    acquired_at: str
    expires_at: str


class ContinuationToken(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str
    run_id: str
    pause_binding_sha256: str
    owner_nonce: str


_CURRENT_TOKEN: ContextVar[Optional[ContinuationToken]] = ContextVar(
    "openadapt_durable_continuation_token", default=None
)


def current_continuation_token() -> Optional[ContinuationToken]:
    return _CURRENT_TOKEN.get()


@contextmanager
def activate_continuation_token(
    token: ContinuationToken,
) -> Iterator[ContinuationToken]:
    """Install one exact continuation token in a trusted owner thread.

    Thread-affine backends move execution to a dedicated owner thread. Copying
    the complete caller context would also copy unrelated request state. This
    narrow bridge carries only the immutable token that already owns the lease.
    """

    current = _CURRENT_TOKEN.get()
    if current is not None and current != token:
        raise ContinuationBusy(
            "the owner thread already carries a different continuation token"
        )
    context = _CURRENT_TOKEN.set(token)
    try:
        yield token
    finally:
        _CURRENT_TOKEN.reset(context)


class ContinuationCoordinator:
    """Coordinate direct and attended continuations through one local lease."""

    def __init__(self, run_dir: Path | str, *, key: Optional[str] = None) -> None:
        self.run_dir = Path(run_dir)
        self.store = CheckpointStore(self.run_dir, key=key)
        self.path = self.run_dir / LEASE_FILENAME
        from openadapt_flow.runtime.durable.authority import DurableAuthority

        self.authority = DurableAuthority(self.run_dir, self.store)

    @staticmethod
    def _nonce_digest(nonce: str) -> str:
        return "sha256:" + hashlib.sha256(nonce.encode("utf-8")).hexdigest()

    def _read(self) -> Optional[ContinuationLeaseRecord]:
        if not self.path.is_file():
            return None
        try:
            return ContinuationLeaseRecord.model_validate_json(self.path.read_text())
        except (OSError, ValueError) as exc:
            raise ContinuationBusy(
                "the retained continuation lease is invalid; reconcile it before retry"
            ) from exc

    def _write(self, record: ContinuationLeaseRecord) -> None:
        self.store._atomic_write_bytes(  # noqa: SLF001 - shared durable primitive
            self.path, record.model_dump_json(indent=2).encode("utf-8")
        )

    def _progress_digest(self) -> str:
        return self.store.continuation_state_digest()

    def _progress_digest_unlocked(self) -> str:
        return self.store._continuation_state_digest_unlocked()  # noqa: SLF001

    def _validate_owned(self, token: ContinuationToken) -> ContinuationLeaseRecord:
        record = self._read()
        if (
            record is None
            or record.attempt_id != token.attempt_id
            or record.run_id != token.run_id
            or record.pause_binding_sha256 != token.pause_binding_sha256
            or record.owner_nonce_sha256 != self._nonce_digest(token.owner_nonce)
        ):
            raise ContinuationBusy(
                "the durable continuation lease is no longer owned by this attempt"
            )
        return record

    @contextmanager
    def lease(
        self,
        *,
        operation: Literal[
            "resume", "continue", "skip", "reject", "teach", "escalate", "reconcile"
        ],
        ttl_s: float = 15 * 60.0,
        now: Optional[datetime] = None,
        wait_s: float = 0.0,
    ) -> Iterator[ContinuationToken]:
        """Acquire or reuse one exact-pause continuation lease."""

        manifest = self.store.read_manifest()
        pending = self.store.read_pending()
        if manifest is None or pending is None:
            raise ContinuationBusy("the run is not durably paused")
        binding = approval_pause_digest(pending)
        current = _CURRENT_TOKEN.get()
        if (
            operation == "resume"
            and current is not None
            and current.run_id == manifest.run_id
            and current.pause_binding_sha256 == binding
        ):
            active_record = self._validate_owned(current)
            if active_record.operation not in {"continue", "skip", "reconcile"}:
                raise ContinuationBusy(
                    "a direct continuation cannot recursively resume itself"
                )
            yield current
            return

        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            now_value = now or _now()
            nonce = secrets.token_hex(32)
            record = ContinuationLeaseRecord(
                attempt_id=secrets.token_hex(16),
                run_id=manifest.run_id,
                pause_binding_sha256=binding,
                operation=operation,
                owner_nonce_sha256=self._nonce_digest(nonce),
                progress_digest=self._progress_digest(),
                acquired_at=_iso(now_value),
                expires_at=_iso(now_value + timedelta(seconds=max(1.0, float(ttl_s)))),
            )
            payload = record.model_dump_json(indent=2).encode("utf-8")
            self.run_dir.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                retained = self._read()
                if retained is not None and _parse(retained.expires_at) < now_value:
                    if retained.phase in {
                        "validating",
                        "completed",
                        "aborted_pre_delivery",
                    }:
                        with self.store.state_lock():
                            current_record = self._read()
                            if current_record == retained:
                                self.path.unlink()
                                self.store._fsync_directory(  # noqa: SLF001
                                    self.run_dir
                                )
                                continue
                    raise ContinuationBusy(
                        "a continuation lease expired without a terminal receipt; "
                        "reconcile it before retry"
                    ) from None
                if time.monotonic() < deadline:
                    time.sleep(0.01)
                    continue
                raise ContinuationBusy(
                    "another continuation is already in progress"
                ) from None
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self.store._fsync_directory(self.run_dir)  # noqa: SLF001
            token = ContinuationToken(
                attempt_id=record.attempt_id,
                run_id=record.run_id,
                pause_binding_sha256=record.pause_binding_sha256,
                owner_nonce=nonce,
            )
            try:
                self.authority.acquire(
                    manifest,
                    pause_binding_sha256=binding,
                    attempt_id=record.attempt_id,
                    operation=operation,
                    owner_nonce_sha256=record.owner_nonce_sha256,
                    acquired_at=record.acquired_at,
                    expires_at=record.expires_at,
                    now=now_value,
                )
            except Exception:
                self._release_local(token)
                raise
            context = _CURRENT_TOKEN.set(token)
            try:
                yield token
            finally:
                _CURRENT_TOKEN.reset(context)
                self.release(token)
            return

    def before_delivery(self, token: ContinuationToken) -> Any:
        """Linearize Reject against the first resumed delivery boundary."""

        manifest = self.store.read_manifest()
        if manifest is None:
            raise ContinuationBusy("the durable manifest disappeared before delivery")
        try:
            remote_permit = self.authority.before_delivery(
                manifest,
                attempt_id=token.attempt_id,
                owner_nonce_sha256=self._nonce_digest(token.owner_nonce),
            )
        except StateDiverged as exc:
            raise ContinuationBusy(str(exc)) from exc
        with self.store.state_lock():
            record = self._validate_owned(token)
            if _parse(record.expires_at) < _now():
                raise ContinuationBusy(
                    "the continuation lease expired before delivery; reconcile it"
                )
            pending = self.store.read_pending()
            if (
                pending is None
                or pending.run_id != token.run_id
                or approval_pause_digest(pending) != token.pause_binding_sha256
                or pending.status == "rejected"
                or record.reject_requested
            ):
                raise ContinuationBusy(
                    "the exact durable pause was rejected or changed before delivery"
                )
            if record.phase == "delivery_started":
                self._write(
                    record.model_copy(
                        update={
                            "delivery_sequence": record.delivery_sequence + 1,
                        }
                    )
                )
                return remote_permit
            if record.phase != "validating":
                raise ContinuationBusy(
                    "the continuation attempt is not eligible for delivery"
                )
            self._write(
                record.model_copy(
                    update={
                        "phase": "delivery_started",
                        "delivery_sequence": record.delivery_sequence + 1,
                        "progress_digest": self._progress_digest_unlocked(),
                    }
                )
            )
        return remote_permit

    def acknowledge_delivery(
        self, token: ContinuationToken, remote_permit: Any
    ) -> None:
        """Commit the receipt for the exact backend edge that just returned."""

        if remote_permit is None:
            return
        manifest = self.store.read_manifest()
        if manifest is None:
            raise ContinuationBusy("the durable manifest disappeared after delivery")
        try:
            self.authority.acknowledge_remote_delivery(
                manifest,
                remote_permit,
                attempt_id=token.attempt_id,
                owner_nonce_sha256=self._nonce_digest(token.owner_nonce),
            )
        except StateDiverged as exc:
            raise ContinuationBusy(str(exc)) from exc

    def bind_approval(self, token: ContinuationToken, approval: ApprovalRecord) -> None:
        """Bind the exact admitted approval in the external authority."""

        manifest = self.store.read_manifest()
        if manifest is None:
            raise ContinuationBusy("the durable manifest disappeared before approval")
        try:
            self.authority.bind_approval(
                manifest,
                attempt_id=token.attempt_id,
                owner_nonce_sha256=self._nonce_digest(token.owner_nonce),
                approval_digest=self.store.model_digest(approval),
            )
        except StateDiverged as exc:
            raise ContinuationBusy(str(exc)) from exc

    def request_reject(
        self,
        *,
        expected_run_id: str,
        expected_pause_binding: str,
    ) -> Literal["none", "preempted", "uncertain"]:
        """Request rejection without waiting behind a validating continuation."""

        try:
            external = self.authority.request_reject(
                expected_run_id=expected_run_id,
                expected_pause_binding=expected_pause_binding,
            )
        except StateDiverged as exc:
            raise ContinuationBusy(str(exc)) from exc
        with self.store.state_lock():
            record = self._read()
            if record is None:
                return external
            if (
                record.run_id != expected_run_id
                or record.pause_binding_sha256 != expected_pause_binding
            ):
                raise ContinuationBusy(
                    "the active continuation belongs to a different pause"
                )
            pending = self.store.read_pending()
            if (
                pending is None
                or pending.run_id != record.run_id
                or approval_pause_digest(pending) != record.pause_binding_sha256
            ):
                raise ContinuationBusy(
                    "the active continuation does not match this pause"
                )
            if record.phase == "delivery_started" or (
                record.phase == "validating" and record.delivery_sequence > 0
            ):
                self._write(
                    record.model_copy(update={"phase": "reconciliation_required"})
                )
                if external != "uncertain":
                    raise ContinuationBusy(
                        "local and external continuation fences disagree"
                    )
                return "uncertain"
            if record.phase != "validating":
                raise ContinuationBusy(
                    "the continuation cannot be cleanly rejected in its current phase"
                )
            self._write(record.model_copy(update={"reject_requested": True}))
            if external != "preempted":
                raise ContinuationBusy(
                    "local and external continuation fences disagree"
                )
            return "preempted"

    def acknowledge_progress(
        self,
        token: ContinuationToken,
        *,
        terminal: bool = False,
        external_delivery: bool = False,
    ) -> None:
        """Bind one dispatched action to its durable checkpoint or pause."""

        manifest = self.store.read_manifest()
        if manifest is None:
            raise ContinuationBusy("the durable manifest disappeared before commit")
        try:
            self.authority.acknowledge_progress(
                manifest,
                attempt_id=token.attempt_id,
                owner_nonce_sha256=self._nonce_digest(token.owner_nonce),
                terminal_pause=terminal,
                external_delivery=external_delivery,
            )
        except StateDiverged as exc:
            raise ContinuationBusy(str(exc)) from exc
        with self.store.state_lock():
            record = self._validate_owned(token)
            if record.phase not in {"validating", "delivery_started"}:
                raise ContinuationBusy(
                    "the continuation cannot acknowledge progress in this phase"
                )
            current = self._progress_digest_unlocked()
            if current == record.progress_digest:
                raise ContinuationBusy(
                    "the continuation has no new durable progress to acknowledge"
                )
            self._write(
                record.model_copy(
                    update={
                        "phase": "completed" if terminal else "validating",
                        "progress_digest": current,
                        "delivery_sequence": (
                            max(1, record.delivery_sequence)
                            if external_delivery
                            else record.delivery_sequence
                        ),
                    }
                )
            )

    def attest_executor_outcome(
        self,
        token: ContinuationToken,
        *,
        status: Literal["completed", "refused", "halted"],
        report_success: Optional[bool],
        source_pause_binding: str,
    ) -> None:
        """Accept an executor receipt only when durable authority proves it."""

        manifest = self.store.read_manifest()
        if manifest is None:
            raise ContinuationBusy(
                "the durable manifest disappeared before executor attestation"
            )
        try:
            self.authority.attest_executor_outcome(
                manifest,
                attempt_id=token.attempt_id,
                owner_nonce_sha256=self._nonce_digest(token.owner_nonce),
                status=status,
                report_success=report_success,
                source_pause_binding=source_pause_binding,
            )
        except StateDiverged as exc:
            raise ContinuationBusy(str(exc)) from exc

    def prove_executor_outcome(
        self,
        token: ContinuationToken,
        *,
        source_pause_binding: str,
    ) -> Optional[tuple[Literal["completed", "refused", "halted"], bool]]:
        """Recover an exact result after the executor transport fails."""

        manifest = self.store.read_manifest()
        if manifest is None:
            raise ContinuationBusy(
                "the durable manifest disappeared before outcome recovery"
            )
        try:
            return self.authority.prove_executor_outcome(
                manifest,
                attempt_id=token.attempt_id,
                owner_nonce_sha256=self._nonce_digest(token.owner_nonce),
                source_pause_binding=source_pause_binding,
            )
        except StateDiverged as exc:
            raise ContinuationBusy(str(exc)) from exc

    def prove_completed_pause(self, *, source_pause_binding: str) -> bool:
        """Prove an already terminal completion after an executor crash."""

        manifest = self.store.read_manifest()
        if manifest is None:
            raise ContinuationBusy(
                "the durable manifest disappeared before terminal recovery"
            )
        try:
            return self.authority.prove_completed_pause(
                manifest, source_pause_binding=source_pause_binding
            )
        except StateDiverged as exc:
            raise ContinuationBusy(str(exc)) from exc

    def mark_executor_uncertain(self, token: ContinuationToken) -> None:
        """Permanently fence an executor call that lacks a proven receipt."""

        manifest = self.store.read_manifest()
        if manifest is None:
            raise ContinuationBusy(
                "the durable manifest disappeared before executor fencing"
            )
        try:
            self.authority.mark_executor_uncertain(
                manifest,
                attempt_id=token.attempt_id,
                owner_nonce_sha256=self._nonce_digest(token.owner_nonce),
            )
        except StateDiverged as exc:
            raise ContinuationBusy(str(exc)) from exc
        with self.store.state_lock():
            record = self._validate_owned(token)
            self._write(record.model_copy(update={"phase": "reconciliation_required"}))

    def prepare_terminal(self, token: ContinuationToken, report_path: Path) -> str:
        """Fence one exact persisted report before consuming its pause."""

        manifest = self.store.read_manifest()
        if manifest is None:
            raise ContinuationBusy(
                "the durable manifest disappeared at terminal commit"
            )
        report_sha256 = "sha256:" + hashlib.sha256(report_path.read_bytes()).hexdigest()
        try:
            self.authority.prepare_terminal(
                manifest,
                attempt_id=token.attempt_id,
                owner_nonce_sha256=self._nonce_digest(token.owner_nonce),
                report_sha256=report_sha256,
            )
        except (OSError, StateDiverged) as exc:
            raise ContinuationBusy(str(exc)) from exc
        with self.store.state_lock():
            record = self._validate_owned(token)
            if (
                record.phase in {"delivery_started", "reconciliation_required"}
                or record.reject_requested
            ):
                raise ContinuationBusy(
                    "the continuation cannot prepare terminal commit"
                )
            self._write(record.model_copy(update={"phase": "terminal_prepared"}))
        return report_sha256

    def completed(
        self,
        token: ContinuationToken,
        *,
        report_sha256: str,
    ) -> None:
        """Commit exact terminal report after the active pause is consumed."""

        manifest = self.store.read_manifest()
        if manifest is None:
            raise ContinuationBusy(
                "the durable manifest disappeared at terminal commit"
            )
        try:
            self.authority.finalize_terminal(
                manifest,
                attempt_id=token.attempt_id,
                owner_nonce_sha256=self._nonce_digest(token.owner_nonce),
                report_sha256=report_sha256,
            )
        except StateDiverged as exc:
            raise ContinuationBusy(str(exc)) from exc

        with self.store.state_lock():
            record = self._validate_owned(token)
            if record.phase != "terminal_prepared":
                raise ContinuationBusy(
                    "the local continuation was not prepared for terminal commit"
                )
            self._write(
                record.model_copy(
                    update={
                        "phase": "completed",
                        "progress_digest": self._progress_digest_unlocked(),
                    }
                )
            )

    def settle_terminal_projection(
        self, token: ContinuationToken, *, report_sha256: str
    ) -> None:
        """Make an exact completed report attestable after ledger projection."""

        # Validate the local owner before changing the external authority. A
        # stale or replaced lease must not settle a different attempt.
        with self.store.state_lock():
            record = self._validate_owned(token)
            if record.phase != "completed":
                raise ContinuationBusy("the local continuation is not completed")
        manifest = self.store.read_manifest()
        if manifest is None:
            raise ContinuationBusy(
                "the durable manifest disappeared after terminal completion"
            )
        try:
            self.authority.settle_terminal_projection(
                manifest,
                report_sha256=report_sha256,
            )
        except StateDiverged as exc:
            raise ContinuationBusy(str(exc)) from exc

    def correct_terminal_projection_failure(
        self,
        token: ContinuationToken,
        *,
        original_report_sha256: str,
        corrected_report_path: Path,
    ) -> str:
        """Bind the one allowed fail-closed terminal report correction."""

        # Validate the owner before the external mutation. The local lease is
        # the continuation capability and must still name this exact attempt.
        with self.store.state_lock():
            record = self._validate_owned(token)
            if record.phase != "completed":
                raise ContinuationBusy("the local continuation is not completed")
        try:
            corrected_report_json = corrected_report_path.read_bytes()
        except OSError as exc:
            raise ContinuationBusy(
                "the corrected terminal report is unavailable"
            ) from exc
        manifest = self.store.read_manifest()
        if manifest is None:
            raise ContinuationBusy(
                "the durable manifest disappeared after terminal completion"
            )
        corrected_report_sha256 = (
            "sha256:" + hashlib.sha256(corrected_report_json).hexdigest()
        )
        try:
            self.authority.correct_terminal_projection_failure(
                manifest,
                original_report_sha256=original_report_sha256,
                corrected_report_sha256=corrected_report_sha256,
                corrected_report_json=corrected_report_json,
            )
        except (OSError, StateDiverged) as exc:
            raise ContinuationBusy(str(exc)) from exc
        return corrected_report_sha256

    def release(self, token: ContinuationToken) -> None:
        """Remove only the exact lease file this token still owns."""

        manifest = self.store.read_manifest()
        if manifest is not None:
            self.authority.release(
                manifest,
                attempt_id=token.attempt_id,
                owner_nonce_sha256=self._nonce_digest(token.owner_nonce),
            )
        self._release_local(token)

    def _release_local(self, token: ContinuationToken) -> None:
        """Remove only the exact diagnostic local lease owned by ``token``."""

        with self.store.state_lock():
            try:
                record = self._validate_owned(token)
            except ContinuationBusy:
                return
            if record.phase in {"reconciliation_required", "terminal_prepared"}:
                return
            if record.phase == "delivery_started":
                self._write(
                    record.model_copy(update={"phase": "reconciliation_required"})
                )
                return
            try:
                owner_stat = os.lstat(self.path)
                retained = self._read()
                current_stat = os.lstat(self.path)
                if (
                    retained is not None
                    and retained.attempt_id == token.attempt_id
                    and retained.owner_nonce_sha256
                    == self._nonce_digest(token.owner_nonce)
                    and owner_stat.st_dev == current_stat.st_dev
                    and owner_stat.st_ino == current_stat.st_ino
                ):
                    self.path.unlink()
                    self.store._fsync_directory(self.run_dir)  # noqa: SLF001
            except FileNotFoundError:
                return


class ContinuationGuard:
    """Replayer hook called at every real delivery boundary."""

    def __init__(
        self,
        coordinator: ContinuationCoordinator,
        token: ContinuationToken,
    ) -> None:
        self.coordinator = coordinator
        self.token = token
        self._pending_remote_permit: Any = None

    def before_delivery(self) -> None:
        if self._pending_remote_permit is not None:
            raise ContinuationBusy(
                "a prior production delivery lacks an acknowledgment receipt"
            )
        self._pending_remote_permit = self.coordinator.before_delivery(self.token)

    def acknowledge_delivery(self) -> None:
        pending = self._pending_remote_permit
        self.coordinator.acknowledge_delivery(self.token, pending)
        self._pending_remote_permit = None

    def bind_approval(self, approval: ApprovalRecord) -> None:
        self.coordinator.bind_approval(self.token, approval)

    def acknowledge_progress(
        self,
        *,
        terminal: bool = False,
        external_delivery: bool = False,
    ) -> None:
        self.coordinator.acknowledge_progress(
            self.token,
            terminal=terminal,
            external_delivery=external_delivery,
        )

    def prepare_terminal(self, report_path: Path) -> str:
        return self.coordinator.prepare_terminal(self.token, report_path)

    def completed(self, *, report_sha256: str) -> None:
        self.coordinator.completed(
            self.token,
            report_sha256=report_sha256,
        )

    def settle_terminal_projection(self, *, report_sha256: str) -> None:
        self.coordinator.settle_terminal_projection(
            self.token,
            report_sha256=report_sha256,
        )

    def correct_terminal_projection_failure(
        self,
        *,
        original_report_sha256: str,
        corrected_report_path: Path,
    ) -> str:
        return self.coordinator.correct_terminal_projection_failure(
            self.token,
            original_report_sha256=original_report_sha256,
            corrected_report_path=corrected_report_path,
        )
