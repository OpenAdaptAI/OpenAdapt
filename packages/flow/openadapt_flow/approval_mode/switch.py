"""The switch record, its signed Cloud status, and the runner's switch log.

Three objects decide whether "Only ask a person about problems" applies on a
runner:

- :class:`SwitchRecord`, the runner copy of the clinic's recorded choice. It
  binds exact digests (bundle, environment, runner, verifier identities and
  the dedicated user) and the thresholds in force.
- :class:`SwitchStatus`, an Ed25519-signed answer from Cloud. It binds the
  runner's nonce and the sequence number and hash of the runner's latest switch
  log event, so Cloud can't report "on" without holding the runner's events,
  and an old status can't be replayed.
- :class:`SwitchLog`, a local append-only, hash-chained log the runner reads
  before every save. Any ``switched_off`` event in it means off, whatever Cloud
  says, so a local switch-off that hasn't reached Cloud can't be undone by
  Cloud. An unreadable or broken log also means off.

Nothing here holds an input value or screen content.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from openadapt_flow.approval_mode.model import (
    CODE_PATTERN,
    DESKTOP_LIST_WITH_HEADER,
    HEX_SHA256_PATTERN,
    OPAQUE_ID_PATTERN,
    PREFIXED_SHA256_PATTERN,
    ItemDecisionKind,
    OffReason,
    SwitchThresholds,
)

SWITCH_RECORD_SCHEMA: Literal["openadapt.approval-mode-switch/v1"] = (
    "openadapt.approval-mode-switch/v1"
)
STATUS_SCHEMA: Literal["openadapt.approval-mode-status/v1"] = (
    "openadapt.approval-mode-status/v1"
)
LOG_EVENT_SCHEMA: Literal["openadapt.approval-mode-log-event/v1"] = (
    "openadapt.approval-mode-log-event/v1"
)

_STATUS_DOMAIN = b"openadapt.approval-mode-status/v1\n"
_RECORD_DOMAIN = b"openadapt.approval-mode-switch/v1\n"
_LOG_DOMAIN = b"openadapt.approval-mode-log/v1\n"
_REARM_DOMAIN = b"openadapt.approval-mode-rearm/v1\n"
#: Consecutive verified items since the switch went off before a person may
#: re-arm it. Never automatic.
REARM_CLEAN_ITEMS = 10
#: Item decisions that count as a verified item toward a re-arm.
_CLEAN_DECISIONS = frozenset({"no_person", "approved"})
GENESIS_SHA256 = "sha256:" + "0" * 64

#: Resolution rungs from strongest to weakest (``ir.Rung``).
RUNG_ORDER: tuple[str, ...] = (
    "structural",
    "template",
    "template_global",
    "ocr",
    "geometry",
    "grounder",
)


def canonical_json(value: Any) -> bytes:
    """Sorted-key, compact UTF-8 JSON used for every digest and signature."""

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SwitchError(ValueError):
    """A switch record, status or log failed validation."""


class SwitchLogError(SwitchError):
    """The switch log is missing, unreadable or its hash chain is broken.

    The gate treats this as "off" for the item: a person approves it.
    """


class StatusRejected(SwitchError):
    """A Cloud status failed its signature or binding checks."""


class SwitchRecord(BaseModel):
    """The runner copy of one switch record (design sections 5 and 10).

    One record covers one workflow version on one runner. When the switch
    turns off, the record closes for good; turning it on again creates a new
    record with a new id and a new log.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.approval-mode-switch/v1"] = SWITCH_RECORD_SCHEMA
    switch_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    org_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    workflow_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    runner_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    #: ``Workflow.manifest.content_digest`` of the sealed bundle.
    bundle_content_digest: str = Field(pattern=HEX_SHA256_PATTERN)
    #: Environment digest, including the live application version read by the
    #: bound environment observer.
    environment_digest: str = Field(pattern=PREFIXED_SHA256_PATTERN)
    #: Every verifier identity the record check may use. Never empty.
    verifier_identities: tuple[str, ...] = Field(min_length=1, max_length=16)
    #: HMAC of the dedicated OpenAdapt user's login, keyed on the runner.
    dedicated_user_ref: str = Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")
    #: The only channel in phase 1.
    channel: Literal["desktop_run_it_list_with_header"] = DESKTOP_LIST_WITH_HEADER
    #: Phase 1 requires the Regulated profile.
    profile: Literal["regulated"] = "regulated"
    #: Per step id, the weakest resolution rung the step used in passing
    #: cases (readiness cases that ended VERIFIED and approved items).
    rung_floor: dict[str, str] = Field(default_factory=dict)
    #: Per step id, the declared dialog dismissals passing cases used.
    allowed_interstitials: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    thresholds: SwitchThresholds = Field(default_factory=SwitchThresholds)

    @model_validator(mode="after")
    def _closed_values(self) -> "SwitchRecord":
        import re

        prefixed = re.compile(PREFIXED_SHA256_PATTERN)
        if len(set(self.verifier_identities)) != len(self.verifier_identities):
            raise ValueError("verifier identities must be unique")
        for identity in self.verifier_identities:
            if not prefixed.fullmatch(identity):
                raise ValueError("a verifier identity must be an opaque sha256 digest")
        for step_id, rung in self.rung_floor.items():
            if rung not in RUNG_ORDER or rung == "grounder":
                raise ValueError(
                    f"rung floor for step {step_id!r} must be a deterministic rung"
                )
        code = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:-]{0,127}$")
        for names in self.allowed_interstitials.values():
            for name in names:
                if not code.fullmatch(name):
                    raise ValueError("allowed interstitial names must be identifiers")
        return self

    def digest(self) -> str:
        """Domain-separated digest of the exact record."""

        payload = self.model_dump(mode="json")
        return (
            "sha256:"
            + hashlib.sha256(_RECORD_DOMAIN + canonical_json(payload)).hexdigest()
        )


class StatusRequest(BaseModel):
    """What the runner sends to Cloud when it asks for a status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    switch_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    switch_record_sha256: str = Field(pattern=PREFIXED_SHA256_PATTERN)
    runner_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    nonce: str = Field(pattern=r"^[A-Za-z0-9_-]{22,128}$")
    runner_event_seq: int = Field(ge=1)
    runner_event_sha256: str = Field(pattern=PREFIXED_SHA256_PATTERN)


class SwitchStatus(BaseModel):
    """A Cloud-signed statement that the switch is on or off for one runner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.approval-mode-status/v1"] = STATUS_SCHEMA
    switch_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    switch_record_sha256: str = Field(pattern=PREFIXED_SHA256_PATTERN)
    runner_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    nonce: str = Field(pattern=r"^[A-Za-z0-9_-]{22,128}$")
    runner_event_seq: int = Field(ge=1)
    runner_event_sha256: str = Field(pattern=PREFIXED_SHA256_PATTERN)
    state: Literal["on", "off"]
    off_reason: Optional[OffReason] = None
    issued_at: str = Field(min_length=1, max_length=64)
    key_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    signature: str = Field(pattern=r"^[A-Za-z0-9+/]{86}==$")

    @model_validator(mode="after")
    def _off_reason_only_when_off(self) -> "SwitchStatus":
        if self.state == "on" and self.off_reason is not None:
            raise ValueError("an 'on' status carries no off reason")
        return self


def status_signing_payload(fields: Mapping[str, Any]) -> bytes:
    """The exact bytes Cloud signs: domain prefix plus canonical JSON."""

    unsigned = {key: value for key, value in fields.items() if key != "signature"}
    return _STATUS_DOMAIN + canonical_json(unsigned)


def sign_status(fields: Mapping[str, Any], private_key: Any) -> SwitchStatus:
    """Sign an unsigned status with an Ed25519 private key.

    This is the reference for Cloud's signer and the source of test vectors.
    The runner never holds a status-signing key.
    """

    unsigned = dict(fields)
    unsigned.pop("signature", None)
    # Normalize through the model first, so the signed bytes are exactly the
    # bytes a verifier re-derives from the parsed status (defaults included).
    placeholder = SwitchStatus.model_validate(
        {**unsigned, "signature": "A" * 86 + "=="}
    )
    signature = private_key.sign(
        status_signing_payload(placeholder.model_dump(mode="json"))
    )
    return SwitchStatus.model_validate(
        {
            **placeholder.model_dump(mode="json"),
            "signature": base64.b64encode(signature).decode("ascii"),
        }
    )


def verify_status(
    raw: Any,
    *,
    request: StatusRequest,
    pinned_keys: Mapping[str, str],
) -> SwitchStatus:
    """Accept a status only if it is signed by a pinned key and answers ``request``.

    ``pinned_keys`` maps a key id to a base64 raw 32-byte Ed25519 public key,
    pinned at runner registration. Any failure raises :class:`StatusRejected`.
    """

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        if isinstance(raw, SwitchStatus):
            status = raw
        elif isinstance(raw, (bytes, str)):
            status = SwitchStatus.model_validate_json(raw)
        else:
            status = SwitchStatus.model_validate(raw)
    except ValidationError as exc:
        raise StatusRejected("the status is malformed") from exc
    encoded_key = pinned_keys.get(status.key_id)
    if not encoded_key:
        raise StatusRejected("the status is signed by a key that is not pinned")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(encoded_key, validate=True)
        )
        public_key.verify(
            base64.b64decode(status.signature, validate=True),
            status_signing_payload(status.model_dump(mode="json")),
        )
    except (InvalidSignature, ValueError, TypeError, binascii.Error) as exc:
        raise StatusRejected("the status signature is invalid") from exc
    if status.switch_id != request.switch_id:
        raise StatusRejected("the status is for another switch")
    if status.switch_record_sha256 != request.switch_record_sha256:
        raise StatusRejected("the status is for another switch record")
    if status.runner_id != request.runner_id:
        raise StatusRejected("the status is for another runner")
    if status.nonce != request.nonce:
        raise StatusRejected("the status does not answer this runner's nonce")
    if (
        status.runner_event_seq != request.runner_event_seq
        or status.runner_event_sha256 != request.runner_event_sha256
    ):
        raise StatusRejected("the status does not include the runner's latest event")
    return status


# -- local switch log --------------------------------------------------------

LogEventKind = Literal[
    "opened",
    "item_started",
    "item_finished",
    "switched_off",
    "status_unconfirmed",
    "status_restored",
    "rearmed",
]


class RearmEvidence(BaseModel):
    """A person's signed re-arm of a switched-off switch (one tap).

    Signed with the person's approver key over everything they were shown:
    the switch, the off event and its reason, the recorded cause, and the run
    refs of the consecutive verified items since the switch went off.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    off_seq: int = Field(ge=1)
    off_sha256: str = Field(pattern=PREFIXED_SHA256_PATTERN)
    off_reason: str = Field(pattern=CODE_PATTERN)
    #: What the person was told caused the switch-off, in a sentence.
    cause: str = Field(min_length=1, max_length=500)
    clean_run_refs: tuple[str, ...] = Field(min_length=REARM_CLEAN_ITEMS)
    approver_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    key_id: str = Field(min_length=1, max_length=128)
    signed_at: str = Field(min_length=1, max_length=64)
    signature: str = Field(min_length=1, max_length=256)

    @field_validator("clean_run_refs")
    @classmethod
    def _refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        import re as _re

        if any(not _re.fullmatch(HEX_SHA256_PATTERN, ref) for ref in value):
            raise ValueError("a clean run ref is a hex SHA-256")
        return value


class SwitchLogEvent(BaseModel):
    """One hash-chained event in the runner's local switch log."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.approval-mode-log-event/v1"] = LOG_EVENT_SCHEMA
    seq: int = Field(ge=1)
    kind: LogEventKind
    switch_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    at: str = Field(min_length=1, max_length=64)
    #: ``opened`` only: the digest of the switch record the log belongs to.
    switch_record_sha256: Optional[str] = Field(
        default=None, pattern=PREFIXED_SHA256_PATTERN
    )
    #: SHA-256 of the run id (``RunReport.run_id_sha256``), never the id.
    run_ref: Optional[str] = Field(default=None, pattern=HEX_SHA256_PATTERN)
    decision: Optional[ItemDecisionKind] = None
    reason: Optional[str] = Field(default=None, pattern=CODE_PATTERN)
    counts_toward_problems: Optional[bool] = None
    #: ``rearmed`` only: the person's signed re-arm.
    rearm: Optional[RearmEvidence] = None
    prev_sha256: str = Field(pattern=PREFIXED_SHA256_PATTERN)
    sha256: str = Field(pattern=PREFIXED_SHA256_PATTERN)

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler: Any) -> dict[str, Any]:
        """Leave ``rearm`` out when unset, so every other event (and every
        existing log line and hash) serializes exactly as before."""

        data: dict[str, Any] = handler(self)
        if self.rearm is None:
            data.pop("rearm", None)
        return data

    @model_validator(mode="after")
    def _shape_per_kind(self) -> "SwitchLogEvent":
        kind = self.kind
        if (kind == "opened") != (self.switch_record_sha256 is not None):
            raise ValueError("only an 'opened' event names the switch record")
        needs_run = kind in {"item_started", "item_finished"}
        if needs_run != (self.run_ref is not None):
            raise ValueError("item events, and only item events, carry a run ref")
        finished = kind == "item_finished"
        if finished != (self.decision is not None) or finished != (
            self.counts_toward_problems is not None
        ):
            raise ValueError("only an 'item_finished' event carries a decision")
        if kind == "switched_off":
            if self.reason is None or self.reason not in {r.value for r in OffReason}:
                raise ValueError("a 'switched_off' event names a closed reason")
        if (kind == "rearmed") != (self.rearm is not None):
            raise ValueError("only a 'rearmed' event carries a signed re-arm")
        return self


def _event_sha256(fields: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in fields.items() if key != "sha256"}
    return (
        "sha256:" + hashlib.sha256(_LOG_DOMAIN + canonical_json(unsigned)).hexdigest()
    )


@dataclass
class SwitchLogState:
    """What the log says, computed from a verified chain."""

    switch_id: str
    switch_record_sha256: str
    last_seq: int
    last_sha256: str
    off_reason: Optional[OffReason] = None
    status_unconfirmed: bool = False
    #: run refs with ``item_started`` and no ``item_finished``.
    open_items: list[str] = field(default_factory=list)
    #: The latest ``item_finished`` per run ref, in log order.
    finished: list[SwitchLogEvent] = field(default_factory=list)

    @property
    def is_off(self) -> bool:
        return self.off_reason is not None


def summarize_events(
    events: list[SwitchLogEvent],
    *,
    approver_keys: Optional[Mapping[str, str]] = None,
) -> SwitchLogState:
    """Fold a verified event list into its state.

    A ``rearmed`` event turns the switch back on only when ``approver_keys``
    (key id to base64 raw Ed25519 public key, pinned for this runner) verify
    it and it meets the re-arm rule against the log before it
    (:func:`rearm_refusal`). Otherwise it is ignored and the switch stays off.
    """

    if not events or events[0].kind != "opened":
        raise SwitchLogError("the switch log does not start with 'opened'")
    first = events[0]
    assert first.switch_record_sha256 is not None
    state = SwitchLogState(
        switch_id=first.switch_id,
        switch_record_sha256=first.switch_record_sha256,
        last_seq=events[-1].seq,
        last_sha256=events[-1].sha256,
    )
    started: dict[str, bool] = {}
    latest_finished: dict[str, SwitchLogEvent] = {}
    for index, event in enumerate(events[1:], start=1):
        if event.kind == "opened":
            raise SwitchLogError("the switch log was opened twice")
        if event.kind == "switched_off" and state.off_reason is None:
            assert event.reason is not None
            state.off_reason = OffReason(event.reason)
        elif event.kind == "rearmed" and state.off_reason is not None:
            assert event.rearm is not None
            if approver_keys and (
                rearm_refusal(events[:index], event.rearm, approver_keys) is None
            ):
                state.off_reason = None
        elif event.kind == "status_unconfirmed":
            state.status_unconfirmed = True
        elif event.kind == "status_restored":
            state.status_unconfirmed = False
        elif event.kind == "item_started":
            assert event.run_ref is not None
            started[event.run_ref] = True
        elif event.kind == "item_finished":
            assert event.run_ref is not None
            started.pop(event.run_ref, None)
            latest_finished.pop(event.run_ref, None)
            latest_finished[event.run_ref] = event
    state.open_items = list(started)
    state.finished = list(latest_finished.values())
    return state


class SwitchLog:
    """Append-only, hash-chained JSON-lines log kept on the runner.

    Every read verifies the whole chain. Every append takes an exclusive file
    lock, re-reads and verifies the chain, then writes one line and fsyncs.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # -- creation ------------------------------------------------------------
    @classmethod
    def open_new(
        cls,
        path: Path | str,
        record: SwitchRecord,
        *,
        at: Optional[str] = None,
    ) -> "SwitchLog":
        """Create the log for a newly accepted switch record.

        Refuses to reuse a path that already holds a log: a closed switch
        record stays closed, and a new record gets a new log.
        """

        log = cls(path)
        log.path.parent.mkdir(parents=True, exist_ok=True)
        with log._lock():
            if log.path.exists() and log.path.stat().st_size > 0:
                raise SwitchLogError("a switch log already exists at this path")
            log._write_event(
                {
                    "schema_version": LOG_EVENT_SCHEMA,
                    "seq": 1,
                    "kind": "opened",
                    "switch_id": record.switch_id,
                    "at": at or _utc_now(),
                    "switch_record_sha256": record.digest(),
                    "run_ref": None,
                    "decision": None,
                    "reason": None,
                    "counts_toward_problems": None,
                    "prev_sha256": GENESIS_SHA256,
                }
            )
        return log

    # -- reading -------------------------------------------------------------
    def read(self) -> list[SwitchLogEvent]:
        """Return every event after verifying the chain, or raise."""

        try:
            raw = self.path.read_bytes()
        except FileNotFoundError as exc:
            raise SwitchLogError("the switch log is missing") from exc
        except OSError as exc:
            raise SwitchLogError("the switch log could not be read") from exc
        if not raw.endswith(b"\n"):
            raise SwitchLogError("the switch log ends with a partial line")
        events: list[SwitchLogEvent] = []
        previous = GENESIS_SHA256
        switch_id: Optional[str] = None
        for index, line in enumerate(raw.split(b"\n")[:-1], start=1):
            try:
                payload = json.loads(line)
                event = SwitchLogEvent.model_validate(payload)
            except (ValueError, ValidationError) as exc:
                raise SwitchLogError("a switch log line is malformed") from exc
            if canonical_json(payload) != line:
                raise SwitchLogError("a switch log line is not canonical")
            if event.seq != index:
                raise SwitchLogError("the switch log sequence has a gap")
            if event.prev_sha256 != previous:
                raise SwitchLogError("the switch log hash chain is broken")
            if _event_sha256(payload) != event.sha256:
                raise SwitchLogError("a switch log event hash does not match")
            if switch_id is None:
                switch_id = event.switch_id
            elif event.switch_id != switch_id:
                raise SwitchLogError("the switch log mixes switch records")
            previous = event.sha256
            events.append(event)
        if not events:
            raise SwitchLogError("the switch log is empty")
        return events

    def state(
        self, *, approver_keys: Optional[Mapping[str, str]] = None
    ) -> SwitchLogState:
        return summarize_events(self.read(), approver_keys=approver_keys)

    # -- appending -----------------------------------------------------------
    def append(
        self,
        kind: LogEventKind,
        *,
        at: Optional[str] = None,
        run_ref: Optional[str] = None,
        decision: Optional[ItemDecisionKind] = None,
        reason: Optional[str] = None,
        counts_toward_problems: Optional[bool] = None,
        rearm: Optional[RearmEvidence] = None,
    ) -> SwitchLogEvent:
        if kind == "opened":
            raise SwitchLogError("use SwitchLog.open_new to open a log")
        with self._lock():
            events = self.read()
            last = events[-1]
            fields: dict[str, Any] = {
                "schema_version": LOG_EVENT_SCHEMA,
                "seq": last.seq + 1,
                "kind": kind,
                "switch_id": last.switch_id,
                "at": at or _utc_now(),
                "switch_record_sha256": None,
                "run_ref": run_ref,
                "decision": decision.value if decision is not None else None,
                "reason": reason,
                "counts_toward_problems": counts_toward_problems,
                "prev_sha256": last.sha256,
            }
            if rearm is not None:
                fields["rearm"] = rearm.model_dump(mode="json")
            return self._write_event(fields)

    def _write_event(self, fields: dict[str, Any]) -> SwitchLogEvent:
        fields = dict(fields)
        fields["sha256"] = _event_sha256(fields)
        event = SwitchLogEvent.model_validate(fields)
        line = canonical_json(event.model_dump(mode="json")) + b"\n"
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            written = os.write(descriptor, line)
            if written != len(line):
                raise SwitchLogError("the switch log append was incomplete")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return event

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(  # type: ignore[attr-defined]
                    descriptor,
                    msvcrt.LK_LOCK,  # type: ignore[attr-defined]
                    1,
                )
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(  # type: ignore[attr-defined]
                        descriptor,
                        msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
                        1,
                    )
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def turn_off(
    log: SwitchLog,
    reason: OffReason = OffReason.TURNED_OFF,
    *,
    at: Optional[str] = None,
) -> SwitchLogEvent:
    """Record a local switch-off. It applies before the next save, offline too.

    The switch record is closed for good once this event exists. Turning the
    switch on again needs a new record and a new log.
    """

    return log.append("switched_off", reason=reason.value, at=at)


# -- re-arming a switched-off switch -------------------------------------------


def _last_off(events: list[SwitchLogEvent]) -> Optional[SwitchLogEvent]:
    """The ``switched_off`` event that holds the switch off now, if any."""

    off: Optional[SwitchLogEvent] = None
    for event in events:
        if event.kind == "switched_off" and off is None:
            off = event
        elif event.kind == "rearmed":
            off = None
    return off


def clean_streak(events: list[SwitchLogEvent]) -> tuple[str, ...]:
    """Run refs of the consecutive verified items since the switch went off.

    The trailing run of ``item_finished`` events after the off event whose
    decision is a verified item (``no_person`` or ``approved``). Any other
    finished item resets it; an item still open after the last one does too.
    """

    off = _last_off(events)
    if off is None:
        return ()
    streak: list[str] = []
    started: set[str] = set()
    for event in events[off.seq :]:
        if event.kind == "item_started" and event.run_ref:
            started.add(event.run_ref)
        elif event.kind == "item_finished" and event.run_ref:
            started.discard(event.run_ref)
            if event.decision is not None and event.decision.value in _CLEAN_DECISIONS:
                streak.append(event.run_ref)
            else:
                streak = []
    return () if started else tuple(streak)


def _public_key_bytes(encoded: str) -> bytes:
    """A raw Ed25519 public key, given as hex (the principal registry's form)
    or base64 (the pinned-key form status keys use)."""

    import re as _re

    if _re.fullmatch(r"[0-9a-f]{64}", encoded):
        return bytes.fromhex(encoded)
    return base64.b64decode(encoded, validate=True)


def registry_approver_keys(key_dir: Optional[Path] = None) -> dict[str, str]:
    """Approver keys registered in the JIT key directory's principal registry.

    ``approvers`` maps a person's approver name (for example Desktop's
    ``desktop-approver``) to its hex public key (``jit register --role
    approver``). An unreadable registry, or one with no approvers, gives no
    keys, and then no re-arm counts.
    """

    try:
        from openadapt_flow.jit.authority import PrincipalRegistry
        from openadapt_flow.jit.lifecycle import default_key_dir

        registry = PrincipalRegistry.load(key_dir or default_key_dir())
    except Exception:  # noqa: BLE001 - no registry proves nothing
        return {}
    approvers = getattr(registry, "approvers", None) or {}
    return {str(name): str(key) for name, key in dict(approvers).items()}


def rearm_signing_payload(
    switch_id: str, switch_record_sha256: str, fields: Mapping[str, Any]
) -> bytes:
    """The bytes a person's approver key signs to re-arm a switch."""

    unsigned = {k: v for k, v in fields.items() if k != "signature"}
    return _REARM_DOMAIN + canonical_json(
        {
            "switch_id": switch_id,
            "switch_record_sha256": switch_record_sha256,
            **unsigned,
        }
    )


def rearm_request(log: SwitchLog, *, cause: str) -> dict[str, Any]:
    """What a person is shown before re-arming, or raise when it isn't offered.

    Offered only while the switch is off and after
    :data:`REARM_CLEAN_ITEMS` consecutive verified items since it went off.
    Returns the unsigned fields the person's key signs (without
    ``approver_id``, ``key_id``, ``signed_at``).
    """

    events = log.read()
    off = _last_off(events)
    if off is None:
        raise SwitchLogError("the switch is on; there is nothing to re-arm")
    streak = clean_streak(events)
    if len(streak) < REARM_CLEAN_ITEMS:
        raise SwitchLogError(
            f"{len(streak)} consecutive verified items since the switch went "
            f"off; a re-arm needs {REARM_CLEAN_ITEMS}"
        )
    return {
        "off_seq": off.seq,
        "off_sha256": off.sha256,
        "off_reason": off.reason,
        "cause": cause,
        "clean_run_refs": list(streak[-REARM_CLEAN_ITEMS:]),
    }


def rearm_to_sign(
    log: SwitchLog,
    request: Mapping[str, Any],
    *,
    approver_id: str,
    key_id: str,
    signed_at: Optional[str] = None,
) -> tuple[dict[str, Any], bytes]:
    """The fields to sign and their exact bytes, for a key held elsewhere.

    For a key that never leaves a keychain: sign the returned bytes there,
    then pass the fields and the base64 signature to :func:`rearm_evidence`.
    """

    first = log.read()[0]
    assert first.switch_record_sha256 is not None
    fields = {
        **dict(request),
        "approver_id": approver_id,
        "key_id": key_id,
        "signed_at": signed_at or _utc_now(),
    }
    return fields, rearm_signing_payload(
        first.switch_id, first.switch_record_sha256, fields
    )


def rearm_evidence(fields: Mapping[str, Any], signature_b64: str) -> RearmEvidence:
    """The re-arm from :func:`rearm_to_sign`'s fields and a signature made
    elsewhere. :func:`rearm` verifies it; nothing is trusted here."""

    return RearmEvidence.model_validate({**dict(fields), "signature": signature_b64})


def sign_rearm(
    log: SwitchLog,
    request: Mapping[str, Any],
    *,
    approver_id: str,
    key_id: str,
    private_key: Any,
    signed_at: Optional[str] = None,
) -> RearmEvidence:
    """A person's approver key signs the re-arm they were shown."""

    fields, payload = rearm_to_sign(
        log, request, approver_id=approver_id, key_id=key_id, signed_at=signed_at
    )
    signature = private_key.sign(payload)
    return rearm_evidence(fields, base64.b64encode(signature).decode("ascii"))


def rearm_refusal(
    events: list[SwitchLogEvent],
    rearm: RearmEvidence,
    approver_keys: Mapping[str, str],
) -> Optional[str]:
    """Why ``rearm`` may not turn the switch on after ``events``, or None."""

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not events or events[0].switch_record_sha256 is None:
        return "the switch log is not readable"
    off = _last_off(events)
    if off is None:
        return "the switch is on; there is nothing to re-arm"
    if rearm.off_seq != off.seq or rearm.off_sha256 != off.sha256:
        return "the re-arm names another switch-off"
    if rearm.off_reason != off.reason:
        return "the re-arm names another off reason"
    streak = clean_streak(events)
    if len(streak) < REARM_CLEAN_ITEMS:
        return (
            f"{len(streak)} consecutive verified items since the switch went off; "
            f"a re-arm needs {REARM_CLEAN_ITEMS}"
        )
    if tuple(rearm.clean_run_refs) != tuple(streak[-REARM_CLEAN_ITEMS:]):
        return "the re-arm names other verified items than the log"
    encoded = approver_keys.get(rearm.key_id)
    if not encoded:
        return "the re-arm is not signed by a registered approver key"
    try:
        public_key = Ed25519PublicKey.from_public_bytes(_public_key_bytes(encoded))
        public_key.verify(
            base64.b64decode(rearm.signature, validate=True),
            rearm_signing_payload(
                events[0].switch_id,
                events[0].switch_record_sha256,
                rearm.model_dump(mode="json"),
            ),
        )
    except (InvalidSignature, ValueError, TypeError, binascii.Error):
        return "the re-arm signature does not verify"
    return None


def rearm(
    log: SwitchLog,
    evidence: RearmEvidence,
    *,
    approver_keys: Mapping[str, str],
    at: Optional[str] = None,
) -> SwitchLogEvent:
    """Record a person's signed re-arm, or raise :class:`SwitchLogError`.

    Never automatic: it needs a signature by a pinned approver key over the
    switch-off, its cause and the :data:`REARM_CLEAN_ITEMS` consecutive
    verified items since. The event records who signed and what they saw.
    """

    refusal = rearm_refusal(log.read(), evidence, approver_keys)
    if refusal is not None:
        raise SwitchLogError(refusal)
    # Every reader checks the re-arm again against the log before it, so an
    # event that lands between this check and the append can only make the
    # re-arm count for nothing, never turn the switch on early.
    return log.append("rearmed", rearm=evidence, at=at)
