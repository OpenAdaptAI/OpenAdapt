"""Delayed recheck of an API read-back and the system's own activity log.

Most systems give no database access. A write checked there has two sources
the system itself produces: a read of the record through its API, and, often,
a log of what each caller changed. A check made right after the write says
what those sources showed at that moment. A background job, a webhook
handler, a colleague's edit or a delayed duplicate can change things later.
This module repeats the check after a window the caller sets.

``begin`` runs right after a verified first check. It takes a log checkpoint,
then reads the record, in that order, so nothing the log records can fall
between the two reads unseen. It keeps a private state: a salt, keyed MACs of
the declared fields and of the record's version (never the values), the
log's opaque checkpoint, and keyed MACs of the write's own events. The
receipt carries only ``state_sha256`` and the due time.

``recheck`` runs at or after the due time. It reads the record again, reads
the log from the checkpoint and judges:

- ``VERIFIED``: as of the recheck time, the read-back showed the same
  declared values and version, and the log, read back to the checkpoint,
  holds no mutation of the record and no mutation by this caller that no
  later declared write accounts for. It never means "persisted": a change
  after the recheck read is not seen.
- ``REFUTED``: the record no longer shows the declared values, is gone, or
  this caller's log entries show a mutation nothing accounts for (late
  collateral, or an extra write to the record).
- ``NOT_CHECKED``: anything else. The recheck was early; the state is
  malformed or doesn't match the receipt; the first check wasn't verified; a
  read failed; the log didn't reach the checkpoint; another actor or an
  unattributed entry changed the record; the version moved while the values
  stayed (A to B to A); the log returned entries it didn't let us inspect.

The local clock only decides when the recheck is due. Which log entries are
in the window is decided by the log's own checkpoint and entry ids, never by
comparing clocks: Medplum's server clock ran about 1.5 s off the client's.

The recheck can only keep or lower the first result. It never turns a first
result that wasn't verified into ``VERIFIED``.

Sources are adapters. :class:`VerifierReadback` wraps any verifier adapter on
main (REST, FHIR, GraphQL, SQL), so the read-back half works with what exists
today. The log half is the :class:`ActivityLog` protocol; a reader of a
system's audit or event log implements it with a thin wrapper. This module
imports nothing from ``tx`` or ``txplan``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Protocol

from openadapt_flow.runtime.effects.late_write import (
    LateOutcome,
    due_at,
    format_time,
    seconds_until,
)

#: The private recheck state's schema.
SCHEMA = "openadapt.api-recheck/v1"
#: Exactly these keys: nothing rides along unbound.
STATE_KEYS: tuple[str, ...] = (
    "schema_version",
    "salt",
    "target",
    "first_outcome",
    "read_at",
    "window_s",
    "due_at",
    "fields_mac",
    "version_mac",
    "log",
    "own_events",
)

Outcome = Literal["VERIFIED", "REFUTED", "NOT_CHECKED"]
#: Who made a log entry, as the log adapter attributes it.
By = Literal["self", "other", "unknown"]
#: What the entry did. ``read`` entries are ignored; ``unknown`` never
#: confirms anything.
Kind = Literal["create", "update", "delete", "read", "unknown"]
Op = Literal["create", "update", "delete"]
_ALL_OPS: tuple[Op, ...] = ("create", "update", "delete")

#: The coordinator's basis names this module can produce.
BASIS_API = "system_api"
BASIS_LOG = "activity_log"
#: Always listed: a read of the record and the log can't see these.
ALWAYS_NOT_COVERED: tuple[str, ...] = (
    "server-side async and outbound effects not observed",
    "changes after the recheck read",
)

#: The recheck outcome in the older vocabularies.
TO_LATE: dict[Outcome, LateOutcome] = {
    "VERIFIED": "CONFIRMED",
    "REFUTED": "REFUTED",
    "NOT_CHECKED": "UNCERTAIN",
}
#: The ``effect.Verdict`` value of each outcome.
TO_VERDICT: dict[Outcome, str] = {
    "VERIFIED": "confirmed",
    "REFUTED": "refuted",
    "NOT_CHECKED": "indeterminate",
}


class ReadFailed(RuntimeError):
    """A source could not be read. Nothing is known about it."""


# -- sources -------------------------------------------------------------------


@dataclass(frozen=True)
class Readback:
    """One read of the record through the system's API.

    ``fields``: the declared fields as read (judged: a change refutes).
    ``version``: anything that moves whenever the record changes (an ETag,
    FHIR ``meta.versionId``, a digest of the whole record). A moved version
    with unchanged fields is a change the fields don't show: never VERIFIED.
    ``found``: False when the system answered that the record is not there.
    """

    found: bool
    fields: Mapping[str, Any] = field(default_factory=dict)
    version: Optional[str] = None


class RecordSource(Protocol):
    """Reads the one record a write made or changed."""

    def read(self) -> Readback:
        """The record now; raises :class:`ReadFailed` when it can't be read."""
        ...


@dataclass(frozen=True)
class LogEntry:
    """One entry of the system's own log, as the adapter normalized it."""

    id: str
    kind: Kind
    by: By
    #: Whether the entry names the checked record.
    on_target: bool


@dataclass(frozen=True)
class LogRead:
    """The entries after a checkpoint.

    ``reached``: the read got back to the checkpoint. A read that stopped
    short (a page limit, a failed page) leaves the window open.
    ``hidden``: entries the reader saw but did not return (for example
    another caller's entries a reader filters out). Any hidden entry may
    have touched the record, so it blocks VERIFIED.
    ``logs``: the operations this log records. A kind it doesn't record
    can't be seen, and the result lists it as not covered.
    """

    entries: Sequence[LogEntry]
    reached: bool
    hidden: int = 0
    logs: tuple[Op, ...] = _ALL_OPS


class ActivityLog(Protocol):
    """A system's audit or event log, read by its own positions."""

    def checkpoint(self) -> Mapping[str, Any]:
        """An opaque, JSON-safe position: the newest entry ids or a stream
        cursor. Raises :class:`ReadFailed` when the log can't be placed."""
        ...

    def since(self, checkpoint: Mapping[str, Any]) -> LogRead:
        """Every entry after ``checkpoint``, newest first or in log order.
        Raises :class:`ReadFailed` when the log can't be read."""
        ...


# -- digests -------------------------------------------------------------------


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()


def _mac(salt: bytes, label: str, value: Any) -> str:
    return hmac.new(
        salt, label.encode() + b"\0" + _canonical(value), hashlib.sha256
    ).hexdigest()


def state_sha256(state: Mapping[str, Any]) -> str:
    """The digest a receipt binds: the whole private state, salt included."""

    return "sha256:" + hashlib.sha256(_canonical(dict(state))).hexdigest()


# -- result --------------------------------------------------------------------


@dataclass
class RecheckResult:
    outcome: Outcome
    reason: str
    #: Plain fields for a receipt's evidence-scope block.
    basis: list[str]
    read_back_at: Optional[str]
    rechecked_at: Optional[str]
    recheck_window_s: Optional[float]
    not_covered: list[str]
    entries_after_checkpoint: int = 0
    other_actor_entries: int = 0

    @property
    def late_outcome(self) -> LateOutcome:
        return TO_LATE[self.outcome]

    def scope(self) -> dict[str, Any]:
        """The evidence-scope fields this recheck can state. Wire scope is
        not this module's to state; a receipt adds it."""

        return {
            "check_level": "system_of_record" if self.outcome == "VERIFIED" else None,
            "basis": self.basis[0] if self.basis else None,
            "bases": list(self.basis),
            "read_back_at": self.read_back_at,
            "rechecked_at": self.rechecked_at,
            "recheck_window_s": self.recheck_window_s,
            "not_covered": list(self.not_covered),
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "entries_after_checkpoint": self.entries_after_checkpoint,
            "other_actor_entries": self.other_actor_entries,
            "scope": self.scope(),
        }


def _not_checked(
    reason: str, state: Optional[Mapping[str, Any]] = None, *, at: Optional[str] = None
) -> RecheckResult:
    basis: list[str] = []
    if isinstance(state, Mapping):
        basis = [BASIS_API] + ([BASIS_LOG] if state.get("log") is not None else [])
    window = state.get("window_s") if isinstance(state, Mapping) else None
    return RecheckResult(
        "NOT_CHECKED",
        reason,
        basis=basis,
        read_back_at=str(state.get("read_at")) if isinstance(state, Mapping) else None,
        rechecked_at=at,
        recheck_window_s=window if isinstance(window, (int, float)) else None,
        not_covered=list(ALWAYS_NOT_COVERED),
    )


# -- begin ---------------------------------------------------------------------


def begin(
    record: RecordSource,
    *,
    target: str,
    first_outcome: str,
    window_s: float,
    log: Optional[ActivityLog] = None,
    own_event_ids: Sequence[str] = (),
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The private recheck state, taken right after the first check.

    ``target``: the record's reference (``Patient/123``); bound, never
    re-read from the state. ``first_outcome``: the first check's outcome; a
    recheck of anything but ``VERIFIED`` stays ``NOT_CHECKED``.
    ``own_event_ids``: the write's own log entries, which the first check
    already judged; they may land after the checkpoint while the log lags.

    The log checkpoint is taken before the read, so an entry between the two
    reads shows up in the window rather than in the baseline. Raises
    :class:`ReadFailed` when either source can't be read, and ``ValueError``
    for a window that isn't a positive number of seconds.
    """

    if (
        isinstance(window_s, bool)
        or not isinstance(window_s, (int, float))
        or not window_s > 0
        or window_s == float("inf")
    ):
        raise ValueError("the recheck window must be a positive number of seconds")
    salt = secrets.token_bytes(32)
    checkpoint = None if log is None else dict(log.checkpoint())
    seen = record.read()
    if not isinstance(seen, Readback):
        raise ReadFailed("the record source returned no read-back")
    read_at = format_time(now or datetime.now(timezone.utc))
    return {
        "schema_version": SCHEMA,
        "salt": salt.hex(),
        "target": _mac(salt, "target", target),
        "first_outcome": str(first_outcome),
        "read_at": read_at,
        "window_s": float(window_s),
        "due_at": due_at(read_at, float(window_s)),
        "fields_mac": _mac(salt, "fields", [seen.found, dict(seen.fields)]),
        "version_mac": None
        if seen.version is None
        else _mac(salt, "version", seen.version),
        "log": checkpoint,
        "own_events": sorted({_mac(salt, "event", i) for i in own_event_ids}),
    }


def schedule(state: Mapping[str, Any]) -> dict[str, Any]:
    """The block a receipt carries: when the recheck is due and the state it
    binds. No salt, no values."""

    return {
        "compares": "api_readback_and_log_since_checkpoint"
        if state.get("log") is not None
        else "api_readback",
        "read_back_at": state["read_at"],
        "recheck_window_s": state["window_s"],
        "due_at": state["due_at"],
        "state_sha256": state_sha256(state),
    }


def state_problem(state: Any) -> Optional[str]:
    """A plain sentence when ``state`` is not a recheck state's shape."""

    if not isinstance(state, Mapping):
        return "the recheck state is not a JSON object"
    if state.get("schema_version") != SCHEMA:
        return f"the recheck state is not an {SCHEMA} document"
    if set(state) != set(STATE_KEYS):
        return "the recheck state must hold exactly " + ", ".join(STATE_KEYS)
    try:
        if len(bytes.fromhex(str(state["salt"]))) != 32:
            return "the recheck state's salt is not 32 bytes"
    except ValueError:
        return "the recheck state's salt is not hex"
    try:
        expected_due = due_at(str(state["read_at"]), float(state["window_s"]))
    except (TypeError, ValueError):
        return "the recheck state's read time or window is malformed"
    if state["due_at"] != expected_due:
        return "the recheck state's due time doesn't follow from its window"
    if state["log"] is not None and not isinstance(state["log"], Mapping):
        return "the recheck state's log checkpoint is not an object"
    if not isinstance(state["own_events"], list):
        return "the recheck state's own events are not a list"
    return None


# -- recheck -------------------------------------------------------------------


def recheck(
    state: Mapping[str, Any],
    record: RecordSource,
    *,
    target: str,
    expected_state_sha256: str,
    log: Optional[ActivityLog] = None,
    excused_event_ids: Sequence[str] = (),
    now: Optional[datetime] = None,
) -> RecheckResult:
    """Read the record and the log again and judge what changed.

    ``expected_state_sha256``: from the receipt (:func:`schedule`), so a
    state that was edited or swapped doesn't match. ``excused_event_ids``:
    log entries of later declared writes by this caller that their own
    checks account for (any outcome); they don't refute this write.
    """

    current = now or datetime.now(timezone.utc)
    at = format_time(current)
    problem = state_problem(state)
    if problem is not None:
        return _not_checked(problem, at=at)
    if not hmac.compare_digest(state_sha256(state), str(expected_state_sha256)):
        return _not_checked("the recheck state doesn't match its receipt", at=at)
    salt = bytes.fromhex(str(state["salt"]))
    if not hmac.compare_digest(_mac(salt, "target", target), str(state["target"])):
        return _not_checked(
            "the recheck state was taken for another record", state, at=at
        )
    if state["first_outcome"] != "VERIFIED":
        return _not_checked(
            "the first check was not VERIFIED; a recheck can't raise it", state, at=at
        )
    if seconds_until(str(state["due_at"]), current) > 0:
        return _not_checked("the recheck is not due yet", state, at=at)
    if (state["log"] is None) != (log is None):
        return _not_checked(
            "the log given doesn't match the log the state was taken with", state, at=at
        )

    # The record first, then the log: an entry between the two reads is in
    # the log read, so a change can't slip between them.
    try:
        seen = record.read()
        if not isinstance(seen, Readback):
            raise ReadFailed("the record source returned no read-back")
    except ReadFailed as exc:
        return _not_checked(f"the record could not be read again: {exc}", state, at=at)
    window: Optional[LogRead] = None
    if log is not None:
        try:
            window = log.since(dict(state["log"]))
        except ReadFailed as exc:
            return _not_checked(f"the log could not be read: {exc}", state, at=at)

    basis = [BASIS_API] + ([BASIS_LOG] if window is not None else [])
    not_covered = list(ALWAYS_NOT_COVERED)
    if window is None:
        not_covered.append("changes reverted between the two reads (no log read)")
    else:
        missing = [op for op in _ALL_OPS if op not in window.logs]
        if missing:
            not_covered.append("log does not record: " + ", ".join(missing))

    def result(outcome: Outcome, reason: str) -> RecheckResult:
        return RecheckResult(
            outcome,
            reason,
            basis=basis,
            read_back_at=str(state["read_at"]),
            rechecked_at=at,
            recheck_window_s=float(state["window_s"]),
            not_covered=not_covered,
            entries_after_checkpoint=len(window.entries) if window else 0,
            other_actor_entries=sum(1 for e in window.entries if e.by == "other")
            if window
            else 0,
        )

    # What the log says, when there is one.
    other_on_target = unknown_on_target = self_on_target = False
    self_elsewhere = unknown_elsewhere = False
    if window is not None:
        own = set(state["own_events"])
        excused = {_mac(salt, "event", i) for i in excused_event_ids}
        for entry in window.entries:
            if entry.kind == "read":
                continue
            h = _mac(salt, "event", entry.id)
            if h in own:
                continue  # the write's own entry, judged by the first check
            if entry.on_target:
                if entry.by == "other":
                    other_on_target = True
                elif (
                    entry.by == "self" and entry.kind != "unknown" and h not in excused
                ):
                    self_on_target = True
                elif h not in excused:
                    unknown_on_target = True
            elif entry.by == "self" and h not in excused:
                if entry.kind == "unknown":
                    unknown_elsewhere = True
                else:
                    self_elsewhere = True
            elif entry.by == "unknown" and h not in excused:
                unknown_elsewhere = True

    fields_now = _mac(salt, "fields", [seen.found, dict(seen.fields)])
    fields_held = hmac.compare_digest(fields_now, str(state["fields_mac"]))
    if not fields_held:
        if other_on_target:
            return result(
                "NOT_CHECKED",
                "the record changed since the first read, and the log attributes "
                "a change to another actor",
            )
        if not seen.found:
            return result("REFUTED", "the record is gone since the first read")
        return result(
            "REFUTED", "the record no longer shows the values the first read showed"
        )
    if self_on_target:
        return result(
            "REFUTED",
            "the log shows another write to the record by this caller that "
            "nothing declared accounts for",
        )
    if self_elsewhere:
        return result(
            "REFUTED",
            "the log shows a change by this caller to another record that "
            "nothing declared accounts for",
        )
    if window is not None and not window.reached:
        return result("NOT_CHECKED", "the log read did not reach the checkpoint")
    if window is not None and window.hidden:
        return result(
            "NOT_CHECKED",
            f"the log returned {window.hidden} entries that could not be inspected",
        )
    if other_on_target:
        return result(
            "NOT_CHECKED",
            "another actor changed the record since the first read; the values "
            "match, but the change was not this write's",
        )
    if unknown_on_target or unknown_elsewhere:
        return result(
            "NOT_CHECKED", "the log holds entries whose author or kind is unknown"
        )
    version_now = None if seen.version is None else _mac(salt, "version", seen.version)
    if version_now != state["version_mac"]:
        return result(
            "NOT_CHECKED",
            "the record's version moved while its declared values stayed the same",
        )
    return result(
        "VERIFIED",
        f"as of {at}, the read-back showed the values the first read showed"
        + (
            ", and the log since the checkpoint shows no unexplained change"
            if window is not None
            else ""
        ),
    )


# -- adapters ------------------------------------------------------------------


def _dotted(record: Any, path: str) -> Any:
    current = record
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


class VerifierReadback:
    """A :class:`RecordSource` over any verifier adapter on main.

    Each read runs ``verifier.verify(effect, before)`` against the pre-write
    baseline, as the first check did. CONFIRMED with exactly one matched
    record gives that record's ``fields`` (default: the effect's match
    keys); REFUTED gives ``found=False``; anything else fails the read.

    ``version_path`` names a server version (``meta.versionId``); without
    it, the version is a digest of the whole matched record minus
    ``ignore`` (volatile fields such as ``meta.lastUpdated`` belong there
    only when the server rewrites them without a change).
    """

    def __init__(
        self,
        verifier: Any,
        effect: Any,
        before: Any,
        *,
        fields: Optional[Sequence[str]] = None,
        version_path: Optional[str] = None,
        ignore: Sequence[str] = (),
        context: Any = None,
    ) -> None:
        self.verifier = verifier
        self.effect = effect
        self.before = before
        self.fields = tuple(fields) if fields is not None else tuple(effect.match)
        self.version_path = version_path
        self.ignore = tuple(ignore)
        self.context = context

    def read(self) -> Readback:
        try:
            verdict = self.verifier.verify(self.effect, self.before, self.context)
        except Exception as exc:  # noqa: BLE001 - any failure is an unread record
            raise ReadFailed(type(exc).__name__) from exc
        status = getattr(getattr(verdict, "verdict", None), "value", None)
        if status == "refuted":
            return Readback(found=False)
        records = list(getattr(verdict, "matched_records", []) or [])
        if status != "confirmed" or len(records) != 1:
            raise ReadFailed(
                getattr(verdict, "reason", "") or "the read was not conclusive"
            )
        rec = records[0]
        if self.version_path:
            raw = _dotted(rec, self.version_path)
            version = None if raw is None else str(raw)
        else:
            trimmed = json.loads(_canonical(rec))
            for path in self.ignore:
                parts = path.split(".")
                holder = (
                    _dotted(trimmed, ".".join(parts[:-1])) if parts[:-1] else trimmed
                )
                if isinstance(holder, dict):
                    holder.pop(parts[-1], None)
            version = hashlib.sha256(_canonical(trimmed)).hexdigest()
        return Readback(
            found=True,
            fields={f: _dotted(rec, f) for f in self.fields},
            version=version,
        )


__all__ = [
    "ALWAYS_NOT_COVERED",
    "ActivityLog",
    "BASIS_API",
    "BASIS_LOG",
    "LogEntry",
    "LogRead",
    "Outcome",
    "ReadFailed",
    "Readback",
    "RecheckResult",
    "RecordSource",
    "SCHEMA",
    "TO_LATE",
    "TO_VERDICT",
    "VerifierReadback",
    "begin",
    "recheck",
    "schedule",
    "state_problem",
    "state_sha256",
]
