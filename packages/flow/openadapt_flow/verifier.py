"""Check one write against the system of record: the one public verifier API.

    from openadapt_flow.verifier import Actor, Contract, begin_write, verify_write

    contract = Contract.load("verify.yaml")
    pending = begin_write(contract, params, actor=Actor("agent", "intake-bot"))
    ...                                      # the agent, skill or program writes
    result = verify_write(pending)
    result.outcome                           # CONFIRMED, REFUTED or UNCERTAIN
    result.scope                             # what the result establishes

Every adapter, the MCP verify tools and ``openadapt verify`` call this
module. It wraps ``verify_run`` and checks exactly what verify-run checks:

- **delta:** the declared records were written with the declared values;
- **frame:** nothing else changed, except what the audit trail attributes to
  another user and the contract's reviewed bookkeeping tables;
- **source** (when a :class:`Source` is given): the saved values match the
  document they came from.

:func:`begin_write` consults the uncertain-write ledger
(:mod:`openadapt_flow.write_ledger`) before anything is read or sent, so a
restart or a second worker can't resend a write whose outcome is unknown; it
raises a :class:`WriteBlocked` when the write must not be sent.
:func:`verify_write` records each outcome there. A failed check never raises.
No model calls. See ``docs/VERIFIER_API.md``.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional, Union

from openadapt_flow.runtime.effects.db_snapshot import Snapshot as DatabaseSnapshot
from openadapt_flow.runtime.effects.db_snapshot import TableDiff, diff, take_snapshot
from openadapt_flow.verify_run import (
    FRAME_DB_DETAIL,
    VerifyConfig,
    VerifyRunError,
    _write_private,
    capture_baseline,
    parse_verify_config,
    receipt_sha256,
    verify_run,
    write_result,
)
from openadapt_flow.write_ledger import (
    Attempt,
    FileLedger,
    Ledger,
    LedgerBackend,
    LedgerError,
    MemoryLedger,
    default_ledger,
)

__all__ = [
    "Actor",
    "AlreadyWritten",
    "BaselineUnreadable",
    "Contract",
    "DatabaseSnapshot",
    "FileLedger",
    "Ledger",
    "LedgerBackend",
    "LedgerError",
    "MemoryLedger",
    "Outcome",
    "PendingWrite",
    "Scope",
    "Source",
    "TableDiff",
    "VerifierError",
    "Window",
    "WriteBlocked",
    "WriteHeld",
    "WriteResult",
    "begin_write",
    "capture_database",
    "diff_database",
    "reconcile",
    "verified_write",
    "verify_write",
]

DEFAULT_WORKDIR = "~/.openadapt/verify"
_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")

#: The receipt's executor kinds, with the names callers also use.
_KINDS = {
    "program": "flow_replay",
    "flow_replay": "flow_replay",
    "agent": "agent",
    "script": "script",
    "person": "human",
    "human": "human",
}

#: Each verifier substrate's read path.
READ_PATHS = {
    "sql": "database",
    "database": "database",
    "rest": "api",
    "graphql": "api",
    "fhir": "fhir",
    "onscreen": "screen",
    "ui_readback": "screen",
    "fresh_session": "screen",
    "desktop_readback": "screen",
    "file": "file",
    "fs": "file",
    "document": "file",
    "email": "email",
}


class Outcome(str, enum.Enum):
    """Does the system of record show the declared write, and only it?"""

    #: Every declared effect was read back from the system of record, and
    #: nothing else in the read set moved that the frame doesn't excuse.
    CONFIRMED = "CONFIRMED"
    #: The store shows something other than the declared write: a wrong
    #: value, another record, a duplicate, a change outside it. Never resend.
    REFUTED = "REFUTED"
    #: The store doesn't show the write, couldn't be read, or was read too
    #: weakly to certify it. It may still land: reconcile before any resend.
    UNCERTAIN = "UNCERTAIN"


class VerifierError(ValueError):
    """The contract can't be loaded or bound, or a call is misused."""


class WriteBlocked(VerifierError):
    """Don't send the write. ``reason`` says why, in a plain sentence."""

    def __init__(self, reason: str, *, key: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.key = key


class BaselineUnreadable(WriteBlocked):
    """The system of record can't be read before the write, so nothing
    could verify it afterwards."""


class WriteHeld(WriteBlocked):
    """An earlier attempt of this write is in flight in another worker,
    ``UNCERTAIN``, or ``REFUTED`` and not yet released by a person."""


class AlreadyWritten(WriteBlocked):
    """This write was already made and checked; its reply never reached the
    caller. ``result`` is that earlier reply. Nothing is sent again."""

    def __init__(self, reason: str, *, key: str, result: Mapping[str, Any]) -> None:
        super().__init__(reason, key=key)
        self.result = dict(result)


@dataclass(frozen=True)
class Actor:
    """Who writes: ``program`` (a compiled replay), ``agent``, ``script`` or
    ``person``, and an id. Its own changes are never excused as another
    user's. ``build`` (strings only) is digested into the receipt."""

    kind: str
    id: str
    build: Optional[Mapping[str, str]] = None

    def run_record(self) -> dict[str, Any]:
        kind = _KINDS.get(self.kind)
        if kind is None:
            raise VerifierError(
                f"actor kind {self.kind!r} is not one of " + ", ".join(sorted(_KINDS))
            )
        record: dict[str, Any] = {"executor_kind": kind, "executor_id": self.id}
        if self.build is not None:
            record["executor_build"] = dict(self.build)
        return record


class Contract:
    """A parsed ``verify.yaml`` (``openadapt.verify/v1``).

    ``writes`` (optional, ``"METHOD /path"``) are the requests a taught
    program under this contract may send; any other write is stopped before
    it is sent.
    """

    def __init__(
        self, config: VerifyConfig, *, name: str = "", writes: Sequence[str] = ()
    ) -> None:
        self.config = config
        self.name = name
        self.writes = tuple(_parse_write(w) for w in writes)

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Contract":
        import yaml

        path = Path(path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise VerifierError(f"{path.name}: {exc}") from exc
        if not isinstance(data, Mapping):
            raise VerifierError(f"{path.name}: a contract is a YAML mapping")
        contract = cls.from_dict(data, name=path.stem)
        contract.config._base_dir = path.resolve().parent
        return contract

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, name: str = "") -> "Contract":
        data = dict(data)
        writes = data.pop("writes", None) or ()
        if isinstance(writes, str) or not isinstance(writes, (list, tuple)):
            raise VerifierError('writes is a list of "METHOD /path" strings')
        try:
            config = parse_verify_config(data, source=name or "contract")
        except VerifyRunError as exc:
            raise VerifierError(str(exc)) from exc
        return cls(config, name=name, writes=[str(w) for w in writes])

    @property
    def digest(self) -> str:
        """SHA-256 of the contract as parsed: part of a write's identity."""

        body = json.dumps(
            {
                "config": self.config.model_dump(mode="json"),
                "writes": list(self.writes),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @property
    def params(self) -> list[str]:
        """Every ``{"param": name}`` the contract's reads and effects bind."""

        found: set[str] = set()

        def walk(value: Any) -> None:
            if isinstance(value, Mapping):
                name = value.get("param")
                if isinstance(name, str) and name:
                    found.add(name)
                for item in value.values():
                    walk(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    walk(item)

        walk(self.config.model_dump(mode="json"))
        return sorted(found)


def _parse_write(text: str) -> str:
    method, _, path = text.strip().partition(" ")
    method = method.upper()
    path = path.strip()
    if method not in ("POST", "PUT", "PATCH", "DELETE") or not path.startswith("/"):
        raise VerifierError(
            f'writes entry {text!r}: use "METHOD /path" (POST, PUT, PATCH or DELETE)'
        )
    return f"{method} {path}"


@dataclass(frozen=True)
class Source:
    """Where the written values came from: provenance and the documents."""

    provenance: Any
    documents: Mapping[str, Any]


@dataclass(frozen=True)
class Window:
    """When the store was read: before the write, and after it."""

    baseline_read_at: Optional[str]
    read_at: Optional[str]
    max_execution_window_s: Optional[int]


@dataclass(frozen=True)
class Scope:
    """What a result establishes, and what it says nothing about."""

    #: ``database``, ``api``, ``fhir``, ``audit_log``, ``screen``, ``file``.
    read_paths: tuple[str, ...]
    #: Each store that was read: the verifier's substrates, ``frame.database``
    #: and ``audit``.
    stores: tuple[str, ...]
    #: Stores the contract names that couldn't be read this time.
    unread: tuple[str, ...]
    window: Window
    #: ``independent_system_of_record``, ``independent_session`` or
    #: ``unchecked``.
    strength: str
    #: Plain sentences: what the result does not cover.
    not_covered: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "read_paths": list(self.read_paths),
            "stores": list(self.stores),
            "unread": list(self.unread),
            "window": {
                "baseline_read_at": self.window.baseline_read_at,
                "read_at": self.window.read_at,
                "max_execution_window_s": self.window.max_execution_window_s,
            },
            "strength": self.strength,
            "not_covered": list(self.not_covered),
        }


#: The old names, for ``to_dict()``'s ``verdict`` and ``openadapt_flow.verify``.
_VERDICTS = {
    Outcome.CONFIRMED: "VERIFIED",
    Outcome.REFUTED: "REFUTED",
    Outcome.UNCERTAIN: "RECONCILIATION_REQUIRED",
}


@dataclass(frozen=True)
class WriteResult:
    #: ``CONFIRMED``, ``REFUTED`` or ``UNCERTAIN``.
    outcome: Outcome
    #: What was read, when, and what this result doesn't cover.
    scope: Scope
    #: ``{"delta", "frame", "source"}``: ``held``, ``refuted`` or
    #: ``not_checked`` (``source`` is ``instruction`` when none was given).
    checked: dict[str, Any] = field(default_factory=dict)
    #: Plain sentences, without record values.
    reasons: list[str] = field(default_factory=list)
    #: True only when the caller said the write was never sent and the store
    #: shows nothing landed.
    retry_safe: bool = False
    #: The verify-run receipt's ``outcome`` (``verified``,
    #: ``reconciliation_required``...), or "" when no receipt was issued.
    receipt_outcome: str = ""
    #: ``present``, ``absent``, ``conflicting`` or ``unknown``.
    observed: str = "unknown"
    receipt: dict[str, Any] = field(default_factory=dict, repr=False)
    receipt_sha256: str = ""
    receipt_path: Optional[str] = None
    evidence: dict[str, Any] = field(default_factory=dict, repr=False)
    write_id: str = ""
    #: The write's ledger key (an HMAC, no values), or "" without a ledger.
    write_key: str = ""
    declared_by: str = "agent"
    authorization: str = "unbound"
    seal: str = "off"
    seal_path: Optional[str] = None

    @property
    def verified(self) -> bool:
        return self.outcome is Outcome.CONFIRMED

    @property
    def verdict(self) -> str:
        """The outcome under its old name (``VERIFIED``, ``REFUTED``,
        ``RECONCILIATION_REQUIRED``)."""

        return _VERDICTS[self.outcome]

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)

    def to_dict(self) -> dict[str, Any]:
        """The reply an agent or the CLI reads: closed fields only."""

        return {
            "verified": self.verified,
            "outcome": self.outcome.value,
            "verdict": self.verdict,
            "retry_safe": self.retry_safe,
            "reason": self.reason,
            "checked": {k: v for k, v in self.checked.items() if k != "coverage"},
            "observed": self.observed,
            "scope": self.scope.to_dict(),
            "write_id": self.write_id,
            "write_key": self.write_key,
            "receipt_outcome": self.receipt_outcome,
            "receipt_sha256": self.receipt_sha256 or None,
            "receipt_path": self.receipt_path,
            "values_declared_by": self.declared_by,
            "authorization": self.authorization,
            "seal": self.seal,
            "seal_path": self.seal_path,
        }


_NO_LEDGER: Any = object()  # sentinel: the caller passed no ``ledger``


@dataclass
class PendingWrite:
    """A write that may now be sent: the store was read before it, and the
    ledger holds its lease. Single use: :func:`verify_write` consumes it."""

    contract: Contract = field(repr=False)
    params: Mapping[str, Any] = field(repr=False)
    actor: Actor
    write_id: str
    workdir: Path
    authorization_ref: str = field(repr=False)
    #: An operator's authorization reference was given (not defaulted).
    authorized: bool = False
    declared_by: str = "agent"
    write_key: str = ""
    ledger: Optional[Ledger] = field(default=None, repr=False)
    write_key_bound: bool = False
    used: bool = False
    _lease: Any = field(default=None, repr=False)

    @property
    def baseline_path(self) -> Path:
        return self.workdir / "baseline.json"

    def release(self) -> None:
        """Give up the ledger lease without verifying (the process keeps
        running but abandons the write). The attempt stays open, so the next
        caller reconciles it before any resend."""

        lease, self._lease = self._lease, None
        if lease is not None:
            lease.release()


def _resolve_ledger(ledger: Any) -> Optional[Ledger]:
    if ledger is _NO_LEDGER:
        return default_ledger()
    if ledger is None or isinstance(ledger, Ledger):
        return ledger
    return Ledger(ledger)  # a LedgerBackend


def begin_write(
    contract: Contract,
    params: Mapping[str, Any],
    *,
    actor: Actor,
    ledger: Any = _NO_LEDGER,
    write_key: Optional[str] = None,
    authorization_ref: str = "",
    workdir: Union[str, Path, None] = None,
    declared_by: str = "agent",
) -> PendingWrite:
    """Consult the ledger, then read the system of record before the write.

    Args:
        contract: What the write must do (and, with a frame, must not do).
        params: The values the write will save, and any that name the record.
        actor: Who writes. Its own changes are never excused as another
            user's.
        ledger: The uncertain-write ledger: a :class:`Ledger`, a
            :class:`LedgerBackend`, or None to turn it off (only for a
            sandbox that resets between runs). Default: :class:`FileLedger`
            in ``OPENADAPT_LEDGER_DIR`` or ``~/.openadapt/ledger``.
        write_key: The caller's id for this one write (a task id). With it,
            a confirmed write is never made again under the same key;
            without it, two writes with the same values are the same write
            only while the first one's outcome is open.
        authorization_ref: An opaque reference to what authorized the write;
            only its salted digest enters the receipt.
        workdir: Where this write's private folder goes (default: the
            ledger's, or ``~/.openadapt/verify`` without one).
        declared_by: Who declared the values, for the receipt reply.

    Raises :class:`WriteBlocked` (:class:`BaselineUnreadable`,
    :class:`WriteHeld` or :class:`AlreadyWritten`) when the write must not
    be sent, and :class:`VerifierError` for a value the contract needs that
    ``params`` lacks.
    """

    actor.run_record()  # an unknown kind fails before anything is read
    if authorization_ref and not _OPAQUE.match(authorization_ref):
        raise VerifierError(
            "authorization_ref must be an opaque id (8-128 of A-Za-z0-9._:-)"
        )
    names = contract.params
    missing = [n for n in names if n not in params]
    if missing:
        raise VerifierError(f"values missing for: {', '.join(missing)}")
    values = {n: params[n] for n in names}
    led = _resolve_ledger(ledger)
    write_id = f"write-{secrets.token_hex(12)}"
    if led is None:
        folder = _new_folder(Path(workdir or DEFAULT_WORKDIR), write_id)
        return _capture(
            contract, params, actor, write_id, folder, authorization_ref, declared_by
        )
    try:
        key = led.key_for(contract.digest, values, write_key)
        lease = led.backend.lease(key)
    except LedgerError as exc:
        raise WriteHeld(f"the write ledger can't be used ({exc})") from exc
    if lease is None:
        raise WriteHeld(
            "this write is in flight in another worker; wait for its result",
            key=key,
        )
    try:
        _settle(led, key, contract, params, actor)
        folder = (
            _new_folder(Path(workdir), write_id)
            if workdir is not None
            else led.backend.attempt_dir(write_id)
        )
        pending = _capture(
            contract, params, actor, write_id, folder, authorization_ref, declared_by
        )
        _write_private(
            folder / "attempt.json",
            {"values": values, "authorization_ref": pending.authorization_ref},
        )
        led.begun(
            key,
            write_id,
            folder=str(folder),
            contract=contract.digest,
            actor_kind=actor.kind,
            actor_id=actor.id,
            write_key_bound=write_key is not None,
            authorized=pending.authorized,
            declared_by=declared_by,
        )
    except BaseException:
        lease.release()
        raise
    pending.ledger = led
    pending.write_key = key
    pending.write_key_bound = write_key is not None
    pending._lease = lease
    return pending


def _new_folder(root: Path, write_id: str) -> Path:
    folder = root.expanduser() / write_id
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o700)
    return folder


def _capture(
    contract: Contract,
    params: Mapping[str, Any],
    actor: Actor,
    write_id: str,
    folder: Path,
    authorization_ref: str,
    declared_by: str,
) -> PendingWrite:
    pending = PendingWrite(
        contract=contract,
        params=dict(params),
        actor=actor,
        write_id=write_id,
        workdir=folder,
        authorization_ref=authorization_ref or write_id,
        authorized=bool(authorization_ref),
        declared_by=declared_by,
    )
    try:
        payload = capture_baseline(
            contract.config, pending.params, pending.baseline_path
        )
    except Exception as exc:  # a store that can't be reached, a bad binding
        raise BaselineUnreadable(
            "the system of record could not be read before the write "
            f"({type(exc).__name__})"
        ) from None
    state = payload.get("state") or {}
    if not state.get("reachable"):
        raise BaselineUnreadable(
            "the system of record could not be read before the write"
        )
    database = contract.config.frame.database if contract.config.frame else None
    if database is not None and FRAME_DB_DETAIL not in (state.get("detail") or {}):
        raise BaselineUnreadable("the database could not be read for the frame")
    return pending


def _settle(
    led: Ledger,
    key: str,
    contract: Contract,
    params: Mapping[str, Any],
    actor: Actor,
) -> None:
    """With the lease held: resolve the key's earlier attempt, or raise."""

    attempt = led.latest(key)
    if attempt is not None and attempt.state in ("open", "uncertain"):
        # Open with a free lease: its owner died between the send and the
        # read-back. Read back before anything is sent.
        _reconcile(led, key, attempt, contract, params)
        attempt = led.latest(key)
    state = attempt.state if attempt is not None else "free"
    if state == "free":
        return
    if state == "undelivered":
        assert attempt is not None and attempt.resolved is not None
        result = dict(attempt.resolved.get("result") or {})
        result["not_sent_again"] = True
        if not attempt.delivered:
            led.delivered(key, attempt.attempt_id)
        raise AlreadyWritten(
            "this write was already made and checked; its result is attached, "
            "and it was not sent again",
            key=key,
            result=result,
        )
    if state == "held":
        raise WriteHeld(
            "an earlier attempt of this write was refuted; a person must check "
            "the record and release it (openadapt verify ledger release)",
            key=key,
        )
    raise WriteHeld(
        "an earlier attempt of this write may still land; the store doesn't "
        "show it yet, so it is held until a read-back finds it or a person "
        "releases it",
        key=key,
    )


def _reconcile(
    led: Ledger,
    key: str,
    attempt: Attempt,
    contract: Contract,
    params: Optional[Mapping[str, Any]] = None,
) -> None:
    """Resolve ``attempt`` by reading back. The caller holds the lease.

    The attempt is judged against its own baseline, so a record that was
    there before it is never taken for its write. A baseline a check already
    used is copied for this read: it is the same write, and the lease keeps
    any other attempt of it from being sent meanwhile. Past the contract's
    window the baseline proves nothing, and the attempt stays uncertain.
    """

    if attempt.begun.get("contract") != contract.digest:
        return  # another contract's attempt: nothing here can judge it
    folder = attempt.folder
    baseline = folder / "baseline.json"
    try:
        saved = json.loads((folder / "attempt.json").read_text(encoding="utf-8"))
        payload = json.loads(baseline.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # nothing to judge with: stays as it is, for a person
    workdir = folder
    if baseline.with_name(baseline.name + ".used").exists():
        workdir = folder / f"reconcile-{secrets.token_hex(6)}"
        workdir.mkdir(mode=0o700)
        _write_private(workdir / "baseline.json", payload)
    previous = (attempt.resolved or {}).get("receipt_sha256")
    pending = PendingWrite(
        contract=contract,
        params={**(saved.get("values") or {}), **dict(params or {})},
        actor=Actor(
            str(attempt.begun.get("actor_kind") or "agent"),
            str(attempt.begun.get("actor_id") or "unidentified"),
        ),
        write_id=attempt.attempt_id,
        workdir=workdir,
        authorization_ref=str(saved.get("authorization_ref") or attempt.attempt_id),
        authorized=bool(attempt.begun.get("authorized")),
        declared_by=str(attempt.begun.get("declared_by") or "agent"),
        write_key=key,
    )
    result = _judge(
        pending,
        write_attempted=True,
        source=None,
        seal=True,
        remediates=f"sha256:{previous}" if previous else None,
    )
    if result.outcome is not Outcome.UNCERTAIN or attempt.resolved is None:
        _record(
            led,
            key,
            attempt.attempt_id,
            result,
            delivered=False,
            via="reconcile",
            write_key_bound=bool(attempt.begun.get("write_key_bound")),
        )
    if (
        result.outcome is Outcome.UNCERTAIN
        and result.observed == "absent"
        and led.settle_s is not None
        and _age_s(attempt) >= led.settle_s
    ):
        led.resolved(
            key,
            attempt.attempt_id,
            outcome=Outcome.UNCERTAIN.value,
            retry_safe=True,
            delivered=True,
            via="settled",
            reason=f"the store showed nothing {int(led.settle_s)}s after the attempt",
        )


def _age_s(attempt: Attempt) -> float:
    import calendar
    import time

    try:
        began = calendar.timegm(
            time.strptime(str(attempt.begun.get("at")), "%Y-%m-%dT%H:%M:%SZ")
        )
    except ValueError:
        return 0.0
    return time.time() - began


def verify_write(
    pending: PendingWrite,
    *,
    write_attempted: bool = True,
    source: Optional[Source] = None,
    seal: bool = True,
    delivered: bool = True,
) -> WriteResult:
    """Read the system of record now and judge the write against the
    baseline :func:`begin_write` took. Never resends the write.

    Args:
        pending: From :func:`begin_write`. Single use.
        write_attempted: False only when the write was never sent (a stop
            before the save); then a store that shows nothing makes the
            result ``retry_safe``.
        source: The written values' provenance and documents, to compare the
            saved record with its source.
        seal: Sign a confirmed receipt with this computer's Seal key.
        delivered: False when the reply won't reach the caller (a
            cancelled tool call); the next identical call then gets this
            result instead of a resend.
    """

    if pending.used:
        raise VerifierError("this write was already checked; begin a new one")
    pending.used = True
    try:
        result = _judge(
            pending, write_attempted=write_attempted, source=source, seal=seal
        )
        if pending.ledger is not None:
            _record(
                pending.ledger,
                pending.write_key,
                pending.write_id,
                result,
                delivered=delivered,
                via="verify_write",
                write_key_bound=pending.write_key_bound,
            )
    finally:
        pending.release()
    return result


def _record(
    led: Ledger,
    key: str,
    attempt_id: str,
    result: WriteResult,
    *,
    delivered: bool,
    via: str,
    write_key_bound: bool,
) -> None:
    led.resolved(
        key,
        attempt_id,
        outcome=result.outcome.value,
        retry_safe=result.retry_safe,
        delivered=delivered,
        via=via,
        write_key_bound=write_key_bound,
        receipt_sha256=result.receipt_sha256 or None,
        result=result.to_dict(),
    )


def _judge(
    pending: PendingWrite,
    *,
    write_attempted: bool,
    source: Optional[Source],
    seal: bool,
    remediates: Optional[str] = None,
) -> WriteResult:
    run_record = pending.actor.run_record()
    base: dict[str, Any] = {
        "write_id": pending.write_id,
        "write_key": pending.write_key,
        "declared_by": pending.declared_by,
        "authorization": "operator" if pending.authorized else "unbound",
    }
    try:
        doc = verify_run(
            pending.contract.config,
            params=pending.params,
            run_record=run_record,
            authorization_ref=pending.authorization_ref,
            baseline_path=pending.baseline_path,
            private_dir=pending.workdir,
            correlation_id=pending.write_id,
            remediates=remediates,
            write_attempted=write_attempted,
            provenance=None if source is None else source.provenance,
            documents=None if source is None else source.documents,
        )
        path: Optional[str] = str(write_result(doc, pending.workdir))
    except Exception as exc:  # noqa: BLE001 - unreadable store, timeout, used baseline
        return WriteResult(
            outcome=Outcome.UNCERTAIN,
            scope=_scope(pending.contract, {}),
            reasons=[
                "the system of record could not be read back "
                f"({type(exc).__name__}); the write may still land"
            ],
            **base,
        )
    receipt = dict(doc["receipt"])
    evidence = dict(doc["evidence"])
    receipt_outcome = str(receipt.get("outcome") or "")
    checked = dict(receipt.get("checked") or {})
    outcome, retry_safe = classify(receipt_outcome, checked, evidence)
    seal_path, seal_state = None, "off"
    if outcome is Outcome.CONFIRMED and seal and path:
        seal_path, seal_state = _seal(path, pending.write_id)
    return WriteResult(
        outcome=outcome,
        scope=_scope(pending.contract, evidence, receipt),
        checked=checked,
        reasons=_reasons(evidence, receipt_outcome),
        retry_safe=retry_safe,
        receipt_outcome=receipt_outcome,
        observed=_observed(evidence),
        receipt=receipt,
        receipt_sha256=receipt_sha256(receipt),
        receipt_path=path,
        evidence=evidence,
        seal=seal_state,
        seal_path=seal_path,
        **base,
    )


def classify(
    receipt_outcome: str, checked: Mapping[str, Any], evidence: Mapping[str, Any]
) -> tuple[Outcome, bool]:
    """A verify-run receipt's outcome as (:class:`Outcome`, retry_safe)."""

    if receipt_outcome == "verified":
        return Outcome.CONFIRMED, False
    if receipt_outcome == "halted_before_effect":
        # The caller never sent it, and the store shows nothing landed.
        return Outcome.REFUTED, True
    if receipt_outcome == "rejected_policy":
        return Outcome.REFUTED, False  # landed, but approved by its own executor
    if receipt_outcome != "reconciliation_required":
        return Outcome.UNCERTAIN, False
    if checked.get("frame") == "refuted" or checked.get("source") == "refuted":
        return Outcome.REFUTED, False
    if checked.get("delta") != "refuted":
        return Outcome.UNCERTAIN, False
    rows = (evidence.get("readback") or {}).get("effects") or []
    failed = [
        r for r in rows if isinstance(r, Mapping) and r.get("verdict") != "CONFIRMED"
    ]
    if failed and all(r.get("observed_effect") == "absent" for r in failed):
        return Outcome.UNCERTAIN, False
    return Outcome.REFUTED, False


def _observed(evidence: Mapping[str, Any]) -> str:
    rows = (evidence.get("readback") or {}).get("effects") or []
    seen = {str(r.get("observed_effect")) for r in rows if isinstance(r, Mapping)}
    if (evidence.get("frame") or {}).get("status") == "refuted":
        seen.add("conflicting")
    if not seen or "unknown" in seen or "None" in seen:
        return "unknown"
    if "conflicting" in seen or seen == {"absent", "present"}:
        return "conflicting"
    return "absent" if "absent" in seen else "present"


def _scope(
    contract: Contract,
    evidence: Mapping[str, Any],
    receipt: Optional[Mapping[str, Any]] = None,
) -> Scope:
    cfg = contract.config
    readback = evidence.get("readback") or {}
    rows = [r for r in readback.get("effects") or [] if isinstance(r, Mapping)]
    frame = evidence.get("frame") or {}
    frame_read = bool(frame) and frame.get("status") != "not_checked"
    stores: list[str] = []
    unread: list[str] = []
    for row in rows:
        name = str(row.get("substrate") or cfg.verifier.kind)
        target = unread if row.get("unavailable") or row.get("stale") else stores
        if name not in target:
            target.append(name)
    if not rows:
        unread.append(cfg.verifier.kind)
    spec = cfg.frame
    if spec is not None and getattr(spec, "database", None) is not None:
        (stores if frame_read and "database" in frame else unread).append(
            "frame.database"
        )
    attribution = getattr(spec, "attribution", None) if spec is not None else None
    if attribution is not None and attribution.kind == "audit_read":
        (stores if frame_read else unread).append("audit")
    paths: list[str] = []
    for name in stores:
        path = (
            "database"
            if name == "frame.database"
            else "audit_log"
            if name == "audit"
            else READ_PATHS.get(name, name)
        )
        if path not in paths:
            paths.append(path)
    contracts = (receipt or {}).get("contracts") or {}
    strength = str(contracts.get("observed_effect_strength") or "unchecked")
    not_covered = [
        "writes that land after read_at (delayed or queued writes)",
        "stores the contract doesn't read: email, other services, files",
        "whether the write was authorized; the verifier doesn't judge it",
    ]
    if spec is None or getattr(spec, "database", None) is None:
        not_covered.append("records outside the read set (no frame.database)")
    return Scope(
        read_paths=tuple(paths),
        stores=tuple(stores),
        unread=tuple(unread),
        window=Window(
            baseline_read_at=readback.get("baseline_read_at"),
            read_at=readback.get("read_at"),
            max_execution_window_s=readback.get("max_execution_window_s"),
        ),
        strength=strength,
        not_covered=tuple(not_covered),
    )


def _reasons(evidence: Mapping[str, Any], receipt_outcome: str) -> list[str]:
    out: list[str] = []
    for row in (evidence.get("readback") or {}).get("effects") or []:
        if not isinstance(row, Mapping) or str(row.get("verdict")) == "CONFIRMED":
            continue
        out.append(
            f"{row.get('kind')} was {str(row.get('verdict')).lower()}: observed "
            f"{row.get('observed_effect')}, {row.get('observed_count')} of "
            f"{row.get('expected_count')} expected record(s)"
        )
    frame = evidence.get("frame") or {}
    if frame.get("status") == "refuted":
        kinds = ", ".join(frame.get("collateral_kinds") or [])
        out.append(
            f"{frame.get('collateral_count', 0)} record(s) outside the declared "
            f"write moved ({kinds}); "
            f"{frame.get('unattributed_count', 0)} of them no actor explains"
        )
    elif frame.get("status") == "not_checked":
        out.append("what else changed could not be read, so the frame is not checked")
    source = evidence.get("source") or {}
    if isinstance(source, Mapping) and source.get("status") == "refuted":
        fields = ", ".join(source.get("mismatched_fields") or [])
        out.append(f"the saved record differs from its source in {fields}")
    if receipt_outcome == "rejected_policy":
        out.append("the write's approver is its own executor")
    if not out and receipt_outcome not in ("verified", ""):
        out.append(
            "the read was too weak to certify the write "
            f"({(evidence.get('readback') or {}).get('verification_tier')})"
        )
    return out


def _seal(receipt_path: str, write_id: str) -> tuple[Optional[str], str]:
    """Sign the receipt file with this computer's Seal key. Returns the Seal's
    path and ``sealed``; or no path and ``off`` (Seals turned off) or
    ``unsealed: <why>``. The outcome stands either way: the Seal attests who
    issued the receipt, not the write."""

    from openadapt_flow import offline_seal

    if not offline_seal.mode_enabled():
        return None, "off"
    try:
        sealed = offline_seal.sign_file(
            Path(receipt_path), subject_kind="record", run_id=write_id
        )
        out = Path(receipt_path).with_name("receipt.seal.json")
        _write_private(out, sealed.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001 - say why, never fail the write
        return (
            None,
            f"unsealed: this computer's Seal key couldn't be used ({type(exc).__name__})",
        )
    return str(out), "sealed"


@dataclass
class WriteBlock:
    """What :func:`verified_write` yields; ``result`` is set after the block."""

    pending: PendingWrite
    write_attempted: bool = True
    result: Optional[WriteResult] = None


@contextmanager
def verified_write(
    contract: Contract,
    params: Mapping[str, Any],
    *,
    actor: Actor,
    **options: Any,
) -> Iterator[WriteBlock]:
    """``with verified_write(...) as w: send()`` then read ``w.result``.

    ``options`` go to :func:`begin_write`. If the block raises before the
    write is sent, set ``w.write_attempted = False`` first; the read-back
    runs either way, because a half-sent write may have landed.
    """

    block = WriteBlock(begin_write(contract, params, actor=actor, **options))
    try:
        yield block
    finally:
        block.result = verify_write(
            block.pending, write_attempted=block.write_attempted
        )


def reconcile(
    contract: Contract,
    key: str,
    *,
    ledger: Any = _NO_LEDGER,
) -> Optional[str]:
    """Resolve the key's open or uncertain attempt by reading back.

    Returns the attempt's state afterwards (``free``, ``undelivered``,
    ``uncertain``, ``held``, ``open``), or None when the key has no
    attempt. Raises :class:`WriteHeld` when another worker holds the key.
    """

    led = _resolve_ledger(ledger)
    if led is None:
        raise VerifierError("reconcile needs a ledger")
    lease = led.backend.lease(key)
    if lease is None:
        raise WriteHeld("this write is in flight in another worker", key=key)
    try:
        attempt = led.latest(key)
        if attempt is None:
            return None
        if attempt.state in ("open", "uncertain"):
            _reconcile(led, key, attempt, contract)
            attempt = led.latest(key)
        return attempt.state if attempt is not None else None
    finally:
        lease.release()


# -- the database frame, without a contract (from #1588) ----------------------

#: The key the snapshots of this process share, when the caller gives none:
#: two captures taken in one process compare. Saved snapshots need an
#: explicit ``key``.
_PROCESS_KEY = secrets.token_bytes(32)


def capture_database(
    read: Mapping[str, Any] | Callable[[], Any],
    dialect: str,
    *,
    allow_tables: Sequence[str] = (),
    max_rows: int = 2000,
    key: Optional[bytes] = None,
    profile: Optional[str] = None,
    before: Optional[DatabaseSnapshot] = None,
    actor_names: Sequence[str] = (),
) -> DatabaseSnapshot:
    """Snapshot every table of a database, in one read-only transaction.

    ``read`` is a read-only connection, as in ``frame.database.read``
    (``sqlite_database``, or ``sql_driver`` with ``sql_connect_args`` and
    ``sql_password_env``), or a function that opens one DB-API connection
    (to read through ``podman exec``, say). It talks to the database over
    that connection only; no application code is imported. The snapshot
    holds keyed MACs, never values: ``key`` keys them (default: one key per
    process). ``allow_tables`` are kept append-only.

    ``profile`` (``openemr``) applies an app profile's bookkeeping rules, the
    same ones the frame uses (``openadapt_flow.app_profiles``); its own-row
    rules need the acting user's sign-in names (``actor_names``), whose ids
    are read in the same transaction. ``before``: a snapshot taken earlier
    with the same key, so this one keeps the plain values of the rows that
    differ from it, in memory only. Raises :class:`BaselineUnreadable`.
    """

    from openadapt_flow.verify_run import _db_connect

    if dialect not in ("sqlite", "mysql"):
        raise VerifierError(f"dialect {dialect!r} is not sqlite or mysql")
    connect = read if callable(read) else _db_connect(read)
    extra: dict[str, str] = {}
    if profile is not None:
        from openadapt_flow.app_profiles import load_profile

        query = load_profile(profile).actor_ids_query(list(actor_names), dialect)
        if query:
            extra["actor_ids"] = query
    try:
        return take_snapshot(
            connect,
            dialect,  # type: ignore[arg-type]
            key=key or _PROCESS_KEY,
            modes=dict.fromkeys(allow_tables, "append_only"),
            max_rows=max_rows,
            before=before,
            extra_queries=extra,
            **_profile_args(profile),
        )
    except Exception as exc:  # noqa: BLE001 - any read failure is unreadable
        raise BaselineUnreadable(f"the database could not be read: {exc}") from exc


def _profile_args(profile: Optional[str]) -> dict[str, Any]:
    if profile is None:
        return {}
    from openadapt_flow.app_profiles import load_profile

    p = load_profile(profile)
    return {
        "optional_modes": {t["table"]: t["mode"] for t in p.allow_tables()},
        "ignore": p.ignore_columns(),
        "ref_columns": p.ref_columns(),
    }


def diff_database(
    before: DatabaseSnapshot,
    after: DatabaseSnapshot,
    *,
    allow_tables: Sequence[str] = (),
    key: Optional[bytes] = None,
    profile: Optional[str] = None,
    actor_names: Sequence[str] = (),
) -> dict[str, TableDiff]:
    """Every table that moved, with counts of rows added, removed and changed
    and the changed column names, never values. Empty when the database is
    back where it was (a compensation that undid everything). Append-only
    tables whose existing rows moved, changed definitions or code, and a row
    written then removed all show, under ``unread``.

    With ``profile`` (captured with the same profile and ``actor_names``, and
    ``after`` captured with ``before=``), a row change the profile's
    conditional rules call the app's own (the acting user's sign-in
    counters, a uuid backfill) is left out, as the frame leaves it out.
    """

    from openadapt_flow.runtime.effects.db_snapshot import cell_hash, unpack

    k = key or _PROCESS_KEY
    moved = diff(before, after, key=k, allow_tables=allow_tables)
    if profile is None:
        return moved
    from openadapt_flow.app_profiles import load_profile

    p = load_profile(profile)
    names = set(actor_names)
    ids = {str(r.get("id")) for r in after.extra.get("actor_ids", [])}
    out: dict[str, TableDiff] = {}
    for name, d in moved.items():
        a, b = before.tables.get(name), after.tables.get(name)
        if d.unread or a is None or b is None or a.cell_columns != b.cell_columns:
            out[name] = d
            continue
        hasher = (lambda t: lambda col, v: cell_hash(k, v, t, col))(name)
        kept = TableDiff()
        columns: set[str] = set()
        for row_key in a.rows.keys() | b.rows.keys():
            old, new = a.rows.get(row_key), b.rows.get(row_key)
            if old is not None and new is not None and old[0] == new[0]:
                continue
            was = None if old is None else unpack(a.cell_columns, old[1])
            now = None if new is None else b.values.get(row_key)
            if new is not None and now is None:
                kept.changed += 1  # no plain values to judge: keep it
                continue
            fields = (
                []
                if was is None or now is None
                else sorted(
                    c for c in a.cell_columns if was[c] != hasher(c, now.get(c))
                )
                or ["(columns not compared)"]
            )
            if p.excuse(name, was, now, fields, hasher, names, ids):
                continue
            if was is None:
                kept.added += 1
            elif now is None:
                kept.removed += 1
            else:
                kept.changed += 1
                columns.update(fields)
        kept.changed_columns = sorted(columns)
        if kept.added or kept.removed or kept.changed:
            out[name] = kept
    return out
