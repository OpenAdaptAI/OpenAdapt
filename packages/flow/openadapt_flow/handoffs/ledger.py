"""The handoff ledger: what we sent, to whom, and what they say they recorded.

One JSON file per handoff in a private directory (0700, files 0600). A
handoff keeps:

- when it was sent and when someone must follow up (``follow_up_due``);
- who the other party is, as a short label, and who follows up;
- the item's key and each sent field's value, only as HMAC-SHA256 digests of
  the normalized value under the ledger's salt, so a read-back can compare
  without the ledger ever holding a value;
- every observation, in order. An observation is never edited or removed.

The handoff's state is its latest observation's state, or ``sent`` with none.
An observation that learned nothing new (the receiver was unreachable, the
key matched two records) carries the previous state with a reason.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.runtime.effects.readback_values import ValueKind, normalize_value

SCHEMA: Literal["openadapt.handoff/v1"] = "openadapt.handoff/v1"

HandoffState = Literal[
    "sent", "acknowledged", "recorded_as_intended", "recorded_differently"
]
#: How the other system was read.
ReadMethod = Literal[
    "x12_999", "x12_277ca", "report_csv", "report_text", "api_readback", "ui_readback"
]
#: ``field_readback``: the fields were read back and compared.
#: ``status_only``: the other side gave a status for the item, not its fields.
Strength = Literal["field_readback", "status_only", "none"]
#: Why an observation needs a person, or learned nothing new.
Reason = Literal[
    "rejected_by_counterparty",
    "conflicting_report",
    "ambiguous_key",
    "receiver_unreachable",
    "not_found",
    "no_answer_by_deadline",
]
#: The receipt schema's kinds of other system.
SystemKind = Literal[
    "ehr",
    "payer_portal",
    "bank",
    "erp",
    "crm",
    "government_portal",
    "email",
    "other",
]
#: Reasons that need a person even before the follow-up time.
ATTENTION: frozenset[str] = frozenset(
    {"rejected_by_counterparty", "conflicting_report", "ambiguous_key"}
)
Status = Literal["needs_follow_up", "waiting", "done"]

_FIELD_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
_DIGEST = r"^[0-9a-f]{64}$"
_LABEL_MAX = 80
_SALT_FILE = ".salt"
_LOCK_FILE = ".lock"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_seconds(moment: datetime) -> str:
    """Whole seconds in UTC with a ``Z``, as the receipt line takes it."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldSpec(_Closed):
    """One field we sent: how it compares, and its value's digest."""

    kind: ValueKind = "text"
    digest: str = Field(pattern=_DIGEST)
    #: For a date written day-first or month-first (``dmy``/``mdy``).
    date_order: Optional[Literal["dmy", "mdy"]] = None


class Observation(_Closed):
    """One look at the other system. Never edited after it is written."""

    state: HandoffState
    observed_at: str
    read_method: ReadMethod
    strength: Strength
    #: Field ids that differ from what we sent (never the values).
    differing_fields: list[str] = Field(default_factory=list, max_length=64)
    #: For each compared field, the digest of the value the other side holds
    #: (``missing`` when it holds none). Private: it opens only in the
    #: receipt's private openings.
    observed_digests: dict[str, str] = Field(default_factory=dict)
    #: A status-only report accepted the item (a 277CA ``A2``). It still says
    #: nothing about the fields, so the state stays ``acknowledged``; the
    #: handoff needs no follow-up.
    counterparty_accepted: bool = False
    reason: Optional[Reason] = None
    #: SHA-256 of the report or read-back body this came from.
    evidence_sha256: Optional[str] = Field(default=None, pattern=_DIGEST)


class Handoff(_Closed):
    schema_version: Literal["openadapt.handoff/v1"] = SCHEMA
    id: str = Field(pattern=r"^handoff:[0-9a-f]{32}$")
    sent_at: str
    follow_up_due: str
    #: The other party, for people ("payer 277CA", "receiving clinic").
    counterparty: str = Field(max_length=_LABEL_MAX)
    system_kind: SystemKind = "other"
    #: Who follows up when it is overdue or differs ("billing", "referrals desk").
    follow_up: str = Field(default="the sender", max_length=_LABEL_MAX)
    key_digest: str = Field(pattern=_DIGEST)
    fields: dict[str, FieldSpec] = Field(default_factory=dict, max_length=64)
    #: Where it came from: a deferred check's id, or a run's id.
    source: Optional[str] = Field(default=None, max_length=128)
    run_id: Optional[str] = Field(default=None, max_length=128)
    step: Optional[int] = Field(default=None, ge=0, le=10_000)
    observations: list[Observation] = Field(default_factory=list)

    @property
    def state(self) -> HandoffState:
        evidence = self.evidence
        return evidence.state if evidence is not None else "sent"

    @property
    def last(self) -> Optional[Observation]:
        return self.observations[-1] if self.observations else None

    @property
    def evidence(self) -> Optional[Observation]:
        """The latest observation that learned something: an unreachable
        receiver says nothing about the record, so it never replaces an
        earlier read. The latest one when every read failed."""

        for observation in reversed(self.observations):
            if observation.reason != "receiver_unreachable":
                return observation
        return self.last

    def overdue(self, now: Optional[datetime] = None) -> bool:
        return parse_iso(self.follow_up_due) <= (now or now_utc())

    def status(self, now: Optional[datetime] = None) -> Status:
        """``done`` only when recorded as intended (or accepted by a
        status-only report) and nothing since says otherwise; ``needs_follow_up`` when it differs, was refused, is
        ambiguous, or is past its follow-up time; else ``waiting``."""

        last = self.evidence
        reason = last.reason if last is not None else None
        if reason is None and (
            self.state == "recorded_as_intended"
            or (last is not None and last.counterparty_accepted)
        ):
            return "done"
        if (
            self.state == "recorded_differently"
            or reason in ATTENTION
            or self.overdue(now)
        ):
            return "needs_follow_up"
        return "waiting"


class LedgerError(ValueError):
    """The ledger refused a change. The message carries no record values."""


class HandoffLedger:
    """A directory of handoffs, changed only under a file lock."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    # -- storage ------------------------------------------------------------
    def _ensure(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._ensure()
        import fcntl

        with open(self.directory / _LOCK_FILE, "a+") as handle:
            os.chmod(self.directory / _LOCK_FILE, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _salt(self) -> bytes:
        path = self.directory / _SALT_FILE
        if not path.exists():
            self._ensure()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(secrets.token_hex(32))
        return bytes.fromhex(path.read_text().strip())

    def digest(self, text: str) -> str:
        return hmac.new(self._salt(), text.encode("utf-8"), hashlib.sha256).hexdigest()

    def field_digest(
        self, value: str, kind: ValueKind, date_order: Optional[str] = None
    ) -> Optional[str]:
        """The digest of ``value`` normalized for ``kind``; None if it doesn't
        parse as that kind (a date that reads two ways, a word for a number)."""

        normal = normalize_value(str(value), kind, date_order=date_order)
        return None if normal is None else self.digest(f"{kind}\x00{normal}")

    def _path(self, handoff_id: str) -> Path:
        return self.directory / f"{handoff_id.split(':', 1)[1]}.json"

    def _write(self, handoff: Handoff) -> None:
        path = self._path(handoff.id)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(handoff.model_dump_json(indent=2))
        os.replace(tmp, path)

    def get(self, handoff_id: str) -> Optional[Handoff]:
        path = self._path(handoff_id)
        if not path.exists():
            return None
        return Handoff.model_validate_json(path.read_text())

    def entries(self) -> list[Handoff]:
        if not self.directory.exists():
            return []
        found = [
            Handoff.model_validate_json(path.read_text())
            for path in sorted(self.directory.glob("*.json"))
        ]
        return sorted(found, key=lambda h: (h.sent_at, h.id))

    def with_key(self, key: str) -> list[Handoff]:
        digest = self.digest(key)
        return [h for h in self.entries() if h.key_digest == digest]

    # -- changes ------------------------------------------------------------
    def send(
        self,
        key: str,
        fields: Mapping[str, tuple[str, ValueKind] | tuple[str, ValueKind, str]],
        *,
        counterparty: str,
        follow_up_hours: float,
        system_kind: SystemKind = "other",
        follow_up: str = "the sender",
        source: Optional[str] = None,
        run_id: Optional[str] = None,
        step: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> Handoff:
        """Record that we sent the item ``key`` with these field values.

        ``fields`` maps a field id to ``(value, kind)`` or ``(value, kind,
        date_order)``. A value that doesn't normalize for its kind is refused,
        since it could never be compared. A key that another open handoff
        already uses is refused: a read-back couldn't tell them apart.
        """

        if not key:
            raise LedgerError("a handoff needs the item's key")
        if follow_up_hours <= 0:
            raise LedgerError("follow_up_hours must be positive")
        specs: dict[str, FieldSpec] = {}
        if len(fields) > 31:
            raise LedgerError("a handoff compares at most 31 fields")
        for field_id, spec in fields.items():
            if not _FIELD_ID.fullmatch(field_id):
                raise LedgerError(
                    f"field id {field_id!r} must be lower case, digits and _, "
                    "at most 32 characters"
                )
            value, kind = spec[0], spec[1]
            order = spec[2] if len(spec) > 2 else None  # type: ignore[misc]
            digest = self.field_digest(value, kind, order)
            if digest is None:
                raise LedgerError(f"field {field_id!r} doesn't read as a {kind}")
            specs[field_id] = FieldSpec(kind=kind, digest=digest, date_order=order)
        moment = now or now_utc()
        with self._locked():
            key_digest = self.digest(key)
            for other in self.entries():
                if other.key_digest == key_digest and other.status(moment) != "done":
                    raise LedgerError("another open handoff already uses this key")
            handoff = Handoff(
                id=f"handoff:{secrets.token_hex(16)}",
                sent_at=iso_seconds(moment),
                follow_up_due=iso_seconds(moment + timedelta(hours=follow_up_hours)),
                counterparty=counterparty[:_LABEL_MAX],
                system_kind=system_kind,
                follow_up=follow_up[:_LABEL_MAX],
                key_digest=key_digest,
                fields=specs,
                source=source,
                run_id=run_id,
                step=step,
            )
            self._write(handoff)
        return handoff

    def adopt(self, handoff: Handoff) -> Handoff:
        """Add a handoff made elsewhere (a deferred check) unless it exists."""

        with self._locked():
            existing = self.get(handoff.id)
            if existing is not None:
                return existing
            self._write(handoff)
            return handoff

    def observe(self, handoff_id: str, observation: Observation) -> Handoff:
        """Append one observation. Earlier ones are never changed."""

        with self._locked():
            current = self.get(handoff_id)
            if current is None:
                raise LedgerError("no such handoff")
            updated = current.model_copy(
                update={"observations": [*current.observations, observation]}
            )
            self._write(updated)
            return updated


def evidence_digest(body: str | bytes) -> str:
    data = body.encode("utf-8") if isinstance(body, str) else body
    return hashlib.sha256(data).hexdigest()


def dump_json(handoff: Handoff) -> str:
    return json.dumps(handoff.model_dump(mode="json"), sort_keys=True)
