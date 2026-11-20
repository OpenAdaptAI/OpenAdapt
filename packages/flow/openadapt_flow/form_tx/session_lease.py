"""Warm authenticated session lease, protected browser auth handoff, heartbeat, and queue runner.

Maintains an already-authenticated session across sequential form transactions
with source-suppressed human login/2FA handoffs, zero-mutation keepalive checks,
and request-idempotent job execution. Zero application or vendor names.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Literal, Mapping, Optional, Sequence, Union
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.form_tx.surface import FormSurfaceBackend
from openadapt_flow.form_tx.types import (
    FormHaltItem,
    FormHaltKind,
    FormPack,
    FormSnapshot,
    FormTxOutcome,
    FormTxResult,
    InputDossier,
    canonical_json_bytes,
)

BrowserAuthMethod = Literal[
    "password_manager",
    "passkey",
    "sso",
    "mfa",
    "device_unlock",
    "other",
]
BrowserAuthOutcome = Literal["completed", "cancelled", "failed", "aborted"]

DEFAULT_BROWSER_SUPPRESSED_SOURCES: tuple[str, ...] = (
    "dom_values",
    "screenshots",
    "network_bodies",
    "input_events",
)

_FORBIDDEN_CREDENTIAL_FIELDS = frozenset(
    {
        "username",
        "user",
        "login",
        "email",
        "password",
        "passcode",
        "secret",
        "otp",
        "totp",
        "mfa_code",
        "recovery_code",
        "passkey_material",
        "vault_item",
        "account_name",
        "account_id",
        "provider_name",
        "token",
        "cookie",
    }
)


def _utc_now_iso(clock_fn: Optional[Callable[[], datetime]] = None) -> str:
    now = clock_fn() if clock_fn is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return now.isoformat().replace("+00:00", "Z")


def _sanitize_url_origin_and_path(raw_url: str) -> str:
    """Strip query parameters, fragments, and URL userinfo from a URL or path."""
    cleaned = raw_url.strip()
    if not cleaned:
        return ""
    parts = urlsplit(cleaned)
    if parts.scheme and parts.hostname:
        port_suffix = f":{parts.port}" if parts.port is not None else ""
        path = parts.path or "/"
        return f"{parts.scheme}://{parts.hostname}{port_suffix}{path}"
    return parts.path


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tmp-", suffix=".json"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class LeaseState(str, Enum):
    """Lifecycle states of a ``WarmSessionLease``."""

    UNINITIALIZED = "uninitialized"
    AUTH_HANDOFF_OPEN = "auth_handoff_open"
    READY = "ready"
    BUSY_RUNNING_JOB = "busy_running_job"
    AUTH_REQUIRED = "auth_required"
    FAULTED = "faulted"


class SessionLeaseStateError(RuntimeError):
    """Raised when an operation violates the ``WarmSessionLease`` state machine."""


class AuthHandoffError(SessionLeaseStateError):
    """Raised when an authentication handoff boundary is violated."""


class HeartbeatMutationError(RuntimeError):
    """Raised when a session heartbeat mutates server state or returns unexpected status."""


class RequestIdConflictError(RuntimeError):
    """Raised when an existing ``request_id`` is re-submitted with a different payload."""


class BrowserAuthHandoffMarker(BaseModel):
    """Value-free timeline marker for an attended browser authentication interval.

    Mirrors ``openadapt-capture``'s authentication handoff privacy boundary:
    records interval timing, method classes, presence flags, suppressed source
    categories, sanitized pre/post URL paths, and landmark verification status.
    Carries zero fields for usernames, passwords, OTPs, vault items, or accounts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    interval_id: str = Field(min_length=1)
    methods: tuple[BrowserAuthMethod, ...] = Field(min_length=1)
    requires_user_presence: bool
    saved_account_selected: bool
    started_at_utc: str = Field(min_length=1)
    ended_at_utc: str = Field(min_length=1)
    outcome: BrowserAuthOutcome
    suppressed_sources: tuple[str, ...] = DEFAULT_BROWSER_SUPPRESSED_SOURCES
    pre_handoff_url_path: str
    post_handoff_url_path: Optional[str] = None
    authenticated_landmark_verified: bool = False

    @model_validator(mode="after")
    def _validate_privacy_and_contract(self) -> "BrowserAuthHandoffMarker":
        try:
            uuid.UUID(self.interval_id)
        except ValueError as exc:
            raise ValueError("interval_id must be a valid UUID string") from exc
        if len(set(self.methods)) != len(self.methods):
            raise ValueError("authentication handoff methods must be unique")
        missing_sources = set(DEFAULT_BROWSER_SUPPRESSED_SOURCES) - set(
            self.suppressed_sources
        )
        if missing_sources:
            raise ValueError(
                f"BrowserAuthHandoffMarker must suppress all browser sources; missing {sorted(missing_sources)}"
            )
        sanitized_pre = _sanitize_url_origin_and_path(self.pre_handoff_url_path)
        if sanitized_pre != self.pre_handoff_url_path:
            raise ValueError(
                "pre_handoff_url_path must not contain query parameters, fragments, or userinfo"
            )
        if self.post_handoff_url_path is not None:
            sanitized_post = _sanitize_url_origin_and_path(self.post_handoff_url_path)
            if sanitized_post != self.post_handoff_url_path:
                raise ValueError(
                    "post_handoff_url_path must not contain query parameters, fragments, or userinfo"
                )
        if self.outcome != "completed" and self.authenticated_landmark_verified:
            raise ValueError(
                "non-completed authentication handoff cannot mark authenticated_landmark_verified=True"
            )
        return self


assert not (
    _FORBIDDEN_CREDENTIAL_FIELDS & set(BrowserAuthHandoffMarker.model_fields)
), "BrowserAuthHandoffMarker violated hard credential-free privacy invariant"


@dataclass(frozen=True)
class _OpenBrowserHandoff:
    """Internal state for an in-progress protected authentication interval."""

    interval_id: str
    methods: tuple[BrowserAuthMethod, ...]
    requires_user_presence: bool
    saved_account_selected: bool
    started_at_utc: str
    pre_handoff_url_path: str


class WarmSessionLease:
    """Manages an authenticated session lease over a ``FormSurfaceBackend``."""

    def __init__(
        self,
        surface: FormSurfaceBackend,
        entry_url: str,
        *,
        clock_fn: Optional[Callable[[], datetime]] = None,
        handoff_ledger_path: Optional[Path] = None,
    ) -> None:
        if not entry_url or not entry_url.strip():
            raise ValueError("entry_url must be non-empty")
        self._surface = surface
        self._entry_url = entry_url.strip()
        self._clock_fn = clock_fn
        self._handoff_ledger_path = handoff_ledger_path
        self._state: LeaseState = LeaseState.UNINITIALIZED
        self._active_handoff: Optional[_OpenBrowserHandoff] = None
        self._handoff_ledger: list[BrowserAuthHandoffMarker] = []
        self._persisted_snapshots: list[FormSnapshot] = []
        self._last_url_path: Optional[str] = None

    @property
    def surface(self) -> FormSurfaceBackend:
        return self._surface

    @property
    def entry_url(self) -> str:
        return self._entry_url

    @property
    def state(self) -> LeaseState:
        return self._state

    @property
    def active_handoff_id(self) -> Optional[str]:
        return self._active_handoff.interval_id if self._active_handoff else None

    @property
    def snapshot_persistence_suppressed(self) -> bool:
        """True while an attended authentication handoff interval is open."""
        return self._state == LeaseState.AUTH_HANDOFF_OPEN

    @property
    def handoff_ledger(self) -> tuple[BrowserAuthHandoffMarker, ...]:
        return tuple(self._handoff_ledger)

    @property
    def persisted_snapshots(self) -> tuple[FormSnapshot, ...]:
        return tuple(self._persisted_snapshots)

    def persist_snapshot(self, snapshot: FormSnapshot) -> bool:
        """Persist ``snapshot`` only when outside a protected auth handoff."""
        if self.snapshot_persistence_suppressed or snapshot.auth_wall_present:
            return False
        self._persisted_snapshots.append(snapshot)
        return True

    def _is_snapshot_authenticated(self, snapshot: FormSnapshot) -> bool:
        if snapshot.auth_wall_present:
            return False
        if (
            self._surface.ui_profile.authenticated_landmark_selectors
            and not snapshot.authenticated_landmark_present
        ):
            return False
        return True

    def preflight_auth(self) -> LeaseState:
        """Verify surface authentication state prior to running a job.

        Navigates to ``entry_url`` when uninitialized or on a blank surface,
        otherwise snapshots the current surface. Fail-closed to
        ``LeaseState.AUTH_REQUIRED`` whenever an auth wall is detected or
        configured authenticated landmarks are absent.
        """
        if self._state in {
            LeaseState.FAULTED,
            LeaseState.AUTH_HANDOFF_OPEN,
            LeaseState.BUSY_RUNNING_JOB,
        }:
            return self._state

        snap = self._surface.snapshot()
        raw_url = snap.url_origin_and_path.strip()
        needs_initial_goto = not raw_url or raw_url in {"about:blank", "about:srcdoc"}
        if not needs_initial_goto and self._state == LeaseState.UNINITIALIZED:
            target_clean = _sanitize_url_origin_and_path(self._entry_url)
            current_clean = _sanitize_url_origin_and_path(raw_url)
            if (
                not snap.auth_wall_present
                and not snap.authenticated_landmark_present
                and current_clean != target_clean
            ):
                needs_initial_goto = True

        if needs_initial_goto:
            self._surface.goto(self._entry_url)
            snap = self._surface.snapshot()

        self._last_url_path = _sanitize_url_origin_and_path(
            snap.url_origin_and_path or self._entry_url
        )
        if self._is_snapshot_authenticated(snap):
            self._state = LeaseState.READY
            self.persist_snapshot(snap)
        else:
            self._state = LeaseState.AUTH_REQUIRED
        return self._state

    def begin_auth_handoff(
        self,
        methods: Union[Sequence[BrowserAuthMethod], BrowserAuthMethod] = (
            "password_manager",
            "mfa",
        ),
        requires_user_presence: bool = True,
        saved_account_selected: bool = False,
    ) -> str:
        """Open a source-suppressed interval for attended login/MFA completion."""
        if (
            self._state == LeaseState.AUTH_HANDOFF_OPEN
            or self._active_handoff is not None
        ):
            raise AuthHandoffError("Nested authentication handoffs are refused")
        if self._state == LeaseState.BUSY_RUNNING_JOB:
            raise AuthHandoffError(
                "Cannot open authentication handoff while a job is running"
            )
        if self._state == LeaseState.FAULTED:
            raise AuthHandoffError(
                "Cannot open authentication handoff on a faulted lease"
            )

        if isinstance(methods, str):
            method_tuple: tuple[BrowserAuthMethod, ...] = (methods,)
        else:
            method_tuple = tuple(methods)
        if not method_tuple:
            raise ValueError("methods must contain at least one authentication method")
        if len(set(method_tuple)) != len(method_tuple):
            raise ValueError("methods must not contain duplicates")
        allowed_methods = {
            "password_manager",
            "passkey",
            "sso",
            "mfa",
            "device_unlock",
            "other",
        }
        invalid_methods = [m for m in method_tuple if m not in allowed_methods]
        if invalid_methods:
            raise ValueError(f"Unsupported authentication method(s): {invalid_methods}")

        if self._last_url_path is not None:
            pre_url = self._last_url_path
        else:
            snap = self._surface.snapshot()
            pre_url = _sanitize_url_origin_and_path(
                snap.url_origin_and_path or self._entry_url
            )
            self._last_url_path = pre_url

        interval_id = str(uuid.uuid4())
        self._active_handoff = _OpenBrowserHandoff(
            interval_id=interval_id,
            methods=method_tuple,
            requires_user_presence=requires_user_presence,
            saved_account_selected=saved_account_selected,
            started_at_utc=_utc_now_iso(self._clock_fn),
            pre_handoff_url_path=pre_url,
        )
        self._state = LeaseState.AUTH_HANDOFF_OPEN
        return interval_id

    def end_auth_handoff(
        self,
        interval_id: str,
        outcome: BrowserAuthOutcome = "completed",
    ) -> BrowserAuthHandoffMarker:
        """Close the active handoff and fail-closed verify post-login landmarks."""
        if self._state != LeaseState.AUTH_HANDOFF_OPEN or self._active_handoff is None:
            raise AuthHandoffError("No authentication handoff is currently open")
        if interval_id != self._active_handoff.interval_id:
            raise AuthHandoffError(
                f"Mismatched handoff interval_id {interval_id!r}; "
                f"expected {self._active_handoff.interval_id!r}"
            )
        if outcome not in {"completed", "cancelled", "failed", "aborted"}:
            raise ValueError(f"Unsupported handoff outcome: {outcome!r}")

        open_handoff = self._active_handoff
        ended_at_utc = _utc_now_iso(self._clock_fn)
        post_url: Optional[str] = None
        verified = False
        resume_snapshot: Optional[FormSnapshot] = None

        if outcome == "completed":
            resume_snapshot = self._surface.snapshot()
            post_url = _sanitize_url_origin_and_path(
                resume_snapshot.url_origin_and_path
            )
            self._last_url_path = post_url
            verified = self._is_snapshot_authenticated(resume_snapshot)
            next_state = LeaseState.READY if verified else LeaseState.AUTH_REQUIRED
        else:
            next_state = LeaseState.AUTH_REQUIRED

        marker = BrowserAuthHandoffMarker(
            interval_id=open_handoff.interval_id,
            methods=open_handoff.methods,
            requires_user_presence=open_handoff.requires_user_presence,
            saved_account_selected=open_handoff.saved_account_selected,
            started_at_utc=open_handoff.started_at_utc,
            ended_at_utc=ended_at_utc,
            outcome=outcome,
            suppressed_sources=DEFAULT_BROWSER_SUPPRESSED_SOURCES,
            pre_handoff_url_path=open_handoff.pre_handoff_url_path,
            post_handoff_url_path=post_url,
            authenticated_landmark_verified=verified,
        )

        self._active_handoff = None
        self._state = next_state
        self._handoff_ledger.append(marker)
        if verified and resume_snapshot is not None:
            self.persist_snapshot(resume_snapshot)
        if self._handoff_ledger_path is not None:
            payload = [m.model_dump(mode="json") for m in self._handoff_ledger]
            _atomic_write_bytes(
                self._handoff_ledger_path, canonical_json_bytes(payload) + b"\n"
            )
        return marker

    def heartbeat(self) -> bool:
        """Execute a zero-mutation liveness/session check when ``state == READY``.

        Raises ``SessionLeaseStateError`` if called outside ``LeaseState.READY``
        (never runs during ``BUSY_RUNNING_JOB`` or ``AUTH_HANDOFF_OPEN``).
        Raises ``HeartbeatMutationError`` and transitions to ``FAULTED`` if
        ``surface.mutation_count`` increases or keepalive returns an unexpected
        HTTP status code.
        """
        if self._state != LeaseState.READY:
            raise SessionLeaseStateError(
                f"heartbeat() is allowed only in state READY; current state is {self._state.value!r}"
            )

        before = self._surface.mutation_count
        keepalive_path = self._surface.ui_profile.keepalive_path
        if keepalive_path:
            status_code = self._surface.ping_keepalive(keepalive_path)
            after = self._surface.mutation_count
            if after != before:
                self._state = LeaseState.FAULTED
                raise HeartbeatMutationError(
                    f"Keepalive check mutated surface state: mutation_count changed from {before} to {after}"
                )
            if status_code in (401, 403):
                self._state = LeaseState.AUTH_REQUIRED
                return False
            if not (200 <= status_code < 300):
                self._state = LeaseState.FAULTED
                raise HeartbeatMutationError(
                    f"Keepalive check returned unexpected HTTP status {status_code}"
                )
            return True

        snap = self._surface.snapshot()
        after = self._surface.mutation_count
        if after != before:
            self._state = LeaseState.FAULTED
            raise HeartbeatMutationError(
                f"Landmark heartbeat mutated surface state: mutation_count changed from {before} to {after}"
            )
        self._last_url_path = _sanitize_url_origin_and_path(
            snap.url_origin_and_path or self._entry_url
        )
        if not self._is_snapshot_authenticated(snap):
            self._state = LeaseState.AUTH_REQUIRED
            return False
        return True

    @contextmanager
    def acquire_job_execution(self) -> Iterator[FormSurfaceBackend]:
        """Transition ``READY -> BUSY_RUNNING_JOB`` for exclusive job execution."""
        if self._state != LeaseState.READY:
            raise SessionLeaseStateError(
                f"Cannot acquire job execution when lease state is {self._state.value!r}"
            )
        self._state = LeaseState.BUSY_RUNNING_JOB
        try:
            yield self._surface
        except BaseException:
            if self._state == LeaseState.BUSY_RUNNING_JOB:
                self._state = LeaseState.READY
            raise
        else:
            if self._state == LeaseState.BUSY_RUNNING_JOB:
                self._state = LeaseState.READY

    def mark_auth_required(self) -> None:
        """Transition lease to ``AUTH_REQUIRED`` after mid-job auth wall detection."""
        if self._state not in {LeaseState.FAULTED, LeaseState.AUTH_HANDOFF_OPEN}:
            self._state = LeaseState.AUTH_REQUIRED


class QueueJobSpec(BaseModel):
    """Idempotent form transaction job specification submitted to a queue worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    pack_id: str = Field(min_length=1)
    dossier: InputDossier


class _IdempotencyLedgerRecord(BaseModel):
    """Value-scrubbed persisted idempotency record keyed by ``request_id``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    job_id: str
    pack_id: str
    pack_digest: str
    dossier_digest: str
    recorded_at_utc: str
    result: FormTxResult


ExecutorFn = Callable[[FormSurfaceBackend, FormPack, InputDossier], FormTxResult]


class FormTxQueueRunner:
    """Idempotent queue worker executing ``QueueJobSpec`` items over a ``WarmSessionLease``."""

    def __init__(
        self,
        lease: WarmSessionLease,
        executor_fn: ExecutorFn,
        *,
        packs: Optional[Mapping[str, FormPack]] = None,
        ledger_dir: Optional[Path] = None,
        clock_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._lease = lease
        self._executor_fn = executor_fn
        self._packs: dict[str, FormPack] = dict(packs) if packs is not None else {}
        self._ledger_dir = ledger_dir
        self._clock_fn = clock_fn
        self._records: dict[str, _IdempotencyLedgerRecord] = {}
        if self._ledger_dir is not None:
            self._ledger_dir.mkdir(parents=True, exist_ok=True)
            self._load_existing_ledger()

    @property
    def lease(self) -> WarmSessionLease:
        return self._lease

    def register_pack(self, pack: FormPack) -> None:
        """Register or update a ``FormPack`` by its ``pack_id``."""
        self._packs[pack.pack_id] = pack

    def get_cached_result(self, request_id: str) -> Optional[FormTxResult]:
        """Return any previously recorded ``FormTxResult`` for ``request_id``."""
        record = self._lookup_record(request_id)
        return record.result if record is not None else None

    def _record_path(self, request_id: str) -> Path:
        assert self._ledger_dir is not None
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return self._ledger_dir / f"{digest}.json"

    def _load_existing_ledger(self) -> None:
        assert self._ledger_dir is not None
        for entry_path in sorted(self._ledger_dir.glob("*.json")):
            raw = json.loads(entry_path.read_text(encoding="utf-8"))
            record = _IdempotencyLedgerRecord.model_validate(raw)
            self._records[record.request_id] = record

    def _lookup_record(self, request_id: str) -> Optional[_IdempotencyLedgerRecord]:
        if request_id in self._records:
            return self._records[request_id]
        if self._ledger_dir is not None:
            path = self._record_path(request_id)
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                record = _IdempotencyLedgerRecord.model_validate(raw)
                self._records[request_id] = record
                return record
        return None

    def _store_record(self, record: _IdempotencyLedgerRecord) -> None:
        self._records[record.request_id] = record
        if self._ledger_dir is not None:
            path = self._record_path(record.request_id)
            _atomic_write_bytes(
                path, canonical_json_bytes(record.model_dump(mode="json")) + b"\n"
            )

    def _resolve_pack(
        self, pack_id: str, explicit_pack: Optional[FormPack]
    ) -> FormPack:
        if explicit_pack is not None:
            if explicit_pack.pack_id != pack_id:
                raise ValueError(
                    f"Pack ID mismatch: job specifies {pack_id!r} "
                    f"but explicit pack has {explicit_pack.pack_id!r}"
                )
            return explicit_pack
        if pack_id not in self._packs:
            raise KeyError(
                f"Unknown FormPack {pack_id!r}; register it before running job"
            )
        return self._packs[pack_id]

    def run_job(
        self,
        job: QueueJobSpec,
        *,
        pack: Optional[FormPack] = None,
        retry_safe_failures: bool = False,
    ) -> FormTxResult:
        """Execute ``job`` idempotently over the warm session lease."""
        target_pack = self._resolve_pack(job.pack_id, pack)
        pack_digest = target_pack.digest()
        dossier_digest = job.dossier.digest()

        existing = self._lookup_record(job.request_id)
        if existing is not None:
            if (
                existing.dossier_digest != dossier_digest
                or existing.pack_digest != pack_digest
            ):
                raise RequestIdConflictError(
                    f"Conflicting payload for request_id={job.request_id!r}: "
                    f"stored (dossier={existing.dossier_digest[:12]}, pack={existing.pack_digest[:12]}) "
                    f"!= incoming (dossier={dossier_digest[:12]}, pack={pack_digest[:12]})"
                )
            if not (existing.result.safe_to_retry and retry_safe_failures):
                return existing.result

        lease_state = self._lease.preflight_auth()
        if lease_state != LeaseState.READY:
            held_result = FormTxResult(
                run_id=f"run-held-{uuid.uuid4().hex[:12]}",
                request_id=job.request_id,
                outcome=FormTxOutcome.AUTH_REQUIRED,
                safe_to_retry=True,
                halts=(
                    FormHaltItem(
                        halt_id=f"halt-auth-{job.job_id}",
                        kind=FormHaltKind.AUTH_WALL_DETECTED,
                        message=(
                            f"Session lease state is {lease_state.value!r}; "
                            "holding job before any field actuation."
                        ),
                    ),
                ),
                what_happened=(
                    f"Job {job.job_id!r} held without actuation because lease state "
                    f"is {lease_state.value!r}."
                ),
            )
            self._store_record(
                _IdempotencyLedgerRecord(
                    request_id=job.request_id,
                    job_id=job.job_id,
                    pack_id=target_pack.pack_id,
                    pack_digest=pack_digest,
                    dossier_digest=dossier_digest,
                    recorded_at_utc=_utc_now_iso(self._clock_fn),
                    result=held_result,
                )
            )
            return held_result

        with self._lease.acquire_job_execution() as surface:
            result = self._executor_fn(surface, target_pack, job.dossier)

        if result.outcome == FormTxOutcome.AUTH_REQUIRED:
            self._lease.mark_auth_required()

        self._store_record(
            _IdempotencyLedgerRecord(
                request_id=job.request_id,
                job_id=job.job_id,
                pack_id=target_pack.pack_id,
                pack_digest=pack_digest,
                dossier_digest=dossier_digest,
                recorded_at_utc=_utc_now_iso(self._clock_fn),
                result=result,
            )
        )
        return result

    def run_batch(
        self,
        jobs: Sequence[QueueJobSpec],
        *,
        retry_safe_failures: bool = False,
    ) -> tuple[FormTxResult, ...]:
        """Run a batch of queue jobs sequentially over the shared session lease."""
        results: list[FormTxResult] = []
        for job in jobs:
            results.append(self.run_job(job, retry_safe_failures=retry_safe_failures))
        return tuple(results)
