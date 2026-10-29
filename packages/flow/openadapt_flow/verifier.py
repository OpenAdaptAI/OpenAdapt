"""Check one write against its contract: the stable API.

    from openadapt_flow.verifier import Contract, capture, verify_write

    contract = Contract.load("verify.yaml")
    before = capture(contract, params)       # read the system of record first
    ...                                      # the agent, skill or program writes
    result = verify_write(contract, before, params=params, actor="openadapt_actor")
    if not result.verified:
        print(result.checked, result.reasons)

MCP skills, the RL reward and the registry all call this module; it wraps
``verify_run`` and checks exactly what verify-run checks:

- **delta:** the declared records were written with the declared values;
- **frame:** nothing else changed, except what the audit trail attributes to
  another user and the contract's reviewed bookkeeping tables;
- **source** (when a :class:`Source` is given): the saved values match the
  document they came from.

Fail-closed: an unreadable read or a missing or stale baseline gives
``verified=False`` with that part ``not_checked``. A failed check never
raises. :class:`VerifierError` is raised for a contract that can't be loaded
or bound (an unbound parameter), a snapshot taken for another contract, and a
snapshot that was already used. Any call uses up its snapshot: a refused one,
and one whose write is ``unconfirmed`` (reconcile, then capture again; don't
re-verify). Whoever can write a snapshot controls the verdict (its records,
and its salt, which is what single use is keyed on), so the writer being
judged must never have access to it, nor to the state directory where
captures are recorded (``OPENADAPT_VERIFIER_STATE_DIR``, default
``~/.cache/openadapt/verifier``): a snapshot verifies only under the state
directory it was captured under, so not across machines. Capture once per
write: two snapshots taken before one write each verify it, and a capture
never verified leaves its record behind. No model calls.

Writes that land later: the frame reads the database twice after the save,
``settle_s`` apart, so a write the app makes later than that (a background
job, a trigger-driven integration, a delayed side record) lands after the
verdict. With ``frame.database.recheck_after_s`` set, a verified result
carries a private :class:`RecheckState` and says when the recheck is due
(``WriteResult.recheck_due_at``, the receipt's ``recheck_due_at``)::

    result = verify_write(contract, before, params=params, actor=actor)
    result.recheck.save("recheck.json")       # private, like a snapshot
    ...                                       # the caller works on the next item
    late = recheck(contract, result, params=params, actor=actor,
                   excused_by=[next_result])  # later writes, any outcome
    if late.outcome == "REFUTED":             # changed after the verdict
        print(late.reasons)

:func:`recheck` reads the whole database again at or after the due time
and judges every change since the post-save read with the frame's rules.
Before the due time it raises :class:`RecheckNotDue` (``wait=True`` sleeps
until then instead). Its outcome is ``CONFIRMED``, ``REFUTED`` (late: the
earlier VERIFIED no longer stands) or ``UNCERTAIN`` (the database or the
state couldn't be read or doesn't match: never a pass).

The public names are ``Contract``, ``Snapshot``, ``Source``, ``capture``,
``verify_write``, ``WriteResult``, ``recheck``, ``RecheckState``,
``RecheckResult``, ``RecheckNotDue`` and ``VerifierError``
(``tests/test_verifier_api.py`` pins their signatures).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.runtime.effects.db_snapshot import Snapshot as DatabaseSnapshot
from openadapt_flow.runtime.effects.db_snapshot import TableDiff, diff, take_snapshot
from openadapt_flow.runtime.effects.late_write import (
    EVIDENCE_KEY,
    LateOutcome,
    seconds_until,
    state_problem,
)
from openadapt_flow.verify_run import (
    FRAME_DB_DETAIL,
    VerifyConfig,
    VerifyRunError,
    _write_private,
    capture_baseline,
    load_verify_config,
    parse_verify_config,
    receipt_sha256,
    recheck_problem,
    recheck_run,
    recheck_state,
    verify_run,
)

__all__ = [
    "Actor",
    "BaselineUnreadable",
    "Contract",
    "DatabaseSnapshot",
    "RecheckNotDue",
    "RecheckResult",
    "RecheckState",
    "Snapshot",
    "Source",
    "TableDiff",
    "VerifierError",
    "WriteResult",
    "capture",
    "capture_database",
    "diff_database",
    "recheck",
    "verify_write",
]

#: The receipt's executor kinds, with the names callers also use.
_KINDS = {
    "program": "flow_replay",
    "flow_replay": "flow_replay",
    "agent": "agent",
    "script": "script",
    "person": "human",
    "human": "human",
}


class VerifierError(ValueError):
    """The contract can't be loaded or bound, or a call is misused."""


class BaselineUnreadable(VerifierError):
    """The system of record can't be read before the write. Don't write:
    nothing could verify it afterwards."""


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


@dataclass(frozen=True)
class Contract:
    """A parsed ``verify.yaml`` (``openadapt.verify/v1``)."""

    config: VerifyConfig

    @classmethod
    def load(cls, path: str | Path) -> "Contract":
        try:
            return cls(load_verify_config(path))
        except VerifyRunError as exc:
            raise VerifierError(str(exc)) from exc

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> "Contract":
        try:
            return cls(parse_verify_config(dict(doc)))
        except VerifyRunError as exc:
            raise VerifierError(str(exc)) from exc


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
        return cls(json.loads(source.read_text()), path=source)


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


def _issued_dir() -> Path:
    root = USED_DIR or Path(
        os.environ.get("OPENADAPT_VERIFIER_STATE_DIR")
        or Path.home() / ".cache" / "openadapt" / "verifier"
    )
    issued = Path(root) / "issued"
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


@dataclass(frozen=True)
class Source:
    """Where the written values came from: provenance and the documents."""

    provenance: Any
    documents: Mapping[str, Any]


@dataclass(frozen=True)
class WriteResult:
    #: True only when every part checked held.
    verified: bool
    #: ``verified``; ``refuted`` (the store shows a different, extra or
    #: duplicate write: don't retry blindly); or ``unconfirmed`` (the store
    #: doesn't show the write, or couldn't be read: it may still land, so
    #: reconcile before retrying).
    status: str
    #: ``verified``, ``reconciliation_required``, ``halted_before_effect``,
    #: or another verify-run outcome: never ``verified`` on a failed check.
    outcome: str
    #: ``{"delta": ..., "frame": ..., "source": ...}``: ``held``,
    #: ``refuted`` or ``not_checked`` (``source`` is ``instruction`` when no
    #: source was given).
    checked: dict[str, str]
    #: Plain sentences, without record values.
    reasons: list[str] = field(default_factory=list)
    #: The effect receipt, its SHA-256, and its evidence, as verify-run
    #: issues them.
    receipt: dict[str, Any] = field(default_factory=dict)
    receipt_sha256: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    #: When the contract sets ``frame.database.recheck_after_s`` and the
    #: database settled after the save: the private state :func:`recheck`
    #: needs. A verified write's is what its recheck compares with; any
    #: write's can excuse its own window in an earlier write's recheck.
    recheck: Optional["RecheckState"] = field(default=None, repr=False, compare=False)
    #: When a verified write's recheck is due (whole-second UTC), as the
    #: receipt's ``recheck_due_at`` says; None when no recheck is due.
    recheck_due_at: Optional[str] = None


@dataclass
class RecheckState:
    """What a recheck compares with: the settled post-save snapshot as keyed
    MACs (no values), the audit entries seen then as keyed hashes, the run's
    salt, and the receipt and evidence it is bound to.

    Private, like a :class:`Snapshot`: it holds the salt, so :meth:`save`
    writes it with mode 0600, and the writer being judged must not be able
    to write it. ``params`` (the write's inputs) stay in memory only; a
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
    #: ``REFUTED`` (something did: the earlier VERIFIED no longer stands), or
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
    #: How many later verified writes explained their own rows.
    excused: int = 0
    #: The frame's closed evidence since the post-save read (counts and
    #: names, no values).
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        """True only when the earlier verdict still stands."""

        return self.outcome == "CONFIRMED"

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


def capture(contract: Contract, params: Mapping[str, object]) -> Snapshot:
    """Read the system of record before the write."""

    with tempfile.TemporaryDirectory(prefix="openadapt-verify-") as tmp:
        try:
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
        raise BaselineUnreadable("the database could not be read for the frame")
    snapshot = Snapshot(dict(payload))
    _issue(snapshot.payload)
    return snapshot


def _status(checked: Mapping[str, str], evidence: Mapping[str, Any]) -> str:
    """``refuted`` when the store shows something other than the declared
    write; ``unconfirmed`` when it doesn't show the write (or couldn't be
    read), so the write may still land."""

    if checked.get("frame") == "refuted" or checked.get("source") == "refuted":
        return "refuted"
    if checked.get("delta") != "refuted":
        return "unconfirmed"
    rows = (evidence.get("readback") or {}).get("effects") or []
    failed = [
        r for r in rows if isinstance(r, Mapping) and r.get("verdict") != "CONFIRMED"
    ]
    if failed and all(r.get("observed_effect") == "absent" for r in failed):
        return "unconfirmed"
    return "refuted"


def _reasons(evidence: Mapping[str, Any]) -> list[str]:
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
    return out


def verify_write(
    contract: Contract,
    before: Snapshot,
    *,
    params: Mapping[str, object],
    actor: Actor,
    source: Optional[Source] = None,
    authorization_ref: str = "verify_write",
    write_attempted: bool = True,
) -> WriteResult:
    """Read the system of record now and judge the write against ``before``.

    Args:
        contract: What the write must do (and, with a frame, must not do).
        before: :func:`capture` taken before the write. Single use.
        params: The run's inputs, which the contract's values bind.
        actor: Who wrote. Its own changes are never excused as another
            user's.
        source: The written values' provenance and documents, to compare the
            saved record with its source.
        authorization_ref: An opaque reference to what authorized the write;
            only its salted digest enters the receipt.
        write_attempted: False when the write was never tried (a stop before
            the save), so the receipt says no effect was attempted.
    """

    run_record = actor.run_record()
    if before.used:
        raise VerifierError("this snapshot was already used; capture again")
    before.used = True
    _consume(before.payload)

    post_state: dict[str, Any] = {}

    def run(path: Path) -> dict[str, Any]:
        try:
            return verify_run(
                contract.config,
                params=params,
                run_record=run_record,
                write_attempted=write_attempted,
                authorization_ref=authorization_ref,
                baseline_path=path,
                provenance=None if source is None else source.provenance,
                documents=None if source is None else source.documents,
                post_state_out=post_state,
            )
        except (VerifyRunError, OSError) as exc:
            raise VerifierError(str(exc)) from exc

    # Judge the payload whose salt was just used, never the file as it is now.
    with tempfile.TemporaryDirectory(prefix="openadapt-verify-") as tmp:
        result = run(before.save(Path(tmp) / "baseline.json"))
    receipt = dict(result["receipt"])
    evidence = dict(result["evidence"])
    outcome = str(receipt.get("terminal_outcome") or receipt.get("outcome") or "")
    checked = dict(receipt.get("checked") or {})
    verified = outcome == "verified"
    status = "verified" if verified else _status(checked, evidence)
    late = recheck_state(before.payload, result, post_state)
    state = None if late is None else RecheckState(late, params=dict(params))
    return WriteResult(
        verified=verified,
        status=status,
        outcome=outcome,
        checked=checked,
        reasons=_reasons(evidence),
        receipt=receipt,
        receipt_sha256=receipt_sha256(receipt),
        evidence=evidence,
        recheck=state,
        recheck_due_at=receipt.get("recheck_due_at"),
    )


def recheck(
    contract: Contract,
    write: WriteResult | RecheckState,
    *,
    params: Mapping[str, object],
    actor: Actor,
    excused_by: Sequence[WriteResult | RecheckState] = (),
    wait: bool = False,
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
            each begun after this write's post-save read, of any outcome.
            Each one's declared effects and side records explain its own
            rows, so the caller can keep working while the window runs. Where
            its own frame was judged (held or refuted), every row and audit
            entry in its window is its own: what it already reported against
            itself doesn't refute this write too. A loaded state needs its
            params (:meth:`RecheckState.load`).
        wait: Before the due time, sleep until it instead of raising
            :class:`RecheckNotDue`.

    Changes the frame excuses after a save are excused here too: the app
    profile's bookkeeping, and another actor's write the audit log
    attributes. Anything else unexplained is ``REFUTED`` (late). A state
    that doesn't match its receipt, the contract, the actor or the params,
    and a database that can't be read or is still moving, give
    ``UNCERTAIN``. Raises :class:`VerifierError` when ``write`` scheduled no
    recheck or an ``excused_by`` entry doesn't qualify, and
    :class:`RecheckNotDue` before the due time.
    """

    state = write.recheck if isinstance(write, WriteResult) else write
    receipt = state.payload.get("receipt") if state is not None else None
    if (
        state is None
        or not isinstance(receipt, dict)
        or receipt.get("outcome") != "verified"
    ):
        raise VerifierError(
            "this write scheduled no recheck: it was not verified, or the "
            "contract doesn't set frame.database.recheck_after_s"
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
    problem = recheck_problem(cfg, state.payload, params=params, run_record=run_record)
    if problem is not None:
        return _replace(base, reasons=[f"{problem}, so the write can't be rechecked"])
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
        if later.params is None:
            raise VerifierError(
                f"excused_by[{i}] has no params: load it with RecheckState.load"
                "(path, params=...)"
            )
        excused.append((later.payload, later.params))
    try:
        verdict, read_at = recheck_run(
            cfg, state.payload, params=params, run_record=run_record, excused=excused
        )
    except VerifyRunError as exc:
        raise VerifierError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - an unreadable store is not a pass
        return _replace(
            base,
            reasons=[f"the database could not be read again ({type(exc).__name__})"],
        )
    evidence = verdict.evidence(spec) if spec is not None else {}
    reasons: list[str] = []
    if verdict.status == "refuted":
        kinds = ", ".join(sorted({c.change for c in verdict.collateral}))
        reasons.append(
            f"{len(verdict.collateral)} record(s) moved after the verdict that "
            f"nothing explains ({kinds}); the earlier VERIFIED no longer stands"
        )
    elif verdict.status != "held":
        reasons.append(verdict.reason or "the database could not be judged again")
    return _replace(
        base,
        outcome=_late_outcome(verdict.status),
        frame=verdict.status,
        reasons=reasons,
        read_at=read_at,
        excused=len(excused),
        evidence=evidence,
    )


def _late_outcome(status: str) -> LateOutcome:
    from openadapt_flow.runtime.effects.late_write import outcome_of

    return outcome_of(status)


def _replace(result: RecheckResult, **changes: Any) -> RecheckResult:
    import dataclasses

    return dataclasses.replace(result, **changes)


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
