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
the record's identity, its declared fields and its version (never the
values), the log's checkpoint, and keyed MACs of the write's own events. The
MAC key is the salt keyed by this host's frame secret (the one
``db_snapshot`` uses), so the state alone can't be tested against guessed
values, and only a host holding that secret can recheck it. The checkpoint is
stored as the log adapter gives it: an adapter must return log positions
(entry ids, a stream cursor), never record values or identifiers. The
receipt carries only ``state_sha256`` and the due time.

``recheck`` runs at or after the due time. It reads the record again, reads
the log from the checkpoint and judges:

- ``VERIFIED``: between the start and the end of the recheck, the read-back
  showed the same record with the same declared values and version. When a
  log was read, the log since the checkpoint also holds no mutation of the
  record and no mutation by this caller that no later declared write
  accounts for. Without a log, the basis is the API read-back alone and the
  result lists this caller's changes to other records as not covered. It
  never means "persisted": a change after the recheck read is not seen.
- ``REFUTED``: the log shows this caller made a mutation nothing accounts
  for (late collateral, or an extra write to the record), or the record no
  longer shows the declared values (or is gone) and nothing in the log
  attributes that change to another or an unknown actor.
- ``NOT_CHECKED``: anything else. The recheck was early; the state is
  malformed or doesn't match the receipt or this host; the first check
  wasn't verified; a read failed or was ambiguous; the read-back shows a
  different record; the log didn't reach the checkpoint; another actor or
  an unattributed entry changed the record; an exempted entry's attribution
  contradicts the write it was exempted for; the version moved while the
  values stayed (A to B to A); the log returned entries it didn't let us
  inspect.

The local clock only decides when the recheck is due. Which log entries are
in the window is decided by the log's own checkpoint and entry ids, never by
comparing clocks: remote server clocks routinely drift relative to the client.

The recheck can only keep or lower the first result. It never turns a first
result that wasn't verified into ``VERIFIED``.

Sources are adapters. :class:`VerifierReadback` wraps a REST, FHIR or GraphQL
verifier on main, so the read-back half works with what exists
today. The log half is the :class:`ActivityLog` protocol; a reader of a
system's audit or event log implements it with a thin wrapper. This module
imports nothing from ``tx`` or ``txplan``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
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
SCHEMA = "openadapt.api-recheck/v2"
#: Exactly these keys: nothing rides along unbound.
STATE_KEYS: tuple[str, ...] = (
    "schema_version",
    "salt",
    "key_check",
    "target",
    "source_mac",
    "log_scope_mac",
    "first_outcome",
    "read_at",
    "window_s",
    "due_at",
    "identity_mac",
    "fields_mac",
    "version_mac",
    "log",
    "own_events",
)
#: The longest recheck window a state may carry (one year).
MAX_WINDOW_S = 366 * 24 * 3600.0

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
#: The verifier substrates whose read goes through the system's API.
API_SUBSTRATES = frozenset({"rest", "fhir", "graphql"})
#: Substrates whose records are a projection: only a named server version
#: can stand for the whole record.
_PROJECTED_SUBSTRATES = frozenset({"fhir", "graphql"})
BASIS_LOG = "activity_log"
#: Always listed: a read of the record and the log can't see these.
ALWAYS_NOT_COVERED: tuple[str, ...] = (
    "server-side async and outbound effects not observed",
    "changes after the recheck read",
)
#: Listed when no log (or a log that records nothing) was read.
NO_LOG_NOT_COVERED: tuple[str, ...] = (
    "changes reverted between the two reads (no log read)",
    "this caller's changes to other records (no log read)",
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

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class ReadFailed(RuntimeError):
    """A source could not be read. Nothing is known about it.

    Messages are fixed sentences: they never carry a URL, a record value or
    an upstream error text, because a recheck result is serialized."""


# -- sources -------------------------------------------------------------------


@dataclass(frozen=True)
class Readback:
    """One read of the record through the system's API.

    ``identity``: the record's own id as the read showed it (required when
    ``found``): a read that lands on another record is never a match.
    ``fields``: the declared fields as read (judged: a change refutes).
    ``version``: anything that moves whenever the record changes (an ETag,
    FHIR ``meta.versionId``, a digest of the whole record), compared with
    its JSON type. A moved version with unchanged fields is a change the
    fields don't show: never VERIFIED.
    ``found``: False when the system answered that no record matches.

    Values must be JSON values (``None``, ``bool``, ``int``, finite
    ``float``, ``str``, lists and string-keyed objects of these); anything
    else fails the read rather than being compared through ``str()``.
    """

    found: bool
    fields: Mapping[str, Any] = field(default_factory=dict)
    version: Any = None
    identity: Any = None


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
    have touched the record, so it blocks VERIFIED and keeps a changed
    value from being attributed.
    ``logs``: the operations this log records. A kind it doesn't record
    can't be seen, and the result lists it as not covered. A log that
    records nothing gives no ``activity_log`` basis.
    """

    entries: Sequence[LogEntry]
    reached: bool
    hidden: int = 0
    logs: tuple[Op, ...] = _ALL_OPS


class ActivityLog(Protocol):
    """A system's audit or event log, read by its own positions."""

    def checkpoint(self) -> Mapping[str, Any]:
        """An opaque, JSON-safe position: the newest entry ids or a stream
        cursor, never record values or identifiers (the private state keeps
        it as given). Raises :class:`ReadFailed` when the log can't be
        placed."""
        ...

    def since(self, checkpoint: Mapping[str, Any]) -> LogRead:
        """Every entry after ``checkpoint``, newest first or in log order.
        Raises :class:`ReadFailed` when the log can't be read."""
        ...


# -- digests -------------------------------------------------------------------


def _json_value(value: Any) -> None:
    """Raise ``TypeError`` unless ``value`` is a plain JSON value."""

    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("a non-finite number has no exact JSON form")
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _json_value(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("object keys must be strings")
            _json_value(item)
        return
    raise TypeError(f"{type(value).__name__} is not a JSON value")


def _canonical(value: Any) -> bytes:
    """One exact text per JSON value: ``1`` and ``"1"`` differ, and a type
    without an exact JSON form raises ``TypeError``."""

    _json_value(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _key(salt: bytes) -> bytes:
    """The state's MAC key: the salt keyed by this host's frame secret."""

    from openadapt_flow.runtime.effects.db_snapshot import mac_key

    return mac_key(salt)


def _mac(key: bytes, label: str, value: Any) -> str:
    return hmac.new(
        key, label.encode() + b"\0" + _canonical(value), hashlib.sha256
    ).hexdigest()


def _fields_mac(key: bytes, seen: Readback) -> str:
    return _mac(key, "fields", [seen.found, dict(seen.fields)])


def _identity_mac(key: bytes, seen: Readback) -> str:
    return _mac(key, "identity", seen.identity)


def _version_mac(key: bytes, seen: Readback) -> Optional[str]:
    return None if seen.version is None else _mac(key, "version", seen.version)


def _event_mac(key: bytes, event_id: str, kind: str) -> str:
    return _mac(key, "event", [event_id, kind])


def state_sha256(state: Mapping[str, Any]) -> str:
    """The digest a receipt binds: the whole private state, salt included."""

    return "sha256:" + hashlib.sha256(_canonical(dict(state))).hexdigest()


def _events(events: Any) -> dict[str, Op]:
    """``{entry id: operation}``; a bare string is refused, since its
    characters would read as separate ids."""

    if isinstance(events, (str, bytes)) or not isinstance(events, Mapping):
        raise ValueError("events must be a mapping of entry id to operation")
    out: dict[str, Op] = {}
    for event_id, kind in events.items():
        if not isinstance(event_id, str) or not event_id or kind not in _ALL_OPS:
            raise ValueError("each event is a non-empty id and create/update/delete")
        out[event_id] = kind
    return out


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
    #: When the recheck's reads started; ``rechecked_at`` is when they ended.
    observed_from: Optional[str] = None

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
            "observed_between": [self.observed_from, self.rechecked_at],
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
    """``state`` only once :func:`state_problem` has passed it."""

    basis: list[str] = []
    if state is not None:
        basis = [BASIS_API] + ([BASIS_LOG] if state["log"] is not None else [])
    return RecheckResult(
        "NOT_CHECKED",
        reason,
        basis=basis,
        read_back_at=str(state["read_at"]) if state is not None else None,
        rechecked_at=at,
        recheck_window_s=float(state["window_s"]) if state is not None else None,
        not_covered=list(ALWAYS_NOT_COVERED),
        observed_from=at,
    )


# -- begin ---------------------------------------------------------------------


def _window_ok(window_s: Any) -> bool:
    return (
        not isinstance(window_s, bool)
        and isinstance(window_s, (int, float))
        and math.isfinite(window_s)
        and 0 < window_s <= MAX_WINDOW_S
    )


def _source_binding(obj: Any, attr: str) -> Any:
    raw = getattr(obj, attr, None)
    if callable(raw):
        return raw()
    return raw


def _nonempty_str(val: Any) -> bool:
    return isinstance(val, str) and bool(val.strip())


def _valid_source_scope(scope: Any) -> bool:
    return isinstance(scope, Mapping) and any(
        _nonempty_str(scope.get(k)) for k in ("account", "tenant", "principal")
    )


def _valid_log_scope(scope: Any) -> bool:
    return (
        isinstance(scope, Mapping)
        and _nonempty_str(scope.get("stream"))
        and _nonempty_str(scope.get("actor"))
    )


def begin(
    record: RecordSource,
    *,
    target: str,
    first_outcome: str,
    window_s: float,
    log: Optional[ActivityLog] = None,
    own_events: Mapping[str, Op] | None = None,
    first_identity: Any = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The private recheck state, taken right after the first check.

    ``target``: the record's reference (``Patient/123``); bound, never
    re-read from the state. ``first_outcome``: the first check's outcome; a
    recheck of anything but ``VERIFIED`` stays ``NOT_CHECKED``.
    ``first_identity``: record id observed by the first check; required when
    ``first_outcome == "VERIFIED"``, and the baseline read must land on the
    same record id.
    ``own_events``: ``{entry id: operation}`` for the write's own log
    entries, which the first check already judged; they may land after the
    checkpoint while the log lags. They are exempt only when the log
    attributes them to this caller with that operation.

    The log checkpoint is taken before the read, so an entry between the two
    reads shows up in the window rather than in the baseline. Raises
    :class:`ReadFailed` when either source can't be read, lacks a complete
    account/source or stream-and-actor binding, lacks ``first_identity`` for a
    ``VERIFIED`` first check, or the record is not there (a baseline of
    "absent" would verify a deletion), and ``ValueError`` for a window that
    isn't a positive number of seconds up to :data:`MAX_WINDOW_S` or for
    malformed events.
    """

    if not _window_ok(window_s):
        raise ValueError("the recheck window must be a positive number of seconds")
    own = _events({} if own_events is None else own_events)
    if str(first_outcome) == "VERIFIED" and first_identity is None:
        raise ReadFailed(
            "the first check's observed record identity is required for a VERIFIED recheck"
        )
    source_scope = _source_binding(record, "source_scope")
    if not _valid_source_scope(source_scope):
        raise ReadFailed("the record source carries no account/tenant source binding")
    log_scope = None if log is None else _source_binding(log, "stream_scope")
    if log is not None and not _valid_log_scope(log_scope):
        raise ReadFailed("the log source must bind both a non-empty stream and actor")
    salt = secrets.token_bytes(32)
    key = _key(salt)
    checkpoint = None if log is None else dict(log.checkpoint())
    seen = record.read()
    if not isinstance(seen, Readback):
        raise ReadFailed("the record source returned no read-back")
    if not seen.found:
        raise ReadFailed("the record is not there; there is nothing to recheck")
    if seen.identity is None:
        raise ReadFailed("the read-back did not show the record's identity")
    try:
        source_mac = _mac(key, "source", _canonical(source_scope).decode("ascii"))
        log_scope_mac = (
            None
            if log_scope is None
            else _mac(key, "log-scope", _canonical(log_scope).decode("ascii"))
        )
        if first_identity is not None and _canonical(seen.identity) != _canonical(
            first_identity
        ):
            raise ReadFailed(
                "the baseline read did not match the first check's observed record"
            )
        if checkpoint is not None:
            _canonical(checkpoint)
        fields_mac = _fields_mac(key, seen)
        identity_mac = _identity_mac(key, seen)
        version_mac = _version_mac(key, seen)
    except (TypeError, ValueError):
        raise ReadFailed("a read returned a value with no exact JSON form") from None
    read_at = format_time(now or datetime.now(timezone.utc))
    return {
        "schema_version": SCHEMA,
        "salt": salt.hex(),
        "key_check": _mac(key, "key-check", SCHEMA),
        "target": _mac(key, "target", str(target)),
        "source_mac": source_mac,
        "log_scope_mac": log_scope_mac,
        "first_outcome": str(first_outcome),
        "read_at": read_at,
        "window_s": float(window_s),
        "due_at": due_at(read_at, float(window_s)),
        "identity_mac": identity_mac,
        "fields_mac": fields_mac,
        "version_mac": version_mac,
        "log": checkpoint,
        "own_events": sorted(_event_mac(key, i, k) for i, k in own.items()),
    }


def schedule(state: Mapping[str, Any]) -> dict[str, Any]:
    """The block a receipt carries: when the recheck is due and the state it
    binds. No salt, no values. Raises ``ValueError`` for a malformed state."""

    problem = state_problem(state)
    if problem is not None:
        raise ValueError(problem)
    return {
        "compares": "api_readback_and_log_since_checkpoint"
        if state["log"] is not None
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
    for name in (
        "salt",
        "key_check",
        "target",
        "source_mac",
        "identity_mac",
        "fields_mac",
    ):
        if not isinstance(state[name], str) or not _HEX64.match(state[name]):
            return f"the recheck state's {name} is not 64 hex digits"
    log_scope_mac = state["log_scope_mac"]
    if state["log"] is None:
        if log_scope_mac is not None:
            return "the recheck state carries a log_scope_mac without a log checkpoint"
    elif not isinstance(log_scope_mac, str) or not _HEX64.match(log_scope_mac):
        return "the recheck state's log_scope_mac is not 64 hex digits"
    version = state["version_mac"]
    if version is not None and (
        not isinstance(version, str) or not _HEX64.match(version)
    ):
        return "the recheck state's version_mac is not 64 hex digits"
    if not isinstance(state["first_outcome"], str):
        return "the recheck state's first outcome is not text"
    if not isinstance(state["read_at"], str) or not _TIME.match(state["read_at"]):
        return "the recheck state's read time is malformed"
    if not _window_ok(state["window_s"]):
        return "the recheck state's window is out of range"
    if not isinstance(state["due_at"], str) or not _TIME.match(state["due_at"]):
        return "the recheck state's due time is malformed"
    try:
        expected_due = due_at(state["read_at"], float(state["window_s"]))
    except (TypeError, ValueError, OverflowError):
        return "the recheck state's read time or window is malformed"
    if state["due_at"] != expected_due:
        return "the recheck state's due time doesn't follow from its window"
    if state["log"] is not None:
        if not isinstance(state["log"], Mapping):
            return "the recheck state's log checkpoint is not an object"
        try:
            _canonical(state["log"])
        except (TypeError, ValueError):
            return "the recheck state's log checkpoint is not plain JSON"
    events = state["own_events"]
    if not isinstance(events, list) or not all(
        isinstance(e, str) and _HEX64.match(e) for e in events
    ):
        return "the recheck state's own events are not a list of digests"
    return None


# -- recheck -------------------------------------------------------------------


def recheck(
    state: Mapping[str, Any],
    record: RecordSource,
    *,
    target: str,
    expected_state_sha256: str,
    log: Optional[ActivityLog] = None,
    excused_events: Mapping[str, Op] | None = None,
    now: Optional[datetime] = None,
) -> RecheckResult:
    """Read the record and the log again and judge what changed.

    ``expected_state_sha256``: from the receipt (:func:`schedule`), so a
    state that was edited or swapped doesn't match. ``excused_events``:
    ``{entry id: operation}`` for log entries of later declared writes by
    this caller that their own checks account for (any outcome); they don't
    refute this write when the log attributes them to this caller with that
    operation. An exempted entry the log attributes otherwise makes the
    recheck ``NOT_CHECKED``.

    ``now`` fixes both ends of the observation (for tests); without it the
    start is taken before the reads and the end after them.
    """

    start = now or datetime.now(timezone.utc)
    started = format_time(start)
    problem = state_problem(state)
    if problem is not None:
        return _not_checked(problem, at=started)
    if not hmac.compare_digest(state_sha256(state), str(expected_state_sha256)):
        return _not_checked("the recheck state doesn't match its receipt", at=started)
    salt = bytes.fromhex(state["salt"])
    try:
        key = _key(salt)
    except Exception:  # noqa: BLE001 - no host secret: nothing can be compared
        return _not_checked("this host's frame secret could not be read", at=started)
    if not hmac.compare_digest(_mac(key, "key-check", SCHEMA), state["key_check"]):
        return _not_checked(
            "the recheck state was taken on a host with another frame secret",
            at=started,
        )
    if not hmac.compare_digest(_mac(key, "target", str(target)), state["target"]):
        return _not_checked(
            "the recheck state was taken for another record", state, at=started
        )
    if state["first_outcome"] != "VERIFIED":
        return _not_checked(
            "the first check was not VERIFIED; a recheck can't raise it",
            state,
            at=started,
        )
    if seconds_until(state["due_at"], start) > 0:
        return _not_checked("the recheck is not due yet", state, at=started)
    if (state["log"] is None) != (log is None):
        return _not_checked(
            "the log given doesn't match the log the state was taken with",
            state,
            at=started,
        )
    source_scope = _source_binding(record, "source_scope")
    if not _valid_source_scope(source_scope):
        return _not_checked(
            "the record source carries no account/tenant source binding",
            state,
            at=started,
        )
    try:
        source_mac = _mac(key, "source", _canonical(source_scope).decode("ascii"))
    except (TypeError, ValueError):
        return _not_checked(
            "the record source binding has no exact JSON form", state, at=started
        )
    if not hmac.compare_digest(source_mac, state["source_mac"]):
        return _not_checked(
            "the record source does not match the source the state was taken with",
            state,
            at=started,
        )
    if log is not None:
        log_scope = _source_binding(log, "stream_scope")
        if not _valid_log_scope(log_scope):
            return _not_checked(
                "the log source must bind both a non-empty stream and actor",
                state,
                at=started,
            )
        try:
            log_scope_mac = _mac(
                key, "log-scope", _canonical(log_scope).decode("ascii")
            )
        except (TypeError, ValueError):
            return _not_checked(
                "the log stream binding has no exact JSON form", state, at=started
            )
        if state["log_scope_mac"] is None or not hmac.compare_digest(
            log_scope_mac, state["log_scope_mac"]
        ):
            return _not_checked(
                "the log stream or actor does not match the log the state was taken with",
                state,
                at=started,
            )
    try:
        excused = {
            _event_mac(key, i, k)
            for i, k in _events(
                {} if excused_events is None else excused_events
            ).items()
        }
    except ValueError:
        return _not_checked("the excused events are malformed", state, at=started)

    # The record first, then the log: an entry between the two reads is in
    # the log read, so a change can't slip between them.
    try:
        seen = record.read()
        if not isinstance(seen, Readback):
            raise ReadFailed("the record source returned no read-back")
        fields_now = _fields_mac(key, seen)
        identity_now = _identity_mac(key, seen) if seen.found else None
        version_now = _version_mac(key, seen)
    except (ReadFailed, TypeError, ValueError):
        return _not_checked("the record could not be read again", state, at=started)
    window: Optional[LogRead] = None
    if log is not None:
        try:
            window = log.since(dict(state["log"]))
            if not isinstance(window, LogRead) or any(
                op not in _ALL_OPS for op in window.logs
            ):
                raise ReadFailed("the log returned no log read")
        except ReadFailed:
            return _not_checked("the log could not be read", state, at=started)
    at = format_time(now or datetime.now(timezone.utc))

    log_covers = window is not None and bool(window.logs)
    basis = [BASIS_API] + ([BASIS_LOG] if log_covers else [])
    not_covered = list(ALWAYS_NOT_COVERED)
    if not log_covers:
        not_covered.extend(NO_LOG_NOT_COVERED)
    if window is not None:
        missing = [op for op in _ALL_OPS if op not in window.logs]
        if missing:
            not_covered.append("log does not record: " + ", ".join(missing))

    def result(outcome: Outcome, reason: str) -> RecheckResult:
        return RecheckResult(
            outcome,
            reason,
            basis=basis,
            read_back_at=state["read_at"],
            rechecked_at=at,
            recheck_window_s=float(state["window_s"]),
            not_covered=not_covered,
            entries_after_checkpoint=len(window.entries) if window else 0,
            other_actor_entries=sum(1 for e in window.entries if e.by == "other")
            if window
            else 0,
            observed_from=started,
        )

    # What the log says, when there is one.
    other_on_target = unknown_on_target = self_on_target = False
    self_elsewhere = unknown_elsewhere = contradicted = False
    if window is not None:
        own = set(state["own_events"])
        for entry in window.entries:
            kind = entry.kind if entry.kind in _ALL_OPS else None
            if entry.kind == "read":
                continue
            by = entry.by if entry.by in ("self", "other") else "unknown"
            exempt = kind is not None and _event_mac(key, str(entry.id), kind) in (
                own | excused
            )
            if exempt and by == "self":
                continue  # this caller's declared write, judged by its own check
            if exempt or any(
                _event_mac(key, str(entry.id), op) in (own | excused) for op in _ALL_OPS
            ):
                # An exempted id whose author or operation disagrees with the
                # write it was exempted for.
                contradicted = True
                continue
            if entry.on_target is True:
                if by == "other":
                    other_on_target = True
                elif by == "self" and kind is not None:
                    self_on_target = True
                else:
                    unknown_on_target = True
            elif entry.on_target is not False:
                unknown_on_target = True
            elif by == "self":
                if kind is None:
                    unknown_elsewhere = True
                else:
                    self_elsewhere = True
            elif by == "unknown":
                unknown_elsewhere = True

    # Mutations the log proves this caller made come first: another
    # actor's change to the record doesn't explain them.
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
    if seen.found and not hmac.compare_digest(str(identity_now), state["identity_mac"]):
        return result(
            "NOT_CHECKED",
            "the read-back now lands on a different record than the first read",
        )
    if window is not None and not window.reached:
        return result("NOT_CHECKED", "the log read did not reach the checkpoint")
    if not hmac.compare_digest(fields_now, state["fields_mac"]):
        if (
            other_on_target
            or unknown_on_target
            or contradicted
            or (window is not None and window.hidden)
        ):
            return result(
                "NOT_CHECKED",
                "the record changed since the first read, and the log does not "
                "attribute the change to this caller",
            )
        if not seen.found:
            return result(
                "REFUTED",
                "no single record shows the declared values since the first read",
            )
        return result(
            "REFUTED", "the record no longer shows the values the first read showed"
        )
    if contradicted:
        return result(
            "NOT_CHECKED",
            "an exempted log entry is attributed to another author or operation",
        )
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
    if version_now != state["version_mac"]:
        return result(
            "NOT_CHECKED",
            "the record's version moved while its declared values stayed the same",
        )
    return result(
        "VERIFIED",
        f"read between {started} and {at}, the read-back showed the record and "
        "values the first read showed"
        + (
            ", and the log since the checkpoint shows no unexplained change"
            if log_covers
            else "; no log was read, so this caller's other changes are not covered"
        ),
    )


# -- adapters ------------------------------------------------------------------

_MISSING = object()


def _dotted(record: Any, path: str) -> Any:
    current = record
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _redacted(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith("[redacted")
    if isinstance(value, Mapping):
        return any(_redacted(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_redacted(v) for v in value)
    return False


class VerifierReadback:
    """A :class:`RecordSource` over a REST, FHIR or GraphQL verifier on main.

    Only an independent, system-of-record verifier is accepted: a screen
    read-back or a database read is not an API read-back, so it can't carry
    the ``system_api`` basis. Rebuilding this source at recheck time needs
    the pre-write ``EffectState``, which holds the records as read; only the
    recheck state is free of values.

    Each read runs ``verifier.verify(effect, before)`` against the pre-write
    baseline, as the first check did. CONFIRMED with exactly one matched
    record gives that record's ``identity_path`` (default ``id``) and
    ``fields`` (default: the effect's match keys); REFUTED with no matched
    record gives ``found=False``; anything else (an ambiguous match, a
    refutation that names records, a missing declared field, a redacted
    value) fails the read.

    ``version_path`` names a server version (``meta.versionId``, or the key
    a FHIR ``field_paths`` maps it to). FHIR and GraphQL records are
    projections, so they need one. A REST verifier that drops no fields may
    leave it out: the version is then a digest of the whole matched record
    minus ``ignore`` (volatile fields such as ``meta.lastUpdated`` belong
    there only when the server rewrites them without a change). A verifier
    whose redaction policy covers a compared field, or any field when the
    version is a whole-record digest, is refused: redacted evidence can't
    tell two values apart.
    """

    def __init__(
        self,
        verifier: Any,
        effect: Any,
        before: Any,
        *,
        fields: Optional[Sequence[str]] = None,
        version_path: Optional[str] = None,
        identity_path: str = "id",
        ignore: Sequence[str] = (),
        context: Any = None,
    ) -> None:
        self.verifier = verifier
        self.effect = effect
        self.before = before
        self.fields = tuple(fields) if fields is not None else tuple(effect.match)
        self.version_path = version_path
        self.identity_path = identity_path
        self.ignore = tuple(ignore)
        self.context = context
        substrate = str(getattr(verifier, "substrate", ""))
        if substrate not in API_SUBSTRATES:
            raise ValueError(
                f"a {substrate or 'unnamed'} verifier is not an API read-back"
            )
        tier_of = getattr(verifier, "verification_tier_for", None)
        tier = tier_of(effect) if callable(tier_of) else verifier.verification_tier
        if (
            getattr(verifier, "independent_system_of_record", False) is not True
            or int(tier) != 1
        ):
            raise ValueError("the verifier does not read the system of record")
        self._substrate = substrate
        self._projected = substrate in _PROJECTED_SUBSTRATES
        if version_path is None and (
            self._projected or tuple(getattr(verifier, "drop_fields", ()))
        ):
            raise ValueError(
                "a projected read needs a named server version (version_path)"
            )
        compared = [*self.fields, identity_path]
        if version_path is not None:
            compared.append(version_path)
        for policy in self._redaction_policies(verifier):
            if version_path is None or any(
                policy.is_redacted(path) or policy.is_redacted(path.split(".")[0])
                for path in compared
            ):
                raise ValueError("the verifier redacts evidence the recheck compares")

    @staticmethod
    def _redaction_policies(verifier: Any) -> list[Any]:
        policies: list[Any] = []
        seen: set[int] = set()
        cur: Any = verifier
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            for attr in ("redaction", "_policy"):
                pol = cur.__dict__.get(attr) if hasattr(cur, "__dict__") else None
                if pol is None:
                    pol = getattr(cur, attr, None)
                if (
                    pol is not None
                    and hasattr(pol, "is_redacted")
                    and pol not in policies
                ):
                    policies.append(pol)
            cur = getattr(cur, "_inner", None)
        return policies

    def source_scope(self) -> Optional[dict[str, Any]]:
        """Identify the underlying API endpoint, account/tenant principal, and query.

        Returns ``None`` when the actual reader cannot establish both its
        endpoint and a stable, non-secret tenant or principal identifier
        (``account_scope``, ``tenant_id``, ``principal_id``, ``account_id``, or
        ``token_source.client_id``). Secret tokens and auth headers are never
        included.
        """
        cur: Any = self.verifier
        seen: set[int] = set()
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            inner = getattr(cur, "_inner", None)
            if inner is None:
                break
            cur = inner
        base = (
            getattr(cur, "base_url", None)
            or getattr(cur, "endpoint", None)
            or getattr(cur, "url", None)
        )
        if not isinstance(base, str) or not base.strip():
            return None
        account: Optional[str] = None
        auth_server: Optional[str] = None
        for attr in ("account_scope", "tenant_id", "principal_id", "account_id"):
            cand = getattr(cur, attr, None)
            if isinstance(cand, str) and cand.strip():
                account = cand.strip()
                break
        if account is None:
            token_src = getattr(cur, "token_source", None)
            if token_src is not None:
                for attr in ("account_scope", "tenant_id", "principal_id"):
                    cand = getattr(token_src, attr, None)
                    if isinstance(cand, str) and cand.strip():
                        account = cand.strip()
                        break
                if account is None:
                    cid = getattr(token_src, "client_id", None)
                    if isinstance(cid, str) and cid.strip():
                        for srv_attr in (
                            "token_url",
                            "_token_url",
                            "token_endpoint",
                            "issuer",
                        ):
                            srv = getattr(token_src, srv_attr, None)
                            if isinstance(srv, str) and srv.strip():
                                auth_server = srv.strip().rstrip("/")
                                break
                        if auth_server is None:
                            return None
                        account = cid.strip()
        if account is None:
            return None
        scope: dict[str, Any] = {
            "substrate": self._substrate,
            "endpoint": base.rstrip("/"),
            "account": account,
            "identity_path": self.identity_path,
        }
        if auth_server is not None:
            scope["auth_server"] = auth_server
        for attr in (
            "records_path",
            "records_key",
            "resource_type",
            "search_params",
            "query",
            "variables",
        ):
            val = getattr(cur, attr, None)
            if val is not None:
                scope[attr] = val
        return scope

    def read(self) -> Readback:
        try:
            verdict = self.verifier.verify(self.effect, self.before, self.context)
        except Exception:  # noqa: BLE001 - any failure is an unread record
            raise ReadFailed("the verifier could not read the record") from None
        status = getattr(getattr(verdict, "verdict", None), "value", None)
        records = list(getattr(verdict, "matched_records", []) or [])
        if status == "refuted" and not records:
            return Readback(found=False)
        if status != "confirmed" or len(records) != 1:
            raise ReadFailed("the read did not identify exactly one record")
        rec = records[0]
        identity = _dotted(rec, self.identity_path)
        if identity is _MISSING or identity is None:
            raise ReadFailed("the record carries no identity")
        fields = {f: _dotted(rec, f) for f in self.fields}
        if any(
            v is _MISSING or (self._projected and v is None) for v in fields.values()
        ):
            raise ReadFailed("the record lacks a declared field")
        if self.version_path:
            version = _dotted(rec, self.version_path)
            if version is _MISSING or version is None:
                # A named version that isn't there would silently drop the
                # check that catches A to B to A.
                raise ReadFailed("the record has no named version")
        else:
            try:
                trimmed = json.loads(_canonical(rec))
            except (TypeError, ValueError):
                raise ReadFailed("the record has a value with no exact form") from None
            for path in self.ignore:
                parts = path.split(".")
                holder = (
                    _dotted(trimmed, ".".join(parts[:-1])) if parts[:-1] else trimmed
                )
                if isinstance(holder, dict):
                    holder.pop(parts[-1], None)
            if _redacted(trimmed):
                raise ReadFailed("the read-back carries redacted values")
            version = hashlib.sha256(_canonical(trimmed)).hexdigest()
        if _redacted([identity, fields, version]):
            raise ReadFailed("the read-back carries redacted values")
        return Readback(found=True, fields=fields, version=version, identity=identity)


__all__ = [
    "ALWAYS_NOT_COVERED",
    "ActivityLog",
    "API_SUBSTRATES",
    "BASIS_API",
    "BASIS_LOG",
    "LogEntry",
    "LogRead",
    "MAX_WINDOW_S",
    "NO_LOG_NOT_COVERED",
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
