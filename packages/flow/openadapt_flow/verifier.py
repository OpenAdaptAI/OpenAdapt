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

Fail-closed: an unreadable read, a missing baseline or an unbound parameter
gives ``verified=False`` with that part ``not_checked``. A failed check never
raises; :class:`VerifierError` is raised only for a contract that can't be
loaded or read at all. No model calls.

The public names are ``Contract``, ``Snapshot``, ``Source``, ``capture``,
``verify_write``, ``WriteResult`` and ``VerifierError``
(``tests/test_verifier_api.py`` pins their signatures).
"""

from __future__ import annotations

import json
import secrets
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.runtime.effects.db_snapshot import Snapshot as DatabaseSnapshot
from openadapt_flow.runtime.effects.db_snapshot import TableDiff, diff, take_snapshot
from openadapt_flow.verify_run import (
    FRAME_DB_DETAIL,
    VerifyConfig,
    VerifyRunError,
    _write_private,
    capture_baseline,
    load_verify_config,
    parse_verify_config,
    receipt_sha256,
    verify_run,
)

__all__ = [
    "Actor",
    "BaselineUnreadable",
    "Contract",
    "DatabaseSnapshot",
    "Snapshot",
    "Source",
    "TableDiff",
    "VerifierError",
    "WriteResult",
    "capture",
    "capture_database",
    "diff_database",
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

    payload: dict[str, Any]
    used: bool = False

    def save(self, path: str | Path) -> Path:
        out = Path(path)
        _write_private(out, self.payload)
        return out

    @classmethod
    def load(cls, path: str | Path) -> "Snapshot":
        return cls(json.loads(Path(path).read_text()))


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
    return Snapshot(dict(payload))


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
        raise VerifierError("this snapshot already verified a write; capture again")
    before.used = True
    with tempfile.TemporaryDirectory(prefix="openadapt-verify-") as tmp:
        path = before.save(Path(tmp) / "baseline.json")
        result = verify_run(
            contract.config,
            params=params,
            run_record=run_record,
            write_attempted=write_attempted,
            authorization_ref=authorization_ref,
            baseline_path=path,
            provenance=None if source is None else source.provenance,
            documents=None if source is None else source.documents,
        )
    receipt = dict(result["receipt"])
    evidence = dict(result["evidence"])
    outcome = str(receipt.get("terminal_outcome") or receipt.get("outcome") or "")
    checked = dict(receipt.get("checked") or {})
    verified = outcome == "verified"
    status = "verified" if verified else _status(checked, evidence)
    return WriteResult(
        verified=verified,
        status=status,
        outcome=outcome,
        checked=checked,
        reasons=_reasons(evidence),
        receipt=receipt,
        receipt_sha256=receipt_sha256(receipt),
        evidence=evidence,
    )


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
