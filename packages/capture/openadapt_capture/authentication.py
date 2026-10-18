"""Source-time protection for attended authentication handoffs.

Capture owns the observation boundary. It does not own credentials or decide
whether an application is authenticated. This module suppresses sensitive
sources during an attended handoff and retains a small, sealed timeline marker.
"""

from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

AUTHENTICATION_HANDOFF_FILENAME = "authentication-handoffs.json"
AUTHENTICATION_HANDOFF_SCHEMA_VERSION = "openadapt.capture.authentication-handoffs/v1"
AUTHENTICATION_METHODS = frozenset(
    {
        "password_manager",
        "passkey",
        "sso",
        "mfa",
        "device_unlock",
        "other",
    }
)
AUTHENTICATION_OUTCOMES = frozenset({"completed", "cancelled", "failed", "aborted"})
SUPPRESSED_SOURCES = (
    "audio",
    "browser",
    "input",
    "screen",
    "structural",
    "window",
)

AuthenticationMethod = Literal[
    "password_manager",
    "passkey",
    "sso",
    "mfa",
    "device_unlock",
    "other",
]
AuthenticationOutcome = Literal["completed", "cancelled", "failed", "aborted"]


class AuthenticationHandoffError(RuntimeError):
    """An authentication handoff could not preserve its privacy boundary."""


class AuthenticationBoundaryError(AuthenticationHandoffError):
    """The recorder must fail because a source boundary became ambiguous."""


class FreshFrameProof(BaseModel):
    """Proof that Capture retained a new source frame before input resumed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: float = Field(ge=0)
    source_ordinal: int = Field(ge=1)
    frame_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capture_source: str = Field(min_length=1, max_length=64)
    window_geometry_generation: int | None = Field(default=None, ge=1)


class AuthenticationHandoff(BaseModel):
    """One privacy-bounded authentication interval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interval_id: str = Field(min_length=36, max_length=36)
    kind: Literal["authentication"] = "authentication"
    methods: tuple[AuthenticationMethod, ...] = Field(min_length=1, max_length=6)
    requires_user_presence: bool
    saved_account_selected: bool
    started_at: float = Field(ge=0)
    entry_frame: FreshFrameProof | None = None
    ended_at: float | None = Field(default=None, ge=0)
    outcome: AuthenticationOutcome | None = None
    suppressed_sources: tuple[
        Literal["audio", "browser", "input", "screen", "structural", "window"],
        ...,
    ]
    resume_frame: FreshFrameProof | None = None

    @model_validator(mode="after")
    def _closed_contract(self) -> "AuthenticationHandoff":
        try:
            uuid.UUID(self.interval_id)
        except ValueError as exc:
            raise ValueError("interval_id must be a UUID") from exc
        if len(set(self.methods)) != len(self.methods):
            raise ValueError("authentication methods must be unique")
        if tuple(sorted(self.suppressed_sources)) != SUPPRESSED_SOURCES:
            raise ValueError("the authentication handoff must suppress every sensitive source")
        closed = self.ended_at is not None or self.outcome is not None
        if closed and (self.ended_at is None or self.outcome is None):
            raise ValueError("a closed authentication handoff needs an end time and outcome")
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("authentication handoff end time precedes its start time")
        if self.entry_frame is not None and self.entry_frame.timestamp > self.started_at:
            raise ValueError("authentication entry frame follows the protected start")
        if self.outcome == "aborted":
            if self.resume_frame is not None:
                raise ValueError("an aborted authentication handoff cannot claim a resume frame")
        elif self.outcome is not None and self.resume_frame is None:
            raise ValueError("a closed authentication handoff needs a fresh resume frame")
        if self.outcome is None and self.resume_frame is not None:
            raise ValueError("an open authentication handoff cannot have a resume frame")
        return self


class AuthenticationHandoffManifest(BaseModel):
    """Canonical sealed list of authentication handoffs in one capture."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.capture.authentication-handoffs/v1"]
    intervals: tuple[AuthenticationHandoff, ...]

    @model_validator(mode="after")
    def _ordered_nonoverlapping_intervals(self) -> "AuthenticationHandoffManifest":
        ids = [interval.interval_id for interval in self.intervals]
        if len(ids) != len(set(ids)):
            raise ValueError("authentication handoff IDs must be unique")
        previous_end: float | None = None
        for interval in self.intervals:
            if previous_end is not None and interval.started_at < previous_end:
                raise ValueError("authentication handoffs cannot overlap")
            previous_end = interval.ended_at
            if previous_end is None and interval is not self.intervals[-1]:
                raise ValueError("only the last authentication handoff can be open")
        return self


@dataclass(frozen=True)
class AuthenticationHandoffHandle:
    """Opaque owner handle for one active handoff."""

    interval_id: str


class _RetentionLease:
    """One in-flight sensitive-source operation that begin() must drain."""

    def __init__(
        self,
        controller: "AuthenticationHandoffController",
        *,
        entry: bool = False,
        resume: bool = False,
    ) -> None:
        self._controller = controller
        self.entry = entry
        self.resume = resume
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._controller._release_retention()

    def __enter__(self) -> "_RetentionLease":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _write_manifest(path: Path, manifest: AuthenticationHandoffManifest) -> None:
    """Replace the owner-only marker file with canonical bytes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _canonical_json_bytes(manifest.model_dump(mode="json"))
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def load_authentication_handoffs(
    capture_dir: str | Path,
    *,
    required: bool = False,
) -> AuthenticationHandoffManifest:
    """Load and validate the canonical authentication marker sidecar."""

    path = Path(capture_dir) / AUTHENTICATION_HANDOFF_FILENAME
    if not path.exists():
        if required:
            raise AuthenticationHandoffError("authentication handoff marker is missing")
        return AuthenticationHandoffManifest(
            schema_version=AUTHENTICATION_HANDOFF_SCHEMA_VERSION,
            intervals=(),
        )
    if not path.is_file() or path.is_symlink():
        raise AuthenticationHandoffError("authentication handoff marker is not a regular file")
    raw = path.read_bytes()
    try:
        manifest = AuthenticationHandoffManifest.model_validate_json(raw)
    except Exception as exc:
        raise AuthenticationHandoffError("authentication handoff marker is malformed") from exc
    if raw != _canonical_json_bytes(manifest.model_dump(mode="json")):
        raise AuthenticationHandoffError("authentication handoff marker bytes are not canonical")
    return manifest


def frame_sha256(image: object) -> str:
    """Hash exact source pixels without retaining another image artifact."""

    mode = str(getattr(image, "mode"))
    width, height = getattr(image, "size")
    pixels = getattr(image, "tobytes")()
    header = _canonical_json_bytes({"height": height, "mode": mode, "width": width})
    return hashlib.sha256(b"openadapt.capture.frame.v1\0" + header + pixels).hexdigest()


class AuthenticationHandoffController:
    """Coordinate atomic source suppression and fresh-frame resumption."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._phase: Literal["unbound", "normal", "entering", "protected", "resuming"] = "unbound"
        self._active_retentions = 0
        self._entry_capture_in_flight = False
        self._entry_capture_complete = False
        self._entry_frame_proof: FreshFrameProof | None = None
        self._entry_error: BaseException | None = None
        self._entry_frame_required = False
        self._resume_capture_in_flight = False
        self._path: Path | None = None
        self._timestamp: Callable[[], float] | None = None
        self._intervals: list[AuthenticationHandoff] = []
        self._active_interval_id: str | None = None
        self._pending_outcome: AuthenticationOutcome | None = None
        self._resume_error: BaseException | None = None
        self._closed = False
        # The audio process cannot share the thread condition. It can share this
        # event and replaces protected chunks with generated silence.
        self.audio_suppressed = multiprocessing.Event()
        self.audio_suppression_ack = multiprocessing.Event()
        self.audio_suppression_ack.set()
        self._audio_enabled = False

    def configure_audio(self, enabled: bool) -> None:
        """Declare whether begin() must wait for a microphone-process cut."""

        with self._condition:
            if self._phase != "unbound":
                raise AuthenticationHandoffError(
                    "audio protection must be configured before recording starts"
                )
            self._audio_enabled = bool(enabled)
            if enabled:
                self.audio_suppression_ack.clear()
            else:
                self.audio_suppression_ack.set()

    def configure_entry_frame(self, required: bool) -> None:
        """Require a clean screen cut before a protected interval starts."""

        with self._condition:
            if self._phase != "unbound":
                raise AuthenticationHandoffError(
                    "entry-frame protection must be configured before recording starts"
                )
            self._entry_frame_required = bool(required)

    @property
    def protected(self) -> bool:
        """Return whether normal source retention is suppressed."""

        with self._condition:
            return self._phase in {"entering", "protected", "resuming"}

    def bind(self, capture_dir: str | Path, timestamp: Callable[[], float]) -> None:
        """Bind the controller to a live capture and create its empty sidecar."""

        with self._condition:
            if self._phase != "unbound":
                raise AuthenticationHandoffError("authentication controller is already bound")
            self._path = Path(capture_dir) / AUTHENTICATION_HANDOFF_FILENAME
            self._timestamp = timestamp
            self._phase = "normal"
            self._persist_locked()
            self._condition.notify_all()

    def begin_retention(self) -> _RetentionLease | None:
        """Enter one normal sensitive-source operation, or suppress it."""

        with self._condition:
            if self._phase != "normal" or self._closed:
                return None
            self._active_retentions += 1
            return _RetentionLease(self)

    def begin_screen_retention(self) -> _RetentionLease | None:
        """Enter a normal frame operation or claim the one resume frame."""

        with self._condition:
            if self._closed or self._phase in {"unbound", "protected"}:
                return None
            if self._phase == "entering":
                if not self._entry_frame_required:
                    return None
                if self._entry_capture_in_flight or self._entry_capture_complete:
                    return None
                self._entry_capture_in_flight = True
                self._active_retentions += 1
                return _RetentionLease(self, entry=True)
            if self._phase == "resuming":
                if self._resume_capture_in_flight:
                    return None
                self._resume_capture_in_flight = True
                self._active_retentions += 1
                return _RetentionLease(self, resume=True)
            self._active_retentions += 1
            return _RetentionLease(self)

    def _release_retention(self) -> None:
        with self._condition:
            if self._active_retentions <= 0:
                raise RuntimeError("authentication retention lease underflow")
            self._active_retentions -= 1
            self._condition.notify_all()

    def begin(
        self,
        *,
        methods: AuthenticationMethod | Sequence[AuthenticationMethod],
        requires_user_presence: bool,
        saved_account_selected: bool = False,
        timeout: float = 10.0,
        interval_id: str | None = None,
    ) -> AuthenticationHandoffHandle:
        """Suppress all sensitive sources and persist an open handoff marker."""

        normalized = self._normalize_methods(methods)
        if interval_id is None:
            interval_id = str(uuid.uuid4())
        else:
            try:
                interval_id = str(uuid.UUID(interval_id))
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError("authentication interval ID must be a UUID") from exc
        deadline = time.monotonic() + timeout
        with self._condition:
            if self._closed or self._phase == "unbound":
                raise AuthenticationHandoffError("recorder is not ready for authentication")
            prior = next(
                (interval for interval in self._intervals if interval.interval_id == interval_id),
                None,
            )
            if prior is not None:
                if (
                    prior.methods != normalized
                    or prior.requires_user_presence != requires_user_presence
                    or prior.saved_account_selected != saved_account_selected
                ):
                    raise AuthenticationHandoffError(
                        "authentication interval ID was reused with different parameters"
                    )
                return AuthenticationHandoffHandle(interval_id)
            if self._phase != "normal":
                raise AuthenticationHandoffError("an authentication handoff is already active")
            self._phase = "entering"
            self._entry_capture_in_flight = False
            self._entry_capture_complete = False
            self._entry_frame_proof = None
            self._entry_error = None
            while self._active_retentions or (
                self._entry_frame_required and not self._entry_capture_complete
            ):
                if self._entry_error is not None or self._closed:
                    self._phase = "protected"
                    raise AuthenticationBoundaryError(
                        "authentication entry frame failed"
                    ) from self._entry_error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._phase = "protected"
                    self._condition.notify_all()
                    raise AuthenticationBoundaryError(
                        "sensitive capture sources did not reach the protected boundary"
                    )
                self._condition.wait(min(remaining, 0.01))
            if self._audio_enabled:
                self.audio_suppression_ack.clear()
            self.audio_suppressed.set()
            while not self.audio_suppression_ack.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._phase = "protected"
                    self._condition.notify_all()
                    raise AuthenticationBoundaryError("audio did not reach the protected boundary")
                self._condition.wait(min(remaining, 0.01))
            started_at = self._now_locked()
            interval = AuthenticationHandoff(
                interval_id=interval_id,
                methods=normalized,
                requires_user_presence=requires_user_presence,
                saved_account_selected=saved_account_selected,
                started_at=started_at,
                entry_frame=self._entry_frame_proof,
                suppressed_sources=SUPPRESSED_SOURCES,
            )
            self._intervals.append(interval)
            self._active_interval_id = interval_id
            self._entry_capture_in_flight = False
            self._entry_capture_complete = False
            self._entry_frame_proof = None
            self._entry_error = None
            self._phase = "protected"
            try:
                self._persist_locked()
            except BaseException as exc:
                self._intervals.pop()
                self._active_interval_id = None
                self._phase = "protected"
                self._condition.notify_all()
                raise AuthenticationBoundaryError(
                    "authentication start marker could not be persisted"
                ) from exc
            self._condition.notify_all()
            return AuthenticationHandoffHandle(interval_id)

    def end(
        self,
        handle: AuthenticationHandoffHandle,
        *,
        outcome: Literal["completed", "cancelled", "failed"] = "completed",
        timeout: float = 10.0,
    ) -> AuthenticationHandoff:
        """Request a fresh frame, then reopen normal source retention."""

        if outcome not in {"completed", "cancelled", "failed"}:
            raise ValueError("authentication outcome must be completed, cancelled, or failed")
        deadline = time.monotonic() + timeout
        with self._condition:
            prior = next(
                (
                    interval
                    for interval in self._intervals
                    if interval.interval_id == handle.interval_id
                ),
                None,
            )
            if prior is not None and prior.outcome is not None:
                if prior.outcome != outcome:
                    raise AuthenticationHandoffError("authentication handoff outcome changed")
                return prior
            if handle.interval_id != self._active_interval_id:
                raise AuthenticationHandoffError("authentication handoff handle is not active")
            if self._phase == "protected":
                self._pending_outcome = outcome
                self._resume_error = None
                self._phase = "resuming"
                self._condition.notify_all()
            elif self._phase != "resuming":
                raise AuthenticationHandoffError("authentication handoff is not protected")
            elif self._pending_outcome != outcome:
                raise AuthenticationHandoffError("authentication handoff outcome changed")
            while self._active_interval_id == handle.interval_id:
                if self._resume_error is not None:
                    raise AuthenticationHandoffError(
                        "fresh-frame capture failed; the handoff remains protected"
                    ) from self._resume_error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AuthenticationHandoffError(
                        "fresh-frame capture timed out; the handoff remains protected"
                    )
                self._condition.wait(remaining)
            closed = self._intervals[-1]
            if closed.outcome != outcome:
                raise AuthenticationHandoffError(
                    "recorder stopped before the authentication handoff resumed"
                )
            return closed

    def complete_resume_frame(
        self,
        lease: _RetentionLease,
        *,
        timestamp: float,
        source_ordinal: int,
        frame_sha256: str,
        capture_source: str,
        window_geometry_generation: int | None,
    ) -> None:
        """Persist the exact resume proof before normal input is reopened."""

        if not lease.resume:
            raise AuthenticationHandoffError("normal frame cannot complete a handoff")
        with self._condition:
            if self._phase != "resuming" or self._active_interval_id is None:
                raise AuthenticationHandoffError("no authentication handoff awaits a frame")
            proof = FreshFrameProof(
                timestamp=timestamp,
                source_ordinal=source_ordinal,
                frame_sha256=frame_sha256,
                capture_source=capture_source,
                window_geometry_generation=window_geometry_generation,
            )
            active = self._intervals[-1]
            closed = active.model_copy(
                update={
                    "ended_at": self._now_locked(),
                    "outcome": self._pending_outcome,
                    "resume_frame": proof,
                }
            )
            # Revalidate model_copy updates. Pydantic does not validate them.
            closed = AuthenticationHandoff.model_validate(closed.model_dump(mode="json"))
            self._intervals[-1] = closed
            try:
                self._persist_locked()
            except BaseException as exc:
                self._intervals[-1] = active
                self._resume_error = exc
                self._resume_capture_in_flight = False
                self._condition.notify_all()
                raise
            self._active_interval_id = None
            self._pending_outcome = None
            self._resume_capture_in_flight = False
            self._phase = "normal"
            self.audio_suppressed.clear()
            if self._audio_enabled:
                self.audio_suppression_ack.clear()
            self._condition.notify_all()

    def complete_entry_frame(
        self,
        lease: _RetentionLease,
        *,
        timestamp: float,
        source_ordinal: int,
        frame_sha256: str,
        capture_source: str,
        window_geometry_generation: int | None,
    ) -> None:
        """Acknowledge the clean frame that closes pre-handoff actions."""

        if not lease.entry:
            raise AuthenticationHandoffError("normal frame cannot open a handoff")
        with self._condition:
            if self._phase != "entering":
                raise AuthenticationHandoffError("no authentication handoff awaits entry")
            self._entry_frame_proof = FreshFrameProof(
                timestamp=timestamp,
                source_ordinal=source_ordinal,
                frame_sha256=frame_sha256,
                capture_source=capture_source,
                window_geometry_generation=window_geometry_generation,
            )
            self._entry_capture_complete = True
            self._condition.notify_all()

    def fail_boundary_frame(self, lease: _RetentionLease, error: BaseException) -> None:
        """Fail an entry cut or retain resume protection after frame failure."""

        if lease.entry:
            with self._condition:
                self._entry_error = error
                self._entry_capture_in_flight = False
                self._condition.notify_all()
            return
        self.fail_resume_frame(lease, error)

    def fail_resume_frame(self, lease: _RetentionLease, error: BaseException) -> None:
        """Keep the boundary closed after a failed fresh-frame attempt."""

        if not lease.resume:
            return
        with self._condition:
            self._resume_error = error
            self._resume_capture_in_flight = False
            self._condition.notify_all()

    def abort_active(self) -> AuthenticationHandoff | None:
        """Close an unfinished handoff for recorder shutdown without a resume claim."""

        with self._condition:
            self._closed = True
            if self._active_interval_id is None:
                self._condition.notify_all()
                if self._phase in {"entering", "protected", "resuming"}:
                    raise AuthenticationBoundaryError(
                        "recording stopped inside an unmarked authentication boundary"
                    )
                return None
            active = self._intervals[-1]
            aborted = active.model_copy(
                update={
                    "ended_at": self._now_locked(),
                    "outcome": "aborted",
                    "resume_frame": None,
                }
            )
            aborted = AuthenticationHandoff.model_validate(aborted.model_dump(mode="json"))
            self._intervals[-1] = aborted
            self._persist_locked()
            self._active_interval_id = None
            self._pending_outcome = None
            self._entry_capture_in_flight = False
            self._entry_capture_complete = False
            self._entry_frame_proof = None
            self._resume_capture_in_flight = False
            self._condition.notify_all()
            return aborted

    def close(self) -> None:
        """Prevent later handoffs after a recording stops."""

        with self._condition:
            self._closed = True
            self._condition.notify_all()

    @staticmethod
    def _normalize_methods(
        methods: AuthenticationMethod | Sequence[AuthenticationMethod],
    ) -> tuple[AuthenticationMethod, ...]:
        if isinstance(methods, str):
            values = (methods,)
        else:
            values = tuple(methods)
        if not values or len(values) > 6:
            raise ValueError("authentication methods must contain one to six values")
        if any(not isinstance(value, str) for value in values):
            raise ValueError("authentication methods are invalid or duplicated")
        if len(values) != len(set(values)) or any(
            value not in AUTHENTICATION_METHODS for value in values
        ):
            raise ValueError("authentication methods are invalid or duplicated")
        return values  # type: ignore[return-value]

    def _now_locked(self) -> float:
        if self._timestamp is None:
            raise AuthenticationHandoffError("authentication controller has no capture clock")
        timestamp = float(self._timestamp())
        if not math.isfinite(timestamp) or timestamp < 0:
            raise AuthenticationHandoffError("authentication timestamp is invalid")
        return timestamp

    def _persist_locked(self) -> None:
        if self._path is None:
            raise AuthenticationHandoffError("authentication controller is not bound")
        manifest = AuthenticationHandoffManifest(
            schema_version=AUTHENTICATION_HANDOFF_SCHEMA_VERSION,
            intervals=tuple(self._intervals),
        )
        _write_manifest(self._path, manifest)
