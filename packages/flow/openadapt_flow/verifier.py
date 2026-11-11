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

Writes that land later: the frame reads the database twice after the save,
``settle_s`` apart, so a write the app makes later than that (a background
job, a trigger-driven integration, a delayed side record) lands after the
verdict. With ``frame.database.recheck_after_s`` set, a confirmed result
carries a private :class:`RecheckState` (saved as ``recheck.json`` in the
write's folder) and says when the recheck is due
(``WriteResult.recheck_due_at``, the receipt's ``recheck_due_at``)::

    result = verify_write(pending)
    ...                                       # the caller works on the next item
    late = recheck(contract, result, params=params, actor=actor,
                   excused_by=[next_result])  # later writes, any outcome
    if late.outcome == "REFUTED":             # changed after the verdict
        print(late.reasons)

:func:`recheck` reads the whole database again at or after the due time
and judges every change since the post-save read with the frame's rules.
Before the due time it raises :class:`RecheckNotDue` (``wait=True`` sleeps
until then instead). Its outcome is ``CONFIRMED``, ``REFUTED`` (late: the
earlier CONFIRMED no longer stands) or ``UNCERTAIN`` (the database or the
state couldn't be read or doesn't match: never a pass). A confirmed result
that carries ``recheck_due_at`` is provisional until the recheck runs.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional, Union

from openadapt_flow.complete_read import CompleteReadProvider
from openadapt_flow.runtime.effects.db_snapshot import (
    DEFAULT_READ_ROW_BUDGET,
    TableDiff,
    diff,
    take_snapshot,
)
from openadapt_flow.runtime.effects.db_snapshot import Snapshot as DatabaseSnapshot
from openadapt_flow.runtime.effects.late_write import (
    EVIDENCE_KEY,
    LATER_WRITE_DISPUTED,
    LATER_WRITE_UNJUDGED,
    LateOutcome,
    parse_time,
    seconds_until,
    state_problem,
)
from openadapt_flow.verify_run import (
    FRAME_DB_DETAIL,
    FRAME_DB_ERROR,
    OwnedBaselineReference,
    VerificationPurpose,
    VerifyConfig,
    VerifyRunError,
    _commit_owned_sqlite_insert,
    _owned_baseline_reference,
    _require_recheck_purpose,
    _require_verification_purpose,
    _write_private,
    capture_baseline,
    parse_verify_config,
    receipt_sha256,
    recheck_problem,
    recheck_run,
    recheck_state,
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
    "RecheckNotDue",
    "RecheckResult",
    "RecheckState",
    "Scope",
    "Snapshot",
    "Source",
    "TableDiff",
    "VerifierError",
    "Window",
    "WriteBlocked",
    "WriteHeld",
    "WriteResult",
    "begin_write",
    "capture",
    "capture_database",
    "diff_database",
    "recheck",
    "reconcile",
    "verified_write",
    "verify_write",
    "issue_recheck_receipt",
    "issue_write_receipt",
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


#: Who declared the values: one name for all (``"agent"``), or each value's
#: name mapped to who set it (``{"clinic": "operator", "note": "agent"}``).
#: Names only, never values.
DeclaredBy = Union[str, Mapping[str, str]]


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

        config = self.config.model_dump(mode="json")
        if config.get("owned_sqlite_insert") is None:
            config.pop("owned_sqlite_insert", None)
        config["verifier"] = self._verifier_dump()
        body = json.dumps(
            {
                "config": config,
                "writes": list(self.writes),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @property
    def store(self) -> str:
        """SHA-256 of which system of record the contract writes to: the
        verifier's kind and location and the ``environment_id``. Timings and
        credentials are left out, and a loopback host is one host."""

        return _digest(
            {
                "environment": self.config.environment_id,
                "verifier": _strip(self._verifier_dump(), drop_secrets=True),
            }
        )

    def _verifier_dump(self) -> dict[str, Any]:
        """The verifier as hashed: fields that lost their defaults still hash
        as their old values, so contracts written before keep their digests."""

        from openadapt_flow.deployment import legacy_hash_dump

        dumped = self.config.verifier.model_dump(mode="json")
        legacy_hash_dump(dumped, defaults_excluded=False)
        return dumped

    @property
    def shape(self) -> str:
        """SHA-256 of the effects, the verifier kind, the ``environment_id``
        and the hosts (or database files) the verifier reads: the same write
        to the same named environment and host, however the address is
        split or spelled. Used only to hold, never to hand back."""

        hosts: set[str] = set()

        def walk(value: Any, name: str = "") -> None:
            if isinstance(value, Mapping):
                for k, v in value.items():
                    walk(v, str(k))
            elif isinstance(value, (list, tuple)):
                for v in value:
                    walk(v, name)
            elif isinstance(value, str) and _URL.match(value):
                hosts.add(_location(value).split("://", 1)[1].split("/", 1)[0])
            elif name.endswith(("database", "_env")) or name in _HOST_KEYS:
                # A database file or server, or the credential that picks
                # a tenant: each tells one store from another.
                hosts.add(f"{name}={_location(str(value)).lower()}")

        verifier = self.config.verifier.model_dump(mode="json")
        walk(verifier)
        return _digest(
            {
                "effects": [
                    _strip(e.model_dump(mode="json")) for e in self.config.effects
                ],
                "kind": verifier.get("kind"),
                "environment": self.config.environment_id,
                "hosts": sorted(hosts),
            }
        )

    @property
    def identity(self) -> str:
        """SHA-256 of what the write is and where: each effect's kind, target
        and values, unbound, and :attr:`store`. Timings, the window and the
        frame are left out, so editing them between a crash and a restart
        can't make the same write look new."""

        return _digest(
            {
                "effects": [
                    _strip(e.model_dump(mode="json")) for e in self.config.effects
                ],
                "store": self.store,
            }
        )

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

    def null_unsafe_params(self) -> dict[str, str]:
        """Each parameter that may not be ``None``, with where it is used.

        A null binds only in an effect's ``match``, ``new_records`` and
        ``value``, where it must equal a stored null. Anywhere else (a query
        or path parameter, an idempotency or composite key, a read-back
        field, the frame) a null has no value to send or compare, so
        :func:`begin_write` refuses it before anything is read.
        """

        found: dict[str, str] = {}

        def walk(value: Any, where: str) -> None:
            if isinstance(value, Mapping):
                name = value.get("param")
                if isinstance(name, str) and name:
                    found.setdefault(name, where)
                for item in value.values():
                    walk(item, where)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    walk(item, where)

        dumped = self.config.model_dump(mode="json")
        for section, body in dumped.items():
            if section != "effects":
                walk(body, section)
        for index, effect in enumerate(dumped.get("effects") or []):
            for key, body in effect.items():
                if key not in ("match", "new_records", "value"):
                    walk(body, f"effects[{index}].{key}")
        return found


#: Settings that change how long a read waits, never what is written.
_TIMINGS = frozenset(
    {"timeout_s", "poll_interval_s", "approval_wait_s", "max_execution_window_s"}
)
#: Keys whose values are secrets themselves. ``*_env`` keys name an
#: environment variable, which tells tenants apart and holds no secret.
_SECRET = re.compile(r"password|secret|token|cookie|header|credential|value$", re.I)
_HOST_KEYS = frozenset({"host", "hostname", "port", "dbname", "db", "unix_socket"})
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_DEFAULT_PORTS = {"http": 80, "https": 443, "mysql": 3306, "postgresql": 5432}


def _location(value: str) -> str:
    """One spelling per address: scheme and host lower case, a loopback host
    as ``loopback``, no default port, no trailing slash."""

    if not _URL.match(value):
        return value.rstrip("/") if value.endswith("/") and len(value) > 1 else value
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return value
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if host in _LOOPBACK:
        host = "loopback"
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, parts.path.rstrip("/"), parts.query, ""))


def _strip(value: Any, *, drop_secrets: bool = False) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            name = str(k)
            if name in _TIMINGS:
                continue
            secret = (
                drop_secrets
                and not name.endswith("_env")
                and not isinstance(v, (Mapping, list, tuple))
                and _SECRET.search(name)
            )
            if not secret:
                out[name] = _strip(v, drop_secrets=drop_secrets)
        return out
    if isinstance(value, (list, tuple)):
        return [_strip(v, drop_secrets=drop_secrets) for v in value]
    if isinstance(value, str):
        return _location(value)
    return value


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


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
_STATUSES = {
    Outcome.CONFIRMED: "verified",
    Outcome.REFUTED: "refuted",
    Outcome.UNCERTAIN: "unconfirmed",
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
    declared_by: DeclaredBy = "agent"
    authorization: str = "unbound"
    seal: str = "off"
    seal_path: Optional[str] = None
    #: Not confirmed, but the store holds the declared records: the write is
    #: never sent again, and it isn't held for a person.
    landed: bool = False
    #: A person should look at the store: shown by ``openadapt verify ledger
    #: list`` until released, though it doesn't stop a retry being handed back.
    review: bool = False
    #: The ledger the write was made under (``begin_write``'s), so
    #: :func:`recheck` re-resolves the attempt there by default.
    ledger: Optional[Ledger] = field(default=None, repr=False, compare=False)
    #: When the contract sets ``frame.database.recheck_after_s`` and the
    #: database settled after the save: the private state :func:`recheck`
    #: needs (saved as ``recheck.json`` in the write's folder). A confirmed
    #: write's is what its recheck compares with; any write's can excuse its
    #: own window in an earlier write's recheck.
    recheck: Optional["RecheckState"] = field(default=None, repr=False, compare=False)
    #: When a confirmed write's recheck is due (whole-second UTC), as the
    #: receipt's ``recheck_due_at`` says; None when no recheck is due. Until
    #: the recheck runs, the result is provisional.
    recheck_due_at: Optional[str] = None
    #: ``off``, ``issued`` or ``not issued: <why>``: the signed write receipt
    #: (``docs/WRITE_RECEIPTS.md``). Issuing never changes the outcome.
    write_receipt: str = "off"
    write_receipt_path: Optional[str] = None

    @property
    def verified(self) -> bool:
        return self.outcome is Outcome.CONFIRMED

    @property
    def provisional(self) -> bool:
        """CONFIRMED, but a recheck is due (``recheck_due_at``): a write that
        lands before it can still refute this result, and only the recheck
        says the verdict stands. A write after the recheck is not seen."""

        return self.verified and bool(self.recheck_due_at)

    @property
    def final(self) -> bool:
        """Whether this verdict is resolved, with no owed final recheck.

        An uncertain read does not settle the verdict. A final refutation
        remains a negative verdict, not verification or permission to retry.
        """

        return (
            self.outcome in (Outcome.CONFIRMED, Outcome.REFUTED)
            and not self.provisional
        )

    @property
    def verdict(self) -> str:
        """The outcome under its old name (``VERIFIED``, ``REFUTED``,
        ``RECONCILIATION_REQUIRED``)."""

        return _VERDICTS[self.outcome]

    @property
    def status(self) -> str:
        """The outcome under #1588's names: ``verified``, ``refuted`` or
        ``unconfirmed``."""

        return _STATUSES[self.outcome]

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
            "values_declared_by": self.declared_by
            if isinstance(self.declared_by, str)
            else dict(self.declared_by),
            "authorization": self.authorization,
            "seal": self.seal,
            "seal_path": self.seal_path,
            "landed": self.landed,
            "review": self.review,
            "recheck_due_at": self.recheck_due_at,
            "provisional": self.provisional,
            "final": self.final,
            "write_receipt": self.write_receipt,
            "write_receipt_path": self.write_receipt_path,
        }


@dataclass
class RecheckState:
    """What a recheck compares with: the settled post-save snapshot as keyed
    MACs (no values), the audit entries seen then as keyed hashes, the run's
    salt, and the receipt and evidence it is bound to.

    Private, like a :class:`Snapshot`: it holds the salt, so :meth:`save`
    writes it with mode 0600, and the writer being judged must not be able
    to write it. Its salt and digests can be recomputed by whoever writes
    the file, so :func:`verify_write` records each state it issues under
    the state directory (``rechecks/``, by digest), and :func:`recheck` uses
    only a state with that record. ``params`` (the write's inputs) stay in memory only; a
    loaded state needs them back (:meth:`load`) to excuse a write
    (``excused_by``).
    """

    payload: dict[str, Any] = field(repr=False)
    params: Optional[Mapping[str, object]] = field(default=None, repr=False)
    path: Optional[Path] = field(default=None, repr=False)

    @property
    def due_at(self) -> str:
        """When the recheck is due (whole-second UTC)."""

        frame = (self.payload.get("evidence") or {}).get("frame") or {}
        return str((frame.get(EVIDENCE_KEY) or {}).get("due_at") or "")

    def save(self, path: str | Path) -> Path:
        out = Path(path)
        _write_private(out, self.payload)
        return out

    @classmethod
    def load(
        cls, path: str | Path, *, params: Optional[Mapping[str, object]] = None
    ) -> "RecheckState":
        """Read a state :meth:`save` wrote. Raises :class:`VerifierError`
        when it can't be read as one; :func:`recheck` checks the rest."""

        source = Path(path)
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise VerifierError(f"can't read the recheck state: {exc}") from exc
        problem = state_problem(payload)
        if problem is not None:
            raise VerifierError(problem)
        return cls(payload, params=params, path=source)


class RecheckNotDue(VerifierError):
    """The recheck is not due yet: a read now would end the window early."""

    def __init__(self, due_at: str, wait_s: float) -> None:
        super().__init__(f"the recheck is due at {due_at}, in {wait_s:.0f} s")
        self.due_at = due_at
        self.wait_s = wait_s


@dataclass(frozen=True)
class RecheckResult:
    #: ``CONFIRMED`` (nothing unexplained moved since the post-save read),
    #: ``REFUTED`` (something did: the earlier CONFIRMED no longer stands), or
    #: ``UNCERTAIN`` (the database or the state couldn't be read or doesn't
    #: match; never a pass).
    outcome: LateOutcome
    #: The frame since the post-save read: ``held``, ``refuted`` or
    #: ``not_checked``.
    frame: str
    #: Plain sentences, without record values.
    reasons: list[str] = field(default_factory=list)
    #: The earlier write's receipt digest.
    receipt_sha256: str = ""
    post_save_read_at: str = ""
    due_at: str = ""
    #: When the database was read for the recheck ("" when it wasn't).
    read_at: str = ""
    #: How many later writes were passed as excuses (``excused_by``).
    excused: int = 0
    #: The frame's closed evidence since the post-save read (counts and
    #: names, no values).
    evidence: dict[str, Any] = field(default_factory=dict)
    #: Identity of the original receipt plus the separately validated later
    #: receipts. These are data bindings, never native-write authority.
    recheck_claim_sha256: str = ""
    #: None means the later-receipt context was not validated. An empty
    #: tuple is the validated context with no later receipts.
    excuse_receipt_sha256s: Optional[tuple[str, ...]] = None
    #: A held native read did not establish the due time, so this recheck
    #: remains pending. No completed judgment may be recorded for it.
    recheck_owed: bool = False

    @property
    def hold_reason(self) -> Optional[str]:
        """The ledger reason for an existing terminal hold, if any."""

        return None

    @property
    def current_observation(self) -> Optional[dict[str, Any]]:
        """The bounded fresh read for a terminally held attempt, if any."""

        return None

    @property
    def verified(self) -> bool:
        """True only when this receipt-bound recheck claim still stands."""

        return self.outcome == "CONFIRMED"

    @property
    def final(self) -> bool:
        """A completed proof through read_at, with no unread history gap."""

        return (
            self.outcome in ("CONFIRMED", "REFUTED")
            and bool(self.read_at)
            and self.frame in ("held", "refuted")
        )

    @property
    def late(self) -> bool:
        """True when a change after the verdict refutes it."""

        return self.outcome == "REFUTED"

    @property
    def status(self) -> str:
        """The outcome under :class:`WriteResult`'s names."""

        return {"CONFIRMED": "verified", "REFUTED": "refuted"}.get(
            self.outcome, "unconfirmed"
        )


@dataclass(frozen=True, kw_only=True)
class _HeldRecheckResult(RecheckResult):
    _hold_reason: str
    _observed_status: LateOutcome

    @property
    def final(self) -> bool:
        return self._observed_status != "UNCERTAIN" and super().final

    @property
    def hold_reason(self) -> str:
        return self._hold_reason

    @property
    def current_observation(self) -> dict[str, Any]:
        def timestamp(value: str) -> Optional[str]:
            try:
                parse_time(value)
            except ValueError:
                return None
            return value

        return {
            "status": self._observed_status,
            "read_window": {
                "post_save_read_at": timestamp(self.post_save_read_at),
                "read_at": timestamp(self.read_at),
            },
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
    declared_by: DeclaredBy = "agent"
    write_key: str = ""
    values_key: str = field(default="", repr=False)
    #: When the baseline read began (epoch seconds): the window's start.
    started: float = field(default=0.0, repr=False)
    ledger: Optional[Ledger] = field(default=None, repr=False)
    write_key_bound: bool = False
    used: bool = False
    _lease: Any = field(default=None, repr=False)
    _read_provider: Optional[CompleteReadProvider] = field(default=None, repr=False)
    purpose: VerificationPurpose = "observation"
    _owned_reference: Optional[OwnedBaselineReference] = field(default=None, repr=False)
    _dispatch_lock: Any = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    @property
    def baseline_path(self) -> Path:
        return self.workdir / "baseline.json"

    def release(self) -> None:
        """Give up the ledger lease without verifying (the process keeps
        running but abandons the write). The attempt stays open, so the next
        caller reconciles it before any resend."""

        with self._dispatch_lock:
            lease, self._lease = self._lease, None
            if lease is not None:
                lease.release()


def _attempt_reference(attempt: Attempt) -> Optional[OwnedBaselineReference]:
    if attempt.begun.get("purpose") != "owned_attempt":
        return None
    execution_id = attempt.begun.get("owned_baseline_execution_id")
    digest = attempt.begun.get("owned_baseline_sha256")
    if (
        not isinstance(execution_id, str)
        or not _OPAQUE.fullmatch(execution_id)
        or not isinstance(digest, str)
        or not re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", digest)
    ):
        return None
    return attempt.attempt_id, execution_id, digest


def commit_pending_sqlite_insert(pending: PendingWrite) -> None:
    """Commit the declared SQLite insert while this attempt owns its ledger lease."""
    with pending._dispatch_lock:
        if (
            pending.used
            or pending.purpose != "owned_attempt"
            or pending.ledger is None
            or pending._lease is None
            or pending._owned_reference is None
        ):
            raise VerifyRunError("the pending write has no live owned dispatch lease")
        attempt = pending.ledger.latest(pending.write_key)
        if (
            attempt is None
            or attempt.state != "open"
            or _attempt_reference(attempt) != pending._owned_reference
        ):
            raise VerifyRunError("the journal does not own this dispatch reservation")
        _commit_owned_sqlite_insert(
            pending.contract.config,
            pending.params,
            pending.baseline_path,
            pending._owned_reference,
        )


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
    declared_by: DeclaredBy = "agent",
    read_provider: Optional[CompleteReadProvider] = None,
    task_record_sha256: Optional[str] = None,
) -> PendingWrite:
    """Consult the ledger, then capture an observation baseline before the write.

    Args:
        contract: What the write must do (and, with a frame, must not do).
        params: The values the write will save, and any that name the record.
        actor: Who writes. Its own changes are never excused as another
            user's.
        read_provider: An in-process reader bound to this config and params.
            Baseline and post-write checks use the same reader. Its ordinary
            completeness checks still apply; pending audit completion cannot
            authorize the baseline. Keep its session open through verification.
        ledger: The uncertain-write ledger: a :class:`Ledger`, a
            :class:`LedgerBackend`, or None to turn it off (only for a
            sandbox that resets between runs). Default: :class:`FileLedger`
            in ``OPENADAPT_LEDGER_DIR`` or ``~/.openadapt/ledger``.
        write_key: The caller's id for this one write (a task id). With it,
            a confirmed write is never made again under the same key;
            without it, two writes with the same values are the same write
            only while the first one's outcome is open.
        task_record_sha256: An optional lowercase SHA-256 digest stored
            on the original begun event only. It requires a ledger and does
            not affect write identity, verification, or retry decisions.
        authorization_ref: An opaque reference to what authorized the write;
            only its salted digest enters the receipt.
        workdir: Where this write's private folder goes (default: the
            ledger's, or ``~/.openadapt/verify`` without one).
        declared_by: Who declared the values, for the reply: a name, or each
            value's name mapped to who set it (operator-pinned or agent).
            To bind it into the receipt too, put it in ``actor.build``.

    Raises :class:`WriteBlocked` (:class:`BaselineUnreadable`,
    :class:`WriteHeld` or :class:`AlreadyWritten`) when the write must not
    be sent, and :class:`VerifierError` for a value the contract needs that
    ``params`` lacks, or that is ``None`` where a null can't be bound
    (:meth:`Contract.null_unsafe_params`).
    """

    return _begin_write(
        contract,
        params,
        actor=actor,
        ledger=ledger,
        write_key=write_key,
        authorization_ref=authorization_ref,
        workdir=workdir,
        declared_by=declared_by,
        read_provider=read_provider,
        task_record_sha256=task_record_sha256,
        purpose="observation",
    )


def begin_owned_write(
    contract: Contract,
    params: Mapping[str, Any],
    *,
    actor: Actor,
    ledger: Any = _NO_LEDGER,
    write_key: Optional[str] = None,
    authorization_ref: str = "",
    workdir: Union[str, Path, None] = None,
    declared_by: DeclaredBy = "agent",
    read_provider: Optional[CompleteReadProvider] = None,
    task_record_sha256: Optional[str] = None,
) -> PendingWrite:
    """Reserve an owned attempt whose confirmation requires commit evidence."""

    return _begin_write(
        contract,
        params,
        actor=actor,
        ledger=ledger,
        write_key=write_key,
        authorization_ref=authorization_ref,
        workdir=workdir,
        declared_by=declared_by,
        read_provider=read_provider,
        task_record_sha256=task_record_sha256,
        purpose="owned_attempt",
    )


def _begin_write(
    contract: Contract,
    params: Mapping[str, Any],
    *,
    actor: Actor,
    ledger: Any,
    write_key: Optional[str],
    authorization_ref: str,
    workdir: Union[str, Path, None],
    declared_by: DeclaredBy,
    read_provider: Optional[CompleteReadProvider],
    task_record_sha256: Optional[str],
    purpose: VerificationPurpose,
) -> PendingWrite:
    if contract.config.owned_sqlite_insert and purpose != "owned_attempt":
        raise VerifierError("the configured SQLite writer requires begin_owned_write")

    if task_record_sha256 is not None and (
        not isinstance(task_record_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", task_record_sha256) is None
    ):
        raise VerifierError("task_record_sha256 must be 64 lowercase hex digits")
    if task_record_sha256 is not None and ledger is None:
        raise VerifierError("task_record_sha256 requires a write ledger")
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
    unsafe = contract.null_unsafe_params()
    read_kind = str(contract.config.verifier.kind).replace("-", "_")
    screen = READ_PATHS.get(read_kind) == "screen"
    nulls = sorted(n for n in names if values[n] is None and (screen or n in unsafe))
    if screen and nulls:
        raise VerifierError(
            "a null value can't be read back from the screen (an empty field "
            "reads as text): " + ", ".join(nulls)
        )
    if nulls:
        raise VerifierError(
            "a null value can't be bound for: "
            + ", ".join(f"{n} (used in {unsafe[n]})" for n in nulls)
            + "; a null binds only in an effect's match, new_records or value"
        )
    from openadapt_flow.runtime.effects.effect import plain_number

    for name, value in values.items():
        try:
            plain_number(value)
        except ValueError as exc:
            # Compared exactly, a number this large can't be read back.
            raise VerifierError(f"parameter {name!r}: {exc}") from None
    try:
        led = _resolve_ledger(ledger)
    except LedgerError as exc:
        raise WriteHeld(f"the write ledger can't be used ({exc})") from exc
    write_id = f"write-{secrets.token_hex(12)}"
    if led is None:
        folder = _new_folder(Path(workdir or DEFAULT_WORKDIR), write_id)
        return _capture(
            contract,
            params,
            actor,
            write_id,
            folder,
            authorization_ref,
            declared_by,
            read_provider=read_provider,
            purpose=purpose,
        )
    try:
        key = led.key_for(contract.identity, values, write_key)
        values_key = led.key_for(contract.identity, values, None)
        shape_key = led.key_for(contract.shape, values, write_key)
        lease = led.backend.lease(key)
    except LedgerError as exc:
        raise WriteHeld(f"the write ledger can't be used ({exc})") from exc
    if lease is None:
        raise WriteHeld(
            "this write is in flight in another worker; wait for its result",
            key=key,
        )
    try:
        _settle(
            led,
            key,
            contract,
            params,
            actor,
            read_provider=read_provider,
            purpose=purpose,
        )
        _hold_elsewhere(led, key, shape_key)
        if any(v is None for v in values.values()):
            _hold_legacy_null(led, contract, values, write_key)
        folder = (
            _new_folder(Path(workdir), write_id)
            if workdir is not None
            else led.backend.attempt_dir(write_id)
        )
        started = time.time()  # before the baseline: this attempt's window opens
        pending = _capture(
            contract,
            params,
            actor,
            write_id,
            folder,
            authorization_ref,
            declared_by,
            read_provider=read_provider,
            purpose=purpose,
            ledger_owned=True,
        )
        pending.started = started
        _write_private(
            folder / "attempt.json",
            {"values": values, "authorization_ref": pending.authorization_ref},
        )
        owned_fields = {}
        if pending._owned_reference is not None:
            _, execution_id, digest = pending._owned_reference
            owned_fields = {
                "purpose": pending.purpose,
                "owned_baseline_execution_id": execution_id,
                "owned_baseline_sha256": digest,
            }
        led.begun(
            key,
            write_id,
            folder=str(folder),
            contract=contract.identity,
            values_key=values_key,
            store=contract.store,
            shape_key=shape_key,
            t0=started,
            records=sum(int(e.expected_count or 1) for e in contract.config.effects),
            actor_kind=actor.kind,
            actor_id=actor.id,
            write_key_bound=write_key is not None,
            authorized=pending.authorized,
            declared_by=declared_by
            if isinstance(declared_by, str)
            else {str(k): str(v) for k, v in declared_by.items()},
            **(
                {"task_record_sha256": task_record_sha256}
                if task_record_sha256 is not None
                else {}
            ),
            **owned_fields,
        )
    except LedgerError as exc:
        lease.release()
        raise WriteHeld(f"the write ledger can't be used ({exc})", key=key) from exc
    except BaseException:
        lease.release()
        raise
    pending.ledger = led
    pending.write_key = key
    pending.values_key = values_key
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
    declared_by: DeclaredBy,
    *,
    read_provider: Optional[CompleteReadProvider] = None,
    purpose: VerificationPurpose,
    ledger_owned: bool = False,
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
        _read_provider=read_provider,
        purpose=purpose,
    )
    try:
        payload = capture_baseline(
            contract.config,
            pending.params,
            pending.baseline_path,
            read_provider=read_provider,
            purpose=pending.purpose,
            _owned_dispatch_owner=write_id
            if ledger_owned and purpose == "owned_attempt"
            else None,
        )
        pending._owned_reference = _owned_baseline_reference(
            payload, bytes.fromhex(payload["salt"])
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
        raise BaselineUnreadable(_frame_db_unread(state))
    if contract.config.frame is not None and contract.config.frame.history is not None:
        from openadapt_flow.runtime.effects.history import DETAIL, checkpoint_problem

        if (
            checkpoint_problem(
                (state.get("detail") or {}).get(DETAIL), contract.config.frame.history
            )
            is not None
        ):
            raise BaselineUnreadable(
                "complete change history could not be read before the write"
            )
    return pending


def _sql_history_coverage(contract: Contract) -> Optional[dict[str, str]]:
    """The model and actual frame bound by a current SQL-history proof."""
    spec = contract.config.frame
    if spec is None:
        return None
    history = spec.history
    if history is None:
        return None
    kinds = {feed.read.get("kind") for feed in history.feeds.values()}
    if not kinds <= {"sql", "fhir"}:
        raise ValueError("history feed kind is unresolved")
    if "sql" not in kinds:
        return None
    from openadapt_flow.runtime.effects.history import SQL_INVENTORY

    return {"model": SQL_INVENTORY, "frame_sha256": spec.digest()}


def _settle(
    led: Ledger,
    key: str,
    contract: Contract,
    params: Mapping[str, Any],
    actor: Actor,
    *,
    read_provider: Optional[CompleteReadProvider] = None,
    purpose: VerificationPurpose,
) -> None:
    """With the lease held: resolve the key's earlier attempt, or raise."""

    attempt = led.latest(key)
    if (
        attempt is not None
        and led.state(attempt) != "free"
        and attempt.begun.get("purpose", "observation") != purpose
    ):
        raise WriteHeld(
            "the earlier attempt was recorded for a different verification purpose",
            key=key,
        )
    if attempt is not None and attempt.state in ("open", "uncertain"):
        # Open with a free lease: its owner died between the send and the
        # read-back. Read back before anything is sent.
        _reconcile(led, key, attempt, contract, params, read_provider=read_provider)
        attempt = led.latest(key)
    state = led.state(attempt)
    if state == "free":
        return
    if state == "undelivered":
        assert attempt is not None and attempt.resolved is not None

        def _require_history_proof(seen: Any) -> None:
            if seen.rechecked is None:
                return
            coverage = _sql_history_coverage(contract)
            if (
                coverage is not None
                and seen.rechecked.get("history_coverage") != coverage
            ):
                raise WriteHeld(
                    "the earlier SQL history proof lacks the current retained-event inventory; a person must reconcile it",
                    key=key,
                )

        # Refuse before journaling a handback, so a held write's journal
        # stays exactly as it was.
        _require_history_proof(attempt)
        # The lifecycle lease does not exclude late rechecks. Linearize the
        # handback against their settlements even if an earlier reply was
        # already delivered, then use that accepted snapshot.
        attempt = led.delivered(
            key, attempt.attempt_id, expected_revision=attempt.revision
        )
        assert attempt.resolved is not None
        result = dict(attempt.resolved.get("result") or {})
        # A legacy saved projection is not proof of retry permission.
        result["retry_safe"] = False
        result["not_sent_again"] = True
        if attempt.rechecked is not None:
            _require_history_proof(attempt)
            # A recheck confirmed it since: no longer provisional, and no
            # recheck is due.
            result["provisional"] = False
            result["final"] = True
            result["recheck_due_at"] = None
            result["rechecked_at"] = attempt.rechecked.get("at")
        raise AlreadyWritten(
            (
                "this write was already made: the store holds it, though it "
                "wasn't confirmed; its result is attached, and it was not sent "
                "again"
                if result.get("landed")
                else "this write was already made and checked; its result is "
                "attached, and it was not sent again"
            ),
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


def _hold_legacy_null(
    led: Ledger,
    contract: Contract,
    values: Mapping[str, Any],
    write_key: Optional[str],
) -> None:
    """Hold a write with a null while any attempt keyed the old way stands.

    Ledgers once keyed ``None`` as ``""``, so an attempt under that key may
    be this write (made before the upgrade) or a ``""`` write, and nothing
    here tells them apart. So no legacy attempt's result is ever handed
    back: every state but ``free`` holds the write for a person, whatever
    the attempt resolved to.
    """

    for digest in (contract.identity, contract.shape):
        legacy = led.key_for(digest, values, write_key, legacy_null=True)
        state = led.state(led.latest(legacy))
        if state == "free":
            continue
        if state in ("open", "uncertain", "held"):
            clear = (
                "A person checks the store. If this write is not there, run "
                f"'openadapt verify ledger release {legacy} --by NAME --reason "
                "TEXT --found absent' and retry: it is sent. If it is there, "
                "don't retry it; '--found landed' records that, and this null "
                "write stays held"
            )
        else:
            clear = (
                "That attempt is in the store (landed or confirmed) and its "
                "result hasn't been handed back, so 'openadapt verify ledger "
                "release' can't clear it (a known limit). Don't retry this "
                "write; a person checks the store and repairs it by hand if "
                "the field must be null"
            )
        raise WriteHeld(
            "an earlier attempt under this write's old ledger key (from before "
            'a null was told apart from "") may be this write or a "" write; '
            f"it is held, never handed back. {clear}",
            key=legacy,
        )


def _hold_elsewhere(led: Ledger, key: str, shape_key: str) -> None:
    """Hold when the same write, under another store address (an edited
    contract, an alias), is open or unresolved: a hold beats a resend."""

    for other, begun, _resolved in led.attempts():
        if other == key or begun.get("shape_key") != shape_key:
            continue
        attempt = led.latest(other)
        if attempt is None or attempt.attempt_id != begun.get("attempt"):
            continue
        if led.state(attempt) != "free":
            raise WriteHeld(
                "the same write, under another address for the store, is in "
                "flight, unresolved or already made; a person checks which "
                "store it went to (openadapt verify ledger list)",
                key=key,
            )


def _reconcile(
    led: Ledger,
    key: str,
    attempt: Attempt,
    contract: Contract,
    params: Optional[Mapping[str, Any]] = None,
    *,
    read_provider: Optional[CompleteReadProvider] = None,
) -> None:
    """Resolve ``attempt`` by reading back. The caller holds the lease.

    The attempt is judged against its own baseline, so a record that was
    there before it is never taken for its write. A baseline a check already
    used is copied for this read: it is the same write, and the lease keeps
    any other attempt of it from being sent meanwhile. Past the contract's
    window the baseline proves nothing, and the attempt stays uncertain.
    """

    folder = attempt.folder
    baseline = folder / "baseline.json"
    try:
        saved = json.loads((folder / "attempt.json").read_text(encoding="utf-8"))
        payload = json.loads(baseline.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # nothing to judge with: stays as it is, for a person
    purpose = attempt.begun.get("purpose", "observation")
    if (
        not isinstance(payload, dict)
        or purpose not in ("observation", "owned_attempt")
        or (payload.get("purpose", "observation") != purpose)
    ):
        return  # do not reinterpret another attempt or promote an unbound read
    if contract.config.owned_sqlite_insert and purpose != "owned_attempt":
        return  # a historical observational attempt cannot acquire native authority
    reference = _attempt_reference(attempt)
    if purpose == "owned_attempt" and reference is None:
        return  # the original owned baseline is not bound in the journal
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
        declared_by=attempt.begun.get("declared_by") or "agent",
        write_key=key,
        _read_provider=read_provider,
        purpose=purpose,
        _owned_reference=reference,
    )
    result = _judge(
        pending,
        write_attempted=True,
        source=None,
        seal=True,
        remediates=f"sha256:{previous}" if previous else None,
    )
    result = _check_overlap(
        led,
        key,
        attempt.attempt_id,
        str(attempt.begun.get("values_key") or ""),
        str(attempt.begun.get("store") or ""),
        float(attempt.begun.get("t0") or 0.0),
        result,
    )
    # Preserve the fresh reconciliation observation at the configured age,
    # but elapsed time and absence grant no dispatch or retry permission.
    settled_observation = (
        result.outcome is Outcome.UNCERTAIN
        and result.observed == "absent"
        and led.settle_s is not None
        and _age_s(attempt) >= led.settle_s
    )
    if (
        settled_observation
        or result.outcome is not Outcome.UNCERTAIN
        or attempt.resolved is None
        or _landed(result)
    ):
        _record(
            led,
            key,
            attempt.attempt_id,
            result,
            delivered=False,
            via="reconcile",
            write_key_bound=bool(attempt.begun.get("write_key_bound")),
            folder=folder,
            expected_revision=attempt.revision,
        )


def _age_s(attempt: Attempt) -> float:
    import calendar

    try:
        began = calendar.timegm(
            time.strptime(str(attempt.begun.get("at")), "%Y-%m-%dT%H:%M:%SZ")
        )
    except ValueError:
        return 0.0
    return time.time() - began


def verify_write(
    pending: Union[PendingWrite, Contract],
    before: Optional["Snapshot"] = None,
    *,
    write_attempted: bool = True,
    source: Optional[Source] = None,
    seal: bool = True,
    delivered: bool = True,
    params: Optional[Mapping[str, object]] = None,
    actor: Optional[Actor] = None,
    authorization_ref: str = "verify_write",
    write_receipt: bool = False,
) -> WriteResult:
    """Read the system of record now and judge the write against the
    baseline :func:`begin_write` took. Never resends the write.

    Args:
        pending: From :func:`begin_write` or :func:`begin_owned_write`. Single use.
        write_attempted: False records the caller's claim that the write
            was never sent (a stop before the save). It does not establish
            retry safety: the attempt stays held without complete dispatch
            and channel evidence. When delivery is unknown, leave it True;
            an absent read then remains ``UNCERTAIN``.
        source: The written values' provenance and documents, to compare the
            saved record with its source.
        seal: Sign a confirmed receipt with this computer's Seal key.
        delivered: False when the reply won't reach the caller (a
            cancelled tool call, a timeout); the next identical call then
            raises :class:`AlreadyWritten` with this result instead of a
            resend. The caller decides this, not the verifier.
        write_receipt: Sign the result, with its scope, as a portable write
            receipt (any outcome; see :func:`issue_write_receipt`).

    Deprecated form (#1588): ``verify_write(contract, before, params=...,
    actor=...)`` with a :func:`capture` snapshot judges the write the same
    way, without the ledger; ``authorization_ref`` belongs to it only.
    """

    if isinstance(pending, Contract):
        if before is None or params is None or actor is None:
            raise VerifierError(
                "verify_write(contract, before, params=..., actor=...) needs all three"
            )
        return _verify_snapshot(
            pending,
            before,
            params=params,
            actor=actor,
            source=source,
            authorization_ref=authorization_ref,
            write_attempted=write_attempted,
        )
    if before is not None or params is not None or actor is not None:
        raise VerifierError(
            "verify_write(pending) takes no snapshot, params or actor: "
            "begin_write bound them"
        )
    if pending.used:
        raise VerifierError("this write was already checked; begin a new one")
    pending.used = True
    # Without the lease (released), another worker may be reconciling this
    # attempt: judge, but leave the ledger to it.
    holds_lease = pending.ledger is not None and pending._lease is not None
    try:
        before_judge = (
            pending.ledger.latest(pending.write_key)
            if pending.ledger is not None
            else None
        )
        same_attempt = (
            before_judge is not None and before_judge.attempt_id == pending.write_id
        )
        purpose_changed = same_attempt and (
            before_judge is not None
            and before_judge.begun.get("purpose", "observation") != pending.purpose
        )
        if purpose_changed or (
            pending.ledger is not None
            and pending.purpose == "owned_attempt"
            and (
                not same_attempt
                or before_judge is None
                or pending._owned_reference is None
                or _attempt_reference(before_judge) != pending._owned_reference
            )
        ):
            raise WriteHeld(
                "the write attempt or its verification purpose changed",
                key=pending.write_key,
            )
        result = _judge(
            pending, write_attempted=write_attempted, source=source, seal=seal
        )
        if holds_lease and pending.ledger is not None:
            result = _check_overlap(
                pending.ledger,
                pending.write_key,
                pending.write_id,
                pending.values_key,
                pending.contract.store,
                pending.started,
                result,
            )
            result = _record(
                pending.ledger,
                pending.write_key,
                pending.write_id,
                result,
                delivered=delivered,
                via="verify_write",
                write_key_bound=pending.write_key_bound,
                folder=pending.workdir,
                expected_revision=before_judge.revision
                if before_judge is not None
                else -1,
            )
    finally:
        pending.release()
    if pending.ledger is not None:
        import dataclasses

        result = dataclasses.replace(result, ledger=pending.ledger)
    if write_receipt:
        result = issue_write_receipt(result)
    return result


def issue_write_receipt(
    result: WriteResult, out: Optional[Path] = None, **kwargs: Any
) -> WriteResult:
    """Sign ``result`` as a write receipt (``docs/WRITE_RECEIPTS.md``) next
    to its verify-run receipt, or under ``OPENADAPT_RECEIPT_HOME``. Returns
    the result with ``write_receipt`` set; the outcome never changes, and a
    receipt that would claim more than the scope is not issued."""

    import dataclasses

    from openadapt_flow import write_receipt as wr
    from openadapt_flow.receipt_log import receipt_home

    if out is None:
        folder = (
            Path(result.receipt_path).parent
            if result.receipt_path
            else receipt_home() / "issued"
        )
        out = folder / f"{result.write_id or 'write'}.receipt.cbor"
    try:
        issued = wr.issue_for_result(result, out, **kwargs)
    except wr.ReceiptError as exc:
        return dataclasses.replace(result, write_receipt=f"not issued: {exc}")
    except Exception as exc:  # noqa: BLE001 - say why, never fail the write
        return dataclasses.replace(
            result, write_receipt=f"not issued: {type(exc).__name__}"
        )
    return dataclasses.replace(
        result, write_receipt="issued", write_receipt_path=str(issued.path)
    )


def issue_recheck_receipt(
    result: WriteResult, late: "RecheckResult", out: Optional[Path] = None
) -> tuple[str, Optional[str]]:
    """Sign a recheck's verdict as a write receipt that supersedes the
    CONFIRMED's (``result.write_receipt_path``). Returns ``("issued",
    path)`` or ``("not issued: <why>", None)``; never raises."""

    import secrets

    from openadapt_flow import write_receipt as wr
    from openadapt_flow.receipt_log import receipt_id

    first = Path(result.write_receipt_path) if result.write_receipt_path else None
    if out is None:
        folder = first.parent if first else Path(result.receipt_path or ".").parent
        tag = secrets.token_hex(4)  # a write may be rechecked more than once
        out = folder / f"{result.write_id or 'write'}.recheck-{tag}.receipt.cbor"
    try:
        supersedes = receipt_id(first.read_bytes()) if first else None
        issued = wr.issue_for_recheck(result, late, out, supersedes=supersedes)
    except wr.ReceiptError as exc:
        return f"not issued: {exc}", None
    except Exception as exc:  # noqa: BLE001 - say why, never change the verdict
        return f"not issued: {type(exc).__name__}", None
    return "issued", str(issued.path)


def _record(
    led: Ledger,
    key: str,
    attempt_id: str,
    result: WriteResult,
    *,
    delivered: bool,
    via: str,
    write_key_bound: bool,
    folder: Optional[Path] = None,
    expected_revision: Optional[int] = None,
) -> WriteResult:
    attempt = led.resolved(
        key,
        attempt_id,
        outcome=result.outcome.value,
        retry_safe=result.retry_safe,
        landed=result.landed,
        review=result.review,
        delivered=delivered,
        via=via,
        write_key_bound=write_key_bound,
        receipt_sha256=result.receipt_sha256 or None,
        result=result.to_dict(),
        expected_revision=expected_revision,
        cleanup=folder if _landed(result) else None,
    )
    if (
        attempt.attempt_id != attempt_id
        or not attempt.events
        or attempt.events[-1].get("applied") is False
    ):
        return _downgrade(
            result, "the ledger attempt changed while the write was checked"
        )
    return result


def _landed(result: WriteResult) -> bool:
    """Resolved for good: nothing will read this attempt back again."""

    return result.outcome is not Outcome.UNCERTAIN or result.landed or result.retry_safe


def _downgrade(
    result: WriteResult, reason: str, *, landed: bool = False, review: bool = False
) -> WriteResult:
    import dataclasses

    if result.recheck is not None and result.outcome is Outcome.CONFIRMED:
        # Its receipt says verified, but the write is no longer CONFIRMED: a
        # recheck of it must not say "the earlier verdict still stands". It
        # still excuses its own window for later writes (its frame held).
        try:
            _withdraw_recheck(result.recheck.payload)
        except VerifierError:
            # verify_write never raises: un-issue the state instead, so a
            # recheck of it is UNCERTAIN ("not issued") all the same.
            try:
                (
                    _issued_dir("rechecks") / _state_digest(result.recheck.payload)
                ).unlink()
            except (OSError, VerifierError):
                pass
    return dataclasses.replace(
        result,
        outcome=Outcome.UNCERTAIN,
        reasons=[*result.reasons, reason],
        retry_safe=False,
        landed=landed,
        review=review,
        recheck_due_at=None,
    )


def _overlapping(
    led: Ledger, own_key: str, since: float, until: float, window_s: float
) -> list[tuple[str, dict[str, Any], Optional[dict[str, Any]]]]:
    """Attempts of other keys whose write could have landed between
    ``since`` (this attempt's baseline read began) and ``until`` (its
    read-back ended). The same key's earlier attempts resolved before this
    one began: its lease orders them. An attempt still open counts while its
    owner is alive, or for ``window_s`` after it began (its owner died, and
    a write it sent may still land); a released one is closed."""

    out: list[tuple[str, dict[str, Any], Optional[dict[str, Any]]]] = []
    for key, begun, resolved in led.attempts():
        began = float(begun.get("t0") or begun.get("t") or 0.0)
        if key == own_key or began > until:
            continue
        if resolved is not None and resolved.get("event") == "released":
            continue  # released before any outcome: a person found nothing sent
        if resolved is not None:
            if float(resolved.get("t") or until) >= since:
                out.append((key, begun, resolved))
        elif began >= since - window_s or _live(led, key):
            out.append((key, begun, resolved))
    return out


def _live(led: Ledger, key: str) -> bool:
    """Another caller holds ``key``'s lease right now."""

    try:
        lease = led.backend.lease(key)
    except LedgerError:
        return True
    if lease is None:
        return True
    lease.release()
    return False


def _check_overlap(
    led: Ledger,
    key: str,
    attempt_id: str,
    values_key: str,
    store: str,
    since: float,
    result: WriteResult,
) -> WriteResult:
    """Judge a result against the other attempts open during its window.

    - Same values: one record can't prove two writes, so a ``CONFIRMED`` is
      ``UNCERTAIN`` for a person (it may be the other attempt's record, or
      this attempt's send may have stored nothing).
    - A frame breach of only added records, no more than other attempts on
      the same store wrote in the window (read back as landed, or live
      now): the declared write landed and the extras are most likely
      theirs. ``UNCERTAIN`` with ``landed`` (never resent, not held) and
      ``review`` (a person looks, since a count can't name the records).
    """

    window_s = float(result.scope.window.max_execution_window_s or 3600)
    others = _overlapping(led, key, since, time.time(), window_s)
    if result.outcome is Outcome.CONFIRMED:
        if values_key and any(
            b.get("values_key") == values_key for _k, b, _r in others
        ):
            return _downgrade(
                result,
                "another attempt with the same values was open during this "
                "write, so the record may be that write; a person checks "
                "which one landed",
            )
        return result
    if (
        result.outcome is not Outcome.REFUTED
        or result.checked.get("delta") != "held"
        or (result.evidence.get("write_attribution") or {}).get("purpose")
        == "owned_attempt"
    ):
        return result
    if result.checked.get("source") == "refuted":
        return result
    frame = result.evidence.get("frame") or {}
    kinds = set(frame.get("collateral_kinds") or [])
    if frame.get("status") != "refuted" or not kinds or kinds - {"added"}:
        return result
    written = 0
    for key, begun, resolved in others:
        if not store or begun.get("store") != store:
            continue  # another system of record can't explain this one's rows
        if resolved is not None:
            if resolved.get("outcome") == "CONFIRMED" or resolved.get("landed"):
                written += int(begun.get("records") or 1)
        elif _live(led, key):
            written += int(begun.get("records") or 1)
    if not written or int(frame.get("collateral_count") or 0) > written:
        return result
    return _downgrade(
        result,
        "the declared records landed; the records added beside them could be "
        "other writes made on the same store in the same window, so this "
        "write isn't confirmed or sent again, and a person checks the extras",
        landed=True,
        review=True,
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
    post_state: dict[str, Any] = {}
    try:
        baseline = json.loads(pending.baseline_path.read_text(encoding="utf-8"))
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
            post_state_out=post_state,
            read_provider=pending._read_provider,
            purpose=pending.purpose,
            _owned_reference=pending._owned_reference,
        )
        path: Optional[str] = str(write_result(doc, pending.workdir))
    except Exception as exc:  # noqa: BLE001 - unreadable store, timeout, used baseline
        reasons = [
            "the system of record could not be read back "
            f"({type(exc).__name__}); the write may still land"
        ]
        if isinstance(exc, VerifyRunError):
            code = (
                exc.validation_reason if type(exc) is VerifyRunError else "unclassified"
            )
            label = (
                "unclassified"
                if code == "unclassified"
                else f"receipt_validation.{code}"
            )
            reasons.append(f"verification diagnostic: {label}")
        return WriteResult(
            outcome=Outcome.UNCERTAIN,
            scope=_scope(pending.contract, {}),
            reasons=reasons,
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
    state = _issue_state(baseline, doc, post_state, pending.params)
    if state is not None:
        state.path = state.save(pending.workdir / "recheck.json")
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
        recheck=state,
        recheck_due_at=receipt.get("recheck_due_at"),
        **base,
    )


def _issue_state(
    baseline: Mapping[str, Any],
    doc: Mapping[str, Any],
    post_state: Mapping[str, Any],
    params: Mapping[str, Any],
) -> Optional["RecheckState"]:
    """The run's recheck state, recorded as issued here, or None when its
    evidence binds no post-save state."""

    payload = recheck_state(baseline, doc, post_state)
    if payload is None:
        return None
    _issue_recheck(payload)
    return RecheckState(payload, params=dict(params))


def classify(
    receipt_outcome: str, checked: Mapping[str, Any], evidence: Mapping[str, Any]
) -> tuple[Outcome, bool]:
    """A verify-run receipt's outcome as (:class:`Outcome`, retry_safe)."""

    if receipt_outcome == "verified":
        return Outcome.CONFIRMED, False
    if receipt_outcome == "halted_before_effect":
        # The receipt preserves the caller's no-send observation. It does
        # not supply durable dispatch facts or complete channel coverage.
        return Outcome.REFUTED, False
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
    schedule = (evidence.get("frame") or {}).get(EVIDENCE_KEY) or {}
    due = schedule.get("due_at") if isinstance(schedule, Mapping) else None
    not_covered = [
        f"writes that land after read_at until the recheck due at {due} "
        "(the result is provisional until it runs), and after that recheck"
        if due
        else "writes that land after read_at (delayed or queued writes)",
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


#: ``frame.versions`` problems (``runtime/effects/versions.py``), as reasons.
_VERSION_REASONS = {
    "another_author": "lost update: another user saved a version of the target "
    "inside the window, so the agent's save may have overwritten it",
    "extra_steps": "lost update: the target moved more than one version in the "
    "window, and only one save is the agent's",
    "went_back": "the target's version went back or was replaced in the window",
    "no_author": "a new version of the target names no author, so it can't be "
    "told from another user's save",
    "not_a_number": "the target's versions aren't whole numbers and no author "
    "is read, so the saves in the window can't be counted",
    "missing": "the target existed before the write but has no version to compare",
    "no_target_id": "a target record has no field naming it in the versions read",
    "unreadable": "the target's versions could not be read, before or after the write",
    "self_unresolved": "frame.versions.self names a parameter the run did not supply",
    "null_version": "a version of the target reads as null, so saves can't be told apart",
    "duplicate_version": "two rows of the target's versions hold the same version, so a "
    "save could hide behind another",
    "no_new_version": "the target changed in the window but no new version of it was read",
    "history_changed": "a version of the target read before the write is gone after it, "
    "so a save in the window could be hidden",
}


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
    versions = frame.get("versions") or {}
    problem = versions.get("problem") if isinstance(versions, Mapping) else None
    if problem:
        out.append(
            _VERSION_REASONS.get(str(problem), f"the target's versions: {problem}")
        )
    if frame.get("status") == "refuted" and frame.get("collateral_count", 0):
        kinds = ", ".join(frame.get("collateral_kinds") or [])
        out.append(
            f"{frame.get('collateral_count', 0)} record(s) outside the declared "
            f"write moved ({kinds}); "
            f"{frame.get('unattributed_count', 0)} of them no actor explains"
        )
    elif frame.get("status") == "not_checked" and not problem:
        database = frame.get("database") or {}
        if database.get("read_status") == "too_large":
            out.append(
                "frame too large: row read budget of "
                f"{database.get('read_row_budget')} exceeded; the frame is not checked"
            )
        else:
            out.append(
                "what else changed could not be read, so the frame is not checked"
            )
    source = evidence.get("source") or {}
    if isinstance(source, Mapping) and source.get("status") == "refuted":
        fields = ", ".join(source.get("mismatched_fields") or [])
        out.append(f"the saved record differs from its source in {fields}")
    if receipt_outcome == "rejected_policy":
        out.append("the write's approver is its own executor")
    if (evidence.get("write_attribution") or {}).get("status") == "unattributed":
        out.append("the store did not bind a committed write to this attempt")
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


# The deprecated #1588 form: capture() and verify_write(contract, before, ...).
# Kept as #1588 shipped it, so a snapshot verifies once and only under the
# state directory it was captured in; results are the one WriteResult.


@dataclass
class Snapshot:
    """The system of record before the write. Private and single-use.

    It holds raw records and the run salt, so it stays on this machine:
    :meth:`save` writes it with mode 0600. One snapshot verifies at most one
    write, as verify-run's baseline does.
    """

    payload: dict[str, Any] = field(repr=False)
    used: bool = False
    #: The file :meth:`load` read, if any (for the caller's reference).
    path: Optional[Path] = field(default=None, repr=False)

    def save(self, path: str | Path) -> Path:
        out = Path(path)
        _write_private(out, self.payload)
        return out

    @classmethod
    def load(cls, path: str | Path) -> "Snapshot":
        """Read a snapshot :meth:`save` wrote. Its contents as read now are
        what's judged; it verifies only where :func:`capture` recorded it."""

        source = Path(path)
        return cls(json.loads(source.read_text(encoding="utf-8")), path=source)


#: The salt of every snapshot this process has used. Each capture draws a
#: fresh salt, so a copy or a reload of a used snapshot is refused too.
_CONSUMED: set[str] = set()
_CONSUMED_LOCK = threading.Lock()
#: Where :func:`capture` records each snapshot it issues, as an empty file
#: named by its salt's digest; :func:`verify_write` removes it, and only one
#: caller can. A snapshot not issued here (another HOME, host or container, a
#: wiped cache, a copy whose record is gone) is refused, never replayed. Set
#: ``OPENADAPT_VERIFIER_STATE_DIR`` to give every worker one directory.
USED_DIR: Optional[Path] = None


def _issued_dir(name: str = "issued") -> Path:
    root = USED_DIR or Path(
        os.environ.get("OPENADAPT_VERIFIER_STATE_DIR")
        or Path.home() / ".cache" / "openadapt" / "verifier"
    )
    issued = Path(root) / name
    try:
        issued.mkdir(mode=0o700, parents=True, exist_ok=True)
        Path(root).chmod(0o700)  # created above, or already this user's own
    except OSError as exc:
        raise VerifierError(f"can't keep the snapshot record: {exc}") from exc
    for path in (Path(root), issued):
        info = os.lstat(path)
        if (
            not os.path.isdir(path)
            or os.path.islink(path)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            # Another user could remove records (replay) or plant them.
            raise VerifierError(
                f"{path} must be a private directory owned by this user"
            )
    return issued


def _frame_db_unread(state: Mapping[str, Any]) -> str:
    why = (state.get("detail") or {}).get(FRAME_DB_ERROR)
    text = "the database could not be read for the frame"
    return f"{text} ({why})" if why else text


def _salt_digest(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise VerifierError("a snapshot's payload must be the dict capture made")
    salt = payload.get("salt")
    if not isinstance(salt, str) or not salt:
        raise VerifierError("the snapshot has no salt; capture again")
    try:
        canonical = bytes.fromhex(salt).hex()  # one name per salt, any case
    except ValueError as exc:
        raise VerifierError("the snapshot's salt is not hex") from exc
    return canonical, hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _issue(payload: Any) -> None:
    """Record a new snapshot as issued (see :data:`USED_DIR`)."""

    _salt, digest = _salt_digest(payload)
    try:
        fd = os.open(
            _issued_dir() / digest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except OSError as exc:
        raise VerifierError(f"can't record the snapshot: {exc}") from exc
    os.close(fd)


def _consume(payload: Any) -> None:
    """Use ``payload``'s snapshot, once: remove its issued record."""

    salt, digest = _salt_digest(payload)
    with _CONSUMED_LOCK:
        if salt in _CONSUMED:
            raise VerifierError("this snapshot was already used; capture again")
        _CONSUMED.add(salt)
    try:
        os.unlink(_issued_dir() / digest)
    except FileNotFoundError:
        raise VerifierError(
            "this snapshot was already used, or wasn't captured under this "
            "state directory; capture again"
        ) from None
    except OSError as exc:
        raise VerifierError(f"can't record the snapshot's use: {exc}") from exc


def _state_digest(payload: Any) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _issue_recheck(payload: Mapping[str, Any]) -> None:
    """Record a recheck state as issued here: an empty file named by the
    digest of its whole payload, under the state directory's ``rechecks``.
    A state is used only when its record is there, so one written by hand
    (whose salt and digests anyone holding the file could recompute) is
    never taken for a verified write's."""

    path = _issued_dir("rechecks") / _state_digest(payload)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as exc:
        raise VerifierError(f"can't record the recheck state: {exc}") from exc
    os.close(fd)


def _withdraw_recheck(payload: Mapping[str, Any]) -> None:
    """Mark an issued recheck state as not to be rechecked: the result it
    came with was downgraded from CONFIRMED."""

    path = _issued_dir("rechecks") / f"{_state_digest(payload)}.withdrawn"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as exc:
        raise VerifierError(f"can't withdraw the recheck state: {exc}") from exc
    os.close(fd)


def _recheck_withdrawn(payload: Any) -> bool:
    try:
        return (
            _issued_dir("rechecks") / f"{_state_digest(payload)}.withdrawn"
        ).exists()
    except (VerifierError, TypeError, ValueError):
        return True


def _recheck_issued(payload: Any) -> bool:
    try:
        return (_issued_dir("rechecks") / _state_digest(payload)).is_file()
    except (VerifierError, TypeError, ValueError):
        return False


def capture(contract: Contract, params: Mapping[str, object]) -> Snapshot:
    """Deprecated (#1588): read the system of record before the write.

    Use :func:`begin_write`, which also holds the write in the uncertain-write
    ledger; a snapshot doesn't, so a restart can't tell it was sent.
    """

    warnings.warn(
        "verifier.capture is deprecated; use begin_write",
        DeprecationWarning,
        stacklevel=2,
    )

    try:
        _require_verification_purpose(contract.config, "observation")
        with tempfile.TemporaryDirectory(prefix="openadapt-verify-") as tmp:
            payload = capture_baseline(
                contract.config, params, Path(tmp) / "baseline.json"
            )
    except VerifyRunError as exc:
        raise BaselineUnreadable(str(exc)) from exc
    state = payload.get("state") or {}
    if not state.get("reachable"):
        raise BaselineUnreadable("the system of record could not be read")
    database = contract.config.frame.database if contract.config.frame else None
    if database is not None and FRAME_DB_DETAIL not in (state.get("detail") or {}):
        raise BaselineUnreadable(_frame_db_unread(state))
    snapshot = Snapshot(dict(payload))
    _issue(snapshot.payload)
    return snapshot


def _verify_snapshot(
    contract: Contract,
    before: Snapshot,
    *,
    params: Mapping[str, object],
    actor: Actor,
    source: Optional[Source],
    authorization_ref: str,
    write_attempted: bool,
) -> WriteResult:
    try:
        _require_verification_purpose(contract.config, "observation")
    except VerifyRunError as exc:
        raise VerifierError(str(exc)) from exc
    run_record = actor.run_record()
    if before.used:
        raise VerifierError("this snapshot was already used; capture again")
    before.used = True
    _consume(before.payload)
    # Judge the payload whose salt was just used, never the file as it is now.
    post_state: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="openadapt-verify-") as tmp:
        try:
            doc = verify_run(
                contract.config,
                params=params,
                run_record=run_record,
                write_attempted=write_attempted,
                authorization_ref=authorization_ref,
                baseline_path=before.save(Path(tmp) / "baseline.json"),
                purpose="observation",
                provenance=None if source is None else source.provenance,
                documents=None if source is None else source.documents,
                post_state_out=post_state,
            )
        except (VerifyRunError, OSError) as exc:
            raise VerifierError(str(exc)) from exc
    receipt = dict(doc["receipt"])
    evidence = dict(doc["evidence"])
    receipt_outcome = str(receipt.get("outcome") or "")
    checked = dict(receipt.get("checked") or {})
    outcome, retry_safe = classify(receipt_outcome, checked, evidence)
    return WriteResult(
        outcome=outcome,
        scope=_scope(contract, evidence, receipt),
        checked=checked,
        reasons=_reasons(evidence, receipt_outcome),
        retry_safe=retry_safe,
        receipt_outcome=receipt_outcome,
        observed=_observed(evidence),
        receipt=receipt,
        receipt_sha256=receipt_sha256(receipt),
        evidence=evidence,
        recheck=_issue_state(before.payload, doc, post_state, params),
        recheck_due_at=receipt.get("recheck_due_at"),
    )


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
    read_provider: Optional[CompleteReadProvider] = None,
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
            _reconcile(led, key, attempt, contract, read_provider=read_provider)
            attempt = led.latest(key)
        return attempt.state if attempt is not None else None
    finally:
        lease.release()


# -- late writes: the recheck --------------------------------------------------


def recheck(
    contract: Contract,
    write: WriteResult | RecheckState,
    *,
    params: Mapping[str, object],
    actor: Actor,
    excused_by: Sequence[WriteResult | RecheckState] = (),
    wait: bool = False,
    ledger: Any = _NO_LEDGER,
    read_provider: Optional[CompleteReadProvider] = None,
) -> RecheckResult:
    """Read the whole database again and judge every change since a verified
    write's post-save read.

    Args:
        contract: The write's contract; it must set
            ``frame.database.recheck_after_s``.
        write: The verified :class:`WriteResult`, or its saved
            :class:`RecheckState`.
        params: The write's inputs, as given to :func:`verify_write`.
        actor: Who made the write.
        excused_by: Later writes under this contract by the same actor,
            each begun after this write's post-save read, of any outcome,
            so the caller can keep working while the window runs. Where its
            own frame was judged (held or refuted), a row or audit entry
            that changed in its own window (between its baseline and its
            post-save read) and still holds what that read saw is its own;
            a row matching its selector that lands later is not. A change
            that may be what refuted it gives ``UNCERTAIN``. Each must have
            been issued by :func:`verify_write` under this state directory;
            a loaded state needs its params (:meth:`RecheckState.load`).
        wait: Before the due time, sleep until it instead of raising
            :class:`RecheckNotDue`.
        ledger: The ledger the write was made under (default: the one a
            :class:`WriteResult` carries from :func:`begin_write`, else the
            default ledger; ``None``: none). When the recheck doesn't
            confirm the write, its attempt is resolved again there
            (``REFUTED`` or ``UNCERTAIN``, for review), so a retry is held
            for a person instead of being handed the earlier ``CONFIRMED``.
            A saved :class:`RecheckState` finds its attempt by the receipt's
            ``correlation_id``. When no attempt is re-resolved, ``reasons``
            says so.

    Changes the frame excuses after a save are excused here too: the app
    profile's bookkeeping, and another actor's write the audit log
    attributes. Anything else unexplained is ``REFUTED`` (late). A state
    that doesn't match its receipt, the contract, the actor or the params,
    and a database that can't be read or is still moving, give
    ``UNCERTAIN``. Raises :class:`VerifierError` when ``write`` scheduled no
    recheck or an ``excused_by`` entry doesn't qualify, and
    :class:`RecheckNotDue` before the due time.

    A write that lands after the recheck's read (``read_at``) is not seen.
    """

    late, trusted, revision = _judge_recheck(
        contract,
        write,
        params=params,
        actor=actor,
        excused_by=excused_by,
        wait=wait,
        ledger=ledger,
        read_provider=read_provider,
    )
    if late.outcome == "REFUTED" and not trusted:
        return late
    if late.recheck_owed:
        if trusted:
            try:
                _, _, _, attempt = _recheck_attempt(ledger, write)
                if attempt is not None:
                    held = _terminal_recheck(attempt, late)
                    if held is not None:
                        return held
            except Exception:  # noqa: BLE001 - the read remains owed
                pass
        return late
    if late.outcome != "CONFIRMED":
        late = _hold_after_recheck(
            ledger, write, late, trusted=trusted, expected_revision=revision
        )
    else:
        late = _mark_rechecked(
            ledger, write, late, contract, expected_revision=revision
        )
    return late


def _recheck_attempt(
    ledger: Any, write: WriteResult | RecheckState
) -> tuple[Optional[Ledger], str, str, Optional[Attempt]]:
    """Resolve only the current attempt with the write's exact journal id."""

    if isinstance(write, WriteResult):
        write_id, key = write.write_id, write.write_key
        if ledger is _NO_LEDGER and write.ledger is not None:
            ledger = write.ledger
    else:
        receipt = write.payload.get("receipt") or {}
        write_id, key = str(receipt.get("correlation_id") or ""), ""
    led = _resolve_ledger(ledger)
    if led is None or not write_id:
        return led, key, write_id, None
    if key:
        attempt = led.latest(key)
        if attempt is None or attempt.attempt_id != write_id:
            attempt = None
    else:
        found = led.find_attempt(write_id)
        key, attempt = found if found is not None else ("", None)
    return led, key, write_id, attempt


def _recheck_hold_receipt_matches(attempt: Attempt, digest: str) -> bool:
    return bool(digest) and any(
        event.get("event") == "resolved"
        and event.get("outcome") in ("CONFIRMED", "REFUTED")
        and event.get("applied") is not False
        and event.get("receipt_sha256") == digest
        for event in attempt.events
    )


def _terminal_recheck(attempt: Attempt, late: RecheckResult) -> Optional[RecheckResult]:
    reason = attempt.recheck_hold()
    if reason is None:
        return None
    if not _recheck_hold_receipt_matches(attempt, late.receipt_sha256):
        return _replace(
            late,
            outcome="UNCERTAIN",
            reasons=[
                *late.reasons,
                "the ledger hold could not be bound to this receipt",
            ],
        )
    message = {
        "prior_refutation": "the ledger already holds this write as REFUTED",
        "person_decision": "a person already settled this attempt; it remains REFUTED for recheck",
        "released_attempt": "a person already released this attempt; it remains REFUTED for recheck",
    }[reason]
    return _HeldRecheckResult(
        outcome="REFUTED",
        frame=late.frame,
        reasons=[*late.reasons, message],
        receipt_sha256=late.receipt_sha256,
        post_save_read_at=late.post_save_read_at,
        due_at=late.due_at,
        read_at=late.read_at,
        excused=late.excused,
        evidence=late.evidence,
        recheck_claim_sha256=late.recheck_claim_sha256,
        excuse_receipt_sha256s=late.excuse_receipt_sha256s,
        recheck_owed=False,
        _hold_reason=reason,
        _observed_status=late.outcome,
    )


def _recheck_journal(
    ledger: Any, write: WriteResult | RecheckState
) -> tuple[Optional[int], Optional[Attempt]]:
    """Snapshot a validated write's journal before its native read.

    Failure is a deliberately unmatchable revision, never a fresh basis.
    This is only called after saved-state validation, not for its claims.
    """
    try:
        led, _, _, attempt = _recheck_attempt(ledger, write)
        if led is None:
            return None, None
        if attempt is not None:
            return attempt.revision, attempt
    except Exception:  # noqa: BLE001 - no read can grant permission on failure
        pass
    return -1, None


def _mark_rechecked(
    ledger: Any,
    write: WriteResult | RecheckState,
    late: RecheckResult,
    contract: Contract,
    *,
    expected_revision: Optional[int] = None,
) -> RecheckResult:
    """Record on the write's ledger attempt that a recheck confirmed it, so
    ``openadapt verify ledger list`` no longer shows a recheck owed and a
    retry is handed a result that is no longer provisional.

    A journal change can invalidate this positive observation. If the
    revision changed or recording fails, return UNCERTAIN unless the same
    attempt has a bound terminal hold. A transient UNCERTAIN recheck may
    recover only the same originally
    confirmed receipt under the unchanged pre-read journal revision.
    The read facts stay intact when recording refuses.
    """

    try:
        led, key, write_id, attempt = _recheck_attempt(ledger, write)
        if led is None:
            return late
        if not write_id:
            return _replace(
                late,
                outcome="UNCERTAIN",
                reasons=[
                    *late.reasons,
                    "the write has no ledger attempt identity; hold this write by hand",
                ],
            )
        if attempt is None:
            return _replace(
                late,
                outcome="UNCERTAIN",
                reasons=[
                    *late.reasons,
                    "the ledger no longer has this attempt as its current write",
                ],
            )
        held = _terminal_recheck(attempt, late)
        if held is not None:
            return held
        prior = (attempt.resolved or {}).get("outcome")
        if prior == "UNCERTAIN":
            recovered = _recover_recheck(led, key, attempt, late, expected_revision)
            if recovered is not None:
                attempt = recovered
                held = _terminal_recheck(attempt, late)
                if held is not None:
                    return held
                expected_revision = attempt.revision
                prior = (attempt.resolved or {}).get("outcome")
        if prior != "CONFIRMED":
            # Other settlements cannot authorize a pass. Transient recovery
            # above requires the original receipt and an unchanged journal.
            return _replace(
                late,
                outcome="UNCERTAIN",
                reasons=[
                    *late.reasons,
                    f"the ledger settled this write as {prior or 'unresolved'}; "
                    "a later positive read does not change that settlement",
                ],
            )
        if (attempt.resolved or {}).get("receipt_sha256") != late.receipt_sha256:
            return _replace(
                late,
                outcome="UNCERTAIN",
                reasons=[
                    *late.reasons,
                    "the ledger confirmation names another receipt",
                ],
            )
        # Only reached after this contract's authenticated checkpoint and
        # actual recheck confirmed.
        coverage = _sql_history_coverage(contract)
        recorded = led.rechecked(
            key,
            write_id,
            outcome=late.outcome,
            read_at=late.read_at,
            receipt_sha256=late.receipt_sha256 or None,
            recheck_claim_sha256=late.recheck_claim_sha256 or None,
            excuse_receipt_sha256s=list(late.excuse_receipt_sha256s)
            if late.excuse_receipt_sha256s is not None
            else None,
            expected_revision=expected_revision,
            **({"history_coverage": coverage} if coverage is not None else {}),
        )
        if recorded.attempt_id == write_id:
            held = _terminal_recheck(recorded, late)
            if held is not None:
                return held
        if (
            recorded.attempt_id != write_id
            or not recorded.events
            or recorded.events[-1].get("applied") is False
        ):
            return _replace(
                late,
                outcome="UNCERTAIN",
                reasons=[
                    *late.reasons,
                    "the ledger did not accept this recheck; the attempt remains held",
                ],
            )
        problem = _recheck_claim_problem(recorded.events, late)
        if problem is not None:
            return _replace(late, outcome="UNCERTAIN", reasons=[*late.reasons, problem])
    except Exception:  # noqa: BLE001 - the list keeps showing it owed
        return _replace(
            late,
            outcome="UNCERTAIN",
            reasons=[
                *late.reasons,
                "the ledger could not record the recheck; hold this write by hand",
            ],
        )
    return late


def _recover_recheck(
    ledger: Ledger,
    key: str,
    attempt: Attempt,
    late: RecheckResult,
    expected_revision: Optional[int],
) -> Optional[Attempt]:
    """Recover only a transient recheck of this originally confirmed receipt.

    The pre-read journal revision binds the fresh native observation. A
    refutation or person decision is never replaced by restoring current rows.
    The restored result stays provisional until the separate rechecked event
    is accepted, so an interrupted recovery cannot claim final completion.
    """
    prior = attempt.resolved or {}
    events = [event for event in attempt.events if event.get("applied") is not False]
    if (
        type(expected_revision) is not int
        or expected_revision < 0
        or attempt.released
        or prior.get("via") != "recheck"
        or prior.get("receipt_sha256") != late.receipt_sha256
        or any(
            event.get("event") == "released"
            or event.get("via") == "person"
            or event.get("event") == "resolved"
            and event.get("outcome") == "REFUTED"
            for event in events
        )
    ):
        return None
    original = next(
        (
            event
            for event in reversed(events)
            if event.get("event") == "resolved"
            and event.get("outcome") == "CONFIRMED"
            and event.get("receipt_sha256") == late.receipt_sha256
            and isinstance(event.get("result"), Mapping)
            and event["result"].get("verified") is True
            and event["result"].get("outcome") == "CONFIRMED"
            and event["result"].get("provisional") is True
        ),
        None,
    )
    if original is None:
        return None
    recorded = ledger.resolved(
        key,
        attempt.attempt_id,
        outcome="CONFIRMED",
        retry_safe=False,
        landed=False,
        review=False,
        delivered=True,
        via="recheck",
        write_key_bound=bool(original.get("write_key_bound")),
        receipt_sha256=late.receipt_sha256,
        result=dict(original["result"]),
        expected_revision=expected_revision,
    )
    if (
        recorded.attempt_id == attempt.attempt_id
        and recorded.recheck_hold() is not None
    ):
        return recorded
    if (
        recorded.attempt_id != attempt.attempt_id
        or not recorded.events
        or recorded.events[-1].get("applied") is False
    ):
        raise LedgerError("the ledger changed during the fresh recheck")
    return recorded


def _recheck_claim_id(receipt: str, excuses: Sequence[str]) -> str:
    """One data identity, insensitive to excuse order or duplication."""

    return _state_digest(
        {"receipt_sha256": receipt, "excuse_receipt_sha256s": sorted(set(excuses))}
    )


def _recheck_claim_problem(
    events: Sequence[Mapping[str, Any]],
    late: RecheckResult,
) -> Optional[str]:
    """A historical refutation is not erased by restoring current rows.

    A new claim needs a separately validated new receipt. Journal labels
    are not native proof; missing legacy context stays unresolved, and
    this comparison never releases a workflow hold or permits a resend.
    """

    negative = [
        e
        for e in events
        if e.get("event") == "resolved"
        and e.get("outcome") == "REFUTED"
        and e.get("applied") is not False
    ]
    if not negative:
        return None

    def digest(value: Any) -> bool:
        return (
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
        )

    current = late.excuse_receipt_sha256s
    if (
        not digest(late.receipt_sha256)
        or current is None
        or late.recheck_claim_sha256 != _recheck_claim_id(late.receipt_sha256, current)
    ):
        return "the recheck has no validated receipt-bound claim context; the earlier hold remains"
    claims: set[str] = set()
    seen_receipts: set[str] = set()
    unknown_context = False
    for event in negative:
        result = event.get("result")
        if not isinstance(result, Mapping):
            return "a prior refutation has no bound claim identity; it cannot be replaced by a current read"
        receipt = result.get("receipt_sha256")
        if (
            not isinstance(receipt, str)
            or not digest(receipt)
            or receipt != event.get("receipt_sha256")
        ):
            return "a prior refutation has no bound claim identity; it cannot be replaced by a current read"
        claims.add(receipt)
        seen_receipts.add(receipt)
        refs = result.get("excuse_receipt_sha256s")
        if (
            not isinstance(refs, (list, tuple))
            or not all(digest(ref) for ref in refs)
            or list(refs) != sorted(set(refs))
            or result.get("recheck_claim_sha256") != _recheck_claim_id(receipt, refs)
        ):
            unknown_context = True
            continue
        if late.recheck_claim_sha256 == result["recheck_claim_sha256"]:
            return "this historical recheck claim was already refuted; restoring current rows cannot confirm it"
        seen_receipts.update(refs)
    if late.receipt_sha256 not in claims:
        return None  # A separately issued new original receipt is a new claim.
    if unknown_context:
        return "a prior refutation has no validated excuse context; the earlier claim remains unresolved"
    if set(current) - seen_receipts:
        return None  # A genuinely new validated later receipt binds a new claim.
    return "a fresh confirmation needs a new validated receipt, not a reordered, removed, or recycled excuse context"


def _hold_after_recheck(
    ledger: Any,
    write: WriteResult | RecheckState,
    late: RecheckResult,
    *,
    trusted: bool = True,
    expected_revision: Optional[int] = None,
) -> RecheckResult:
    """Resolve the write's ledger attempt again with the recheck's outcome,
    for review: ``REFUTED`` holds it, ``UNCERTAIN`` keeps it uncertain.
    Either way a retry is not handed the earlier ``CONFIRMED``.

    The attempt is the write's own: its ledger key and write id from a
    :class:`WriteResult`, or, for a saved :class:`RecheckState`, the
    receipt's ``correlation_id`` (the write id) looked up in the ledger.
    The ledger is the one given, else the one the write was made under.
    When no attempt is re-resolved (no ledger, no such attempt, or a newer
    attempt of the key), the result says so: the write must be held by hand.
    A saved state that failed validation (``trusted`` False) names no attempt
    that can be trusted, so its ``correlation_id`` is never looked up.
    """

    if isinstance(write, WriteResult):
        write_id, key = write.write_id, write.write_key
        digest = write.receipt_sha256 or None
        if ledger is _NO_LEDGER and write.ledger is not None:
            ledger = write.ledger
    else:
        receipt = write.payload.get("receipt") or {}
        write_id = str(receipt.get("correlation_id") or "")
        key = ""
        digest = receipt_sha256(receipt) if receipt else None

    def unheld(why: str) -> RecheckResult:
        return _replace(
            late,
            reasons=[
                *late.reasons,
                f"no ledger attempt was re-resolved ({why}); hold this write by "
                "hand so a retry isn't handed the earlier CONFIRMED",
            ],
        )

    if not trusted and not isinstance(write, WriteResult):
        # A WriteResult names its own attempt; a saved state that failed
        # validation could name another write's.
        return unheld("the saved recheck state failed validation")
    try:
        led = _resolve_ledger(ledger)
        if led is None:
            return unheld("no ledger was given")
        if not write_id:
            return unheld("the write has no write id")
        if not key:
            found = [
                k
                for k, begun, _ in led.attempts()
                if str(begun.get("attempt")) == write_id
            ]
            if len(found) != 1:
                return unheld("this ledger has no attempt with the write's id")
            key = found[0]
        attempt = led.latest(key)
        if attempt is None:
            return unheld("this ledger has no attempt with the write's id")
        if attempt.attempt_id != write_id:
            return unheld("a newer attempt of this write is in the ledger")
        if trusted:
            held = _terminal_recheck(attempt, late)
            if held is not None:
                return held
        prior = attempt.resolved or {}
        reason = "; ".join(late.reasons) or "the recheck did not confirm the write"
        recorded = led.resolved(
            key,
            write_id,
            outcome=late.outcome,
            retry_safe=False,
            landed=False,
            review=True,
            delivered=True,
            via="recheck",
            late=late.late,
            write_key_bound=bool(prior.get("write_key_bound")),
            receipt_sha256=digest,
            result={
                "verified": False,
                "outcome": late.outcome,
                "late": late.late,
                "reason": reason,
                "receipt_sha256": digest,
                "recheck_claim_sha256": late.recheck_claim_sha256 or None,
                "excuse_receipt_sha256s": list(late.excuse_receipt_sha256s)
                if late.excuse_receipt_sha256s is not None
                else None,
            },
            expected_revision=expected_revision,
        )
        if (
            recorded.attempt_id != write_id
            or not recorded.events
            or recorded.events[-1].get("applied") is False
        ):
            if trusted and recorded.attempt_id == write_id:
                held = _terminal_recheck(recorded, late)
                if held is not None:
                    return held
            return unheld(
                "a concurrent settlement kept the existing hold or changed the attempt"
            )
    except Exception as exc:  # noqa: BLE001 - the verdict stands; say so
        return _replace(
            late,
            reasons=[
                *late.reasons,
                f"the ledger could not record it ({type(exc).__name__}); hold "
                "this write by hand",
            ],
        )
    return late


#: ``recheck_problem`` sentences that fault the recheck call (the params or
#: actor it was given), not the write.
_CALL_PROBLEMS = (
    "the params don't bind the contract",
    "the params are not the ones the write was verified with",
    "the recheck state's write was made by a different actor",
    "the contract can't be bound for the recheck",
    "the recheck state was taken under a different contract",
)


def _recheck_refusal(problem: str) -> str:
    """The UNCERTAIN reason for a recheck that couldn't run. When the call
    was given other params, another actor or another contract than the
    write was made with, it says the call was wrong: the write itself may
    be fine, and stays unrechecked until a call with its own does."""

    if problem.startswith(_CALL_PROBLEMS):
        return (
            f"{problem}: the recheck call doesn't match the write (its params, "
            "actor or contract), so nothing was rechecked; this says nothing "
            "about the write itself"
        )
    return f"{problem}, so the write can't be rechecked"


def _judge_recheck(
    contract: Contract,
    write: WriteResult | RecheckState,
    *,
    params: Mapping[str, object],
    actor: Actor,
    excused_by: Sequence[WriteResult | RecheckState],
    wait: bool,
    ledger: Any,
    read_provider: Optional[CompleteReadProvider] = None,
) -> tuple[RecheckResult, bool, Optional[int]]:
    """The read result, state validation, and its pre-read journal revision."""

    state = write.recheck if isinstance(write, WriteResult) else write
    receipt = state.payload.get("receipt") if state is not None else None
    if (
        state is None
        or not isinstance(receipt, dict)
        or receipt.get("outcome") != "verified"
    ):
        raise VerifierError(
            "this write scheduled no recheck: it was not verified, or the "
            "contract schedules no history or database recheck"
        )
    run_record = actor.run_record()
    cfg = contract.config
    spec = cfg.frame
    post = state.payload.get("post_state")
    base = RecheckResult(
        outcome="UNCERTAIN",
        frame="not_checked",
        receipt_sha256=receipt_sha256(receipt),
        due_at=state.due_at,
        post_save_read_at=str(post.get("read_at") or "")
        if isinstance(post, dict)
        else "",
    )
    problem = (
        "the recheck state was not issued under this state directory"
        if not _recheck_issued(state.payload)
        else "the write's result was downgraded from CONFIRMED to UNCERTAIN"
        if _recheck_withdrawn(state.payload)
        else None
    )
    if problem is None:
        try:
            _require_recheck_purpose(cfg, receipt)
        except VerifyRunError as exc:
            # A call under the wrong purpose cannot settle the original write.
            return (
                _replace(
                    base,
                    reasons=[_recheck_refusal(str(exc))],
                    recheck_owed=True,
                ),
                False,
                None,
            )
        problem = recheck_problem(
            cfg,
            state.payload,
            params=params,
            run_record=run_record,
            read_provider=read_provider,
        )
    if problem is not None:
        return (
            _replace(base, reasons=[_recheck_refusal(problem)]),
            False,
            None,
        )
    wait_s = seconds_until(state.due_at)
    if wait_s > 0:
        if not wait:
            raise RecheckNotDue(state.due_at, wait_s)
        time.sleep(wait_s)
    excused: list[tuple[Mapping[str, Any], Mapping[str, object]]] = []
    for i, other in enumerate(excused_by):
        later = other.recheck if isinstance(other, WriteResult) else other
        if later is None:
            raise VerifierError(
                f"excused_by[{i}] has no recheck state: its database didn't "
                "settle after its save, so what it wrote can't be told apart; "
                "reconcile it before rechecking this write"
            )
        if not _recheck_issued(later.payload):
            raise VerifierError(
                f"excused_by[{i}] was not issued under this state directory"
            )
        if later.params is None:
            raise VerifierError(
                f"excused_by[{i}] has no params: load it with RecheckState.load"
                "(path, params=...)"
            )
        excused.append((later.payload, later.params))
    revision, attempt = _recheck_journal(ledger, write)
    if (receipt.get("write_attribution") or {}).get("purpose") == "owned_attempt":
        reference = _owned_baseline_reference(
            state.payload["baseline"], bytes.fromhex(state.payload["salt"])
        )
        # A person settlement can omit a digest. Only a terminal hold may
        # use the original applied receipt binding from this same attempt.
        receipt_matches = attempt is not None and (
            _recheck_hold_receipt_matches(attempt, base.receipt_sha256)
            if attempt.recheck_hold() is not None
            else (attempt.resolved or {}).get("receipt_sha256") == base.receipt_sha256
        )
        if (
            attempt is None
            or reference is None
            or _attempt_reference(attempt) != reference
            or not receipt_matches
        ):
            return (
                _replace(
                    base,
                    recheck_owed=True,
                    reasons=["the original owned journal claim could not be matched"],
                ),
                False,
                None,
            )
    try:
        verdict, read_at = recheck_run(
            cfg,
            state.payload,
            params=params,
            run_record=run_record,
            excused=excused,
            read_provider=read_provider,
        )
    except VerifyRunError as exc:
        raise VerifierError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - an unreadable store is not a pass
        return (
            _replace(
                base,
                reasons=[
                    f"the database could not be read again ({type(exc).__name__})"
                ],
            ),
            True,
            revision,
        )
    # recheck_run has now validated every later receipt, its params and
    # read contract, and its temporal window. A count or caller label is
    # insufficient; bind the exact issued receipt identities instead.
    excuse_refs = tuple(
        sorted({receipt_sha256(other["receipt"]) for other, _ in excused})
    )
    evidence = verdict.evidence(spec) if spec is not None else {}
    reasons: list[str] = []
    status = verdict.status
    if status == "held" and LATER_WRITE_DISPUTED in verdict.excused_by:
        # A change in a later write's window may be what refuted that write;
        # the harm may be this write's, so this verdict can't stand as is.
        status = "not_checked"
        reasons.append(
            "a change since the verdict lies in a later write that was not "
            "verified and may be what refuted it; reconcile both"
        )
    if status == "refuted" and LATER_WRITE_UNJUDGED in verdict.excused_by:
        # A later write whose own frame wasn't judged may have made the
        # change: the harm may not be this write's, but it isn't excused.
        status = "not_checked"
        reasons.append(
            "a change since the verdict may be a later write's whose own "
            "frame was not judged; reconcile both"
        )
    keep_owed = False
    if status == "held":
        try:
            read_was_due = parse_time(read_at) >= parse_time(state.due_at)
        except ValueError:
            read_was_due = False
        if not read_was_due:
            status = "not_checked"
            keep_owed = True
            reasons.append(
                "the recheck's read time does not establish that it was due; "
                "the write remains unrechecked"
            )
    if status == "refuted":
        kinds = ", ".join(sorted({c.change for c in verdict.collateral}))
        reasons.append(
            f"{len(verdict.collateral)} record(s) moved after the verdict that "
            f"nothing explains ({kinds}); the earlier CONFIRMED no longer stands"
        )
    elif status != "held" and not reasons:
        reasons.append(verdict.reason or "the database could not be judged again")
    return (
        _replace(
            base,
            outcome=_late_outcome(status),
            frame=verdict.status,
            reasons=reasons,
            read_at=read_at,
            excused=len(excused),
            evidence=evidence,
            recheck_claim_sha256=_recheck_claim_id(base.receipt_sha256, excuse_refs),
            excuse_receipt_sha256s=excuse_refs,
            recheck_owed=keep_owed,
        ),
        True,
        revision,
    )


def _late_outcome(status: str) -> LateOutcome:
    from openadapt_flow.runtime.effects.late_write import outcome_of

    return outcome_of(status)


def _replace(result: RecheckResult, **changes: Any) -> RecheckResult:
    import dataclasses

    return dataclasses.replace(result, **changes)


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
    read_row_budget: int = DEFAULT_READ_ROW_BUDGET,
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
    process). ``allow_tables`` are kept append-only. ``read_row_budget``
    caps data rows across tables and auxiliary queries; exceeding it refuses
    the read without returning a partial snapshot.

    ``profile`` (``openemr``) applies an app profile's bookkeeping rules, the
    same ones the frame uses (``openadapt_flow.app_profiles``); its own-row
    rules need the acting user's sign-in names (``actor_names``), whose ids
    are read in the same transaction. ``before``: a snapshot taken earlier
    with the same key, so this one keeps the plain values of the rows that
    differ from it, in memory only. Raises :class:`BaselineUnreadable`.
    """

    from openadapt_flow.verify_run import _db_connect

    if dialect not in ("sqlite", "mysql", "postgresql"):
        raise VerifierError(f"dialect {dialect!r} is not sqlite, mysql or postgresql")
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
            read_row_budget=read_row_budget,
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

    from openadapt_flow.runtime.effects.db_snapshot import (
        cell_hash,
        every_cell_hashed,
        unpack,
    )

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
        if (
            d.unread
            or a is None
            or b is None
            or a.cell_columns != b.cell_columns
            or not every_cell_hashed(a)
        ):
            # A column no cell MAC shows may have moved: no rule judges it.
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
