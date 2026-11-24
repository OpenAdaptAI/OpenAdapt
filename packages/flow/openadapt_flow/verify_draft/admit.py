"""Admission by qualification: a drafted contract earns trust or is refused.

A draft is admitted only when, on the sandbox:

1. **It refutes every planted fault.** Each case restores the store, takes
   the contract's baseline, replays the demonstrated write through the app,
   then plants one fault with the privileged connection:

   =================  ===================================================
   ``wrong_value``    one written column of the record holds another value
   ``wrong_row``      the write landed on another record (an update moved
                      to a sibling, a create under another parent)
   ``duplicate``      a second copy of the record
   ``sibling_edit``   another business record edited beside the write
   ``late_revert``    the write undone after the check confirmed it; the
                      recheck must refute
   ``lost_update``    (updates) the other user saves the record first and
                      the agent's save overwrites it, through the app
   =================  ===================================================

2. **It shows no false stop on 5 clean writes** with other values (and the
   recheck of each confirms too).

3. **The causal-attribution probe agrees with its bookkeeping exclusions.**
   The write is made by two actors with two payloads (2 x 2, each from a
   restored store). Every column that moved is attributed: to the payload
   (its value follows the values written), to the actor (it follows who
   wrote), or to the clock (it moves every time, to a new value). The
   contract must confirm all four writes, must not exclude any column the
   payload moves (that would hide the write itself), and must not exclude a
   column that never moved (an exclusion with nothing to excuse can only
   hide a fault).

Admission tests run with the contract's recheck window shortened to a few
seconds; the check is otherwise the same, and the admitted contract keeps
its own window. Nothing here calls a model.
"""

from __future__ import annotations

import copy
import datetime as _dt
import decimal
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from openadapt_flow.verdict import (
    Errored,
    FailClosed,
    Obligations,
    Observed,
    Result,
    Settlement,
    Status,
    Subject,
    observe,
    settle,
)
from openadapt_flow.verdict_sites import verdict_site
from openadapt_flow.verifier import Outcome
from openadapt_flow.verify_draft.inputs import SECRET_NAME_RE
from openadapt_flow.verify_draft.sandbox import (
    App,
    Move,
    Request,
    Rows,
    SandboxConfig,
    Store,
    moves,
    swap_map,
    text,
)

FAULTS: tuple[str, ...] = (
    "wrong_value",
    "wrong_row",
    "duplicate",
    "sibling_edit",
    "late_revert",
    "lost_update",
)
CLEAN_PROBES = 5
#: The recheck window admission cases use.
ADMISSION_RECHECK_S = 2.0
#: What a drafted contract may contain. Everything else is refused before
#: the draft is run: the model read untrusted trace and row text, so a key it
#: wrote must not choose a store, a URL, a credential or a hook.
_TOP_KEYS = frozenset(
    {
        "schema_version",
        "verifier",
        "effects",
        "frame",
        "environment_id",
        "minimum_effect_strength",
        "max_execution_window_s",
    }
)
_READ_KEYS = frozenset({"kind", "sql_query", "records_drop_fields"})
_FRAME_KEYS = frozenset(
    {
        "identity_field",
        "ignore_fields",
        "target_fields",
        "target_fields_by_surface",
        "attribution",
        "side_records",
        "database",
        "versions",
    }
)
_DATABASE_KEYS = frozenset(
    {
        "dialect",
        "allow_tables",
        "views",
        "judged_by",
        "new_row_fields",
        "ignore_columns",
        "max_rows",
        "read_row_budget",
        "settle_s",
        "settle_attempts",
        "recheck_after_s",
        "schemas",
    }
)
_VERSIONS_KEYS = frozenset(
    {
        "read",
        "record_field",
        "target_field",
        "target_surface",
        "version_field",
        "author_field",
        "self",
    }
)
_ATTRIBUTION_KEYS = frozenset(
    {
        "kind",
        "actor_field",
        "record_field",
        "record_key",
        "self",
        "read",
        "event_field",
        "write_events",
        "insert_events",
        "delete_events",
        "table_field",
        "server_set",
    }
)
_SECRET_COLUMN = SECRET_NAME_RE
_IDENTIFIER = re.compile(r"(^|_)(id|key|no|number|code|slug|mrn|ref|uuid)$", re.I)


class DraftProblem(ValueError):
    """The draft can't be run at all (unparseable, unsafe, invalid)."""


# -- the contract --------------------------------------------------------------


def _sql_read(read: Any, where: str) -> list[str]:
    if not isinstance(read, dict):
        return [f"{where} must be a read"]
    bad = [f"{where}.{k}" for k in read if k not in _READ_KEYS]
    if read.get("kind") != "sql" or "sql_query" not in read:
        bad.append(f"{where} must be kind: sql with a sql_query")
    return bad


def _disallowed(data: Mapping[str, Any]) -> list[str]:
    """Keys a draft may not set (an allowlist, not a denylist)."""

    bad = [k for k in data if k not in _TOP_KEYS]
    verifier = data.get("verifier")
    if not isinstance(verifier, dict):
        return bad + ["verifier"]
    if verifier.get("kind") == "surfaces":
        bad += [f"verifier.{k}" for k in verifier if k not in ("kind", "surfaces")]
        for name, sub in (verifier.get("surfaces") or {}).items():
            bad += _sql_read(sub, f"verifier.surfaces.{name}")
    else:
        bad += _sql_read(verifier, "verifier")
    frame = data.get("frame") or {}
    if not isinstance(frame, dict):
        return bad + ["frame"]
    bad += [f"frame.{k}" for k in frame if k not in _FRAME_KEYS]
    db = frame.get("database") or {}
    if isinstance(db, dict):
        bad += [f"frame.database.{k}" for k in db if k not in _DATABASE_KEYS]
    versions = frame.get("versions")
    if isinstance(versions, dict):
        bad += [f"frame.versions.{k}" for k in versions if k not in _VERSIONS_KEYS]
        bad += _sql_read(versions.get("read"), "frame.versions.read")
    attribution = frame.get("attribution")
    if isinstance(attribution, dict):
        bad += [
            f"frame.attribution.{k}" for k in attribution if k not in _ATTRIBUTION_KEYS
        ]
        if "read" in attribution:
            bad += _sql_read(attribution["read"], "frame.attribution.read")
    return bad


def _reads(value: Any) -> list[dict[str, Any]]:
    """Every read section of a contract: dicts that hold a ``sql_query``."""

    out = []
    if isinstance(value, dict):
        if "sql_query" in value:
            out.append(value)
        for v in value.values():
            out += _reads(v)
    elif isinstance(value, list):
        for v in value:
            out += _reads(v)
    return out


def prepare(contract_yaml: str, sandbox: SandboxConfig) -> dict[str, Any]:
    """Parse a draft, refuse anything unsafe, and point its reads at the
    sandbox's read-only connection."""

    import yaml

    from openadapt_flow.generate.verify_contract import guard_problem
    from openadapt_flow.verifier import Contract, VerifierError

    try:
        data = yaml.safe_load(contract_yaml)
    except yaml.YAMLError as exc:
        raise DraftProblem(f"the contract is not valid JSON or YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise DraftProblem("the contract must be one JSON object")
    data.pop("provenance", None)
    if "${" in json.dumps(data, default=str):
        raise DraftProblem(
            "the contract may not reference environment variables (${...})"
        )
    bad = _disallowed(data)
    if bad:
        raise DraftProblem(
            "the contract may only read the database with plain SQL; it may not set "
            + ", ".join(sorted(set(bad)))
        )
    frame = data.get("frame") or {}
    database = frame.get("database") if isinstance(frame, dict) else None
    if not isinstance(database, dict):
        raise DraftProblem("the contract must diff the whole database (frame.database)")
    if database.get("dialect") != sandbox.store.dialect:
        raise DraftProblem(f"frame.database.dialect must be {sandbox.store.dialect}")
    location = connection(sandbox)
    for read in _reads(data):
        problem = guard_problem(str(read.get("sql_query") or ""))
        if problem:
            raise DraftProblem(f"a read is not a plain SELECT: {problem}")
        read.update(location)
    database["read"] = dict(location)
    try:
        Contract.from_dict(copy.deepcopy(data), name="draft")
    except (VerifierError, ValueError) as exc:
        raise DraftProblem(f"the contract does not load: {exc}") from exc
    return data


def connection(sandbox: SandboxConfig) -> dict[str, Any]:
    read = dict(sandbox.store.read)
    keys = ("sqlite_database", "sql_driver", "sql_connect_args", "sql_password_env")
    return {k: read[k] for k in keys if k in read}


def excluded(contract: Mapping[str, Any]) -> dict[str, set[str]]:
    """Table -> the columns the contract excuses as bookkeeping (``*`` for a
    skipped table)."""

    database = (contract.get("frame") or {}).get("database") or {}
    out: dict[str, set[str]] = {}
    for entry in database.get("allow_tables") or []:
        table = entry if isinstance(entry, str) else entry.get("table")
        mode = (
            "append_only"
            if isinstance(entry, str)
            else entry.get("mode", "append_only")
        )
        out.setdefault(str(table), set()).add("*" if mode == "skip" else "+")
    for table, cols in (database.get("ignore_columns") or {}).items():
        out.setdefault(str(table), set()).update(str(c) for c in cols)
    return out


def check_sha256(contract: Mapping[str, Any]) -> str:
    """The fault gate's check digest: effects, read and frame, not where
    the store is."""

    from openadapt_flow.fault_gate import check_digest
    from openadapt_flow.verifier import Contract

    data = {k: v for k, v in contract.items() if k != "provenance"}
    return check_digest(Contract.from_dict(copy.deepcopy(data), name="draft").config)


# -- the demonstration ---------------------------------------------------------


@dataclass
class Demo:
    """The demonstrated write as the sandbox replays it."""

    writes: list[Request]
    params: dict[str, Any]
    base: Rows
    after: Rows
    main: Optional[Move]
    param_columns: dict[str, str]  # column -> param
    statuses: list[int] = field(default_factory=list)

    @property
    def change(self) -> str:
        return self.main.change if self.main else ""

    def moved_text(self) -> str:
        """What the demonstration moved, for the model: tables, rows and
        columns with their values (synthetic sandbox data)."""

        lines = []
        for m in moves(self.base, self.after):
            row = m.after if m.after is not None else m.before
            cols = m.columns if m.change == "changed" else sorted(row or {})
            vals = {
                c: (
                    "***"  # a secret-named column: its name, never its value
                    if _SECRET_COLUMN.search(c)
                    else f"{text((m.before or {}).get(c))!r} -> {text((m.after or {}).get(c))!r}"
                    if m.change == "changed"
                    else text((row or {}).get(c))
                )
                for c in cols
            }
            lines.append(
                f"{m.change} {m.table} key={_keytext(m.key)}: {json.dumps(vals)[:1500]}"
            )
        return "\n".join(lines) or "(nothing moved)"


def _keytext(key: Any) -> str:
    return ",".join(text(k) for k in key) if isinstance(key, tuple) else str(key)


def _matches(stored: Any, value: Any) -> bool:
    a, b = text(stored).strip(), text(value).strip()
    if not b:
        return False
    if a == b:
        return True
    if "@" in b and a.casefold() == b.casefold():
        return True  # an email address the app stored in another case
    try:
        return decimal.Decimal(a) == decimal.Decimal(b)
    except (decimal.InvalidOperation, ValueError):
        pass
    return len(b) >= 10 and a.startswith(b)  # a date read back as a date-time


def main_move(
    found: Sequence[Move], params: Mapping[str, Any]
) -> tuple[Optional[Move], dict[str, str]]:
    """The added or changed row holding the most task values, and which of
    its columns holds which value."""

    best: tuple[int, Optional[Move], dict[str, str]] = (0, None, {})
    for m in found:
        if m.after is None:
            continue
        cols = {}
        for col, stored in m.after.items():
            for name, value in params.items():
                if name == "actor_id":
                    continue
                if _matches(stored, value) and (
                    m.change == "added" or col in m.columns
                ):
                    cols[col] = name
                    break
        score = len(set(cols.values()))
        if score > best[0]:
            best = (score, m, cols)
    return best[1], best[2]


def record_demo(
    store: Store, app: App, writes: list[Request], params: dict[str, Any], base: Rows
) -> Demo:
    store.remember(base)
    store.restore(base)
    statuses = app.send(writes, "agent", {})
    after = store.read_moved(base)
    found = moves(base, after)
    main, cols = main_move(found, params)
    store.restore(base)
    return Demo(writes, dict(params), base, after, main, cols, statuses)


# -- cases ---------------------------------------------------------------------


@dataclass
class Case:
    group: str  # fault | clean | probe
    name: str
    detail: str
    expect: str  # confirmed | refuted
    outcome: str = ""
    recheck: str = ""
    passed: bool = False
    applicable: bool = True
    #: The sandbox can't build this case (no other record, no second user):
    #: never a pass, and an operator fix rather than the model's.
    untestable: bool = False
    note: str = ""
    reasons: list[str] = field(default_factory=list)
    moved: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def line(self) -> str:
        verdict = (
            "UNTESTABLE"
            if self.untestable
            else "ok"
            if self.passed
            else ("n/a" if not self.applicable else "FAILED")
        )
        got = self.outcome + (f", recheck {self.recheck}" if self.recheck else "")
        why = "; ".join(self.reasons[:3])
        if self.moved and not self.passed:
            why += ". Moved: " + "; ".join(
                f"{m.get('table')} {m.get('change')} {m.get('columns')}"
                + (f" ({m.get('attribution')})" if m.get("attribution") else "")
                for m in self.moved[:5]
            )
        return (
            f"[{verdict}] {self.group} {self.name}: {self.detail}. Expected {self.expect}, got {got or '-'}"
            + (f". {self.note}" if self.note else "")
            + (f". Reasons: {why}" if why and not self.passed else "")
        )


@dataclass
class Attribution:
    table: str
    column: str
    cause: str  # payload | actor | clock | constant
    excluded: bool
    problem: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Admission:
    admitted: bool
    check_sha256: str
    cases: list[Case]
    attribution: list[Attribution]
    problems: list[str]
    masked: list[dict[str, str]] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "check_sha256": self.check_sha256,
            "problems": self.problems,
            "masked": self.masked,
            "seconds": self.seconds,
            "cases": [c.as_dict() for c in self.cases],
            "attribution": [a.as_dict() for a in self.attribution],
        }

    def untestable(self) -> list[Case]:
        return [c for c in self.cases if c.untestable]

    def only_untestable(self) -> bool:
        """Every failure is the sandbox's, not the draft's."""

        return bool(self.untestable()) and not (
            self.problems
            or any(a.problem for a in self.attribution)
            or any(
                not c.passed and not c.untestable and c.applicable for c in self.cases
            )
        )

    def feedback(self) -> str:
        lines = [
            c.line()
            for c in self.cases
            if not c.passed and c.applicable and not c.untestable
        ]
        lines += [
            c.line() + " (the sandbox can't build this case; not the contract's fault)"
            for c in self.untestable()
        ]
        lines += [
            f"[FAILED] attribution {a.table}.{a.column} ({a.cause}): {a.problem}"
            for a in self.attribution
            if a.problem
        ]
        lines += [f"[FAILED] {p}" for p in self.problems]
        return "\n".join(lines)


def _perturb(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 7
    if isinstance(value, float):
        return value + 1.5
    if isinstance(value, decimal.Decimal):
        return value + 1
    if isinstance(value, _dt.datetime):
        return value + _dt.timedelta(days=1, hours=1)
    if isinstance(value, _dt.date):
        return value + _dt.timedelta(days=1)
    if isinstance(value, uuid.UUID):
        return uuid.uuid4()
    if value is None:
        return "X"
    s = str(value)
    if re.fullmatch(r"-?\d+", s):
        return str(int(s) + 7)
    return s + " X"


@verdict_site("verify_draft.judge_case", classes="C3 C5")
def judge_case(case: Case) -> Settlement:
    """Settle one admission case against its expected verifier outcome.

    An untestable case, an empty/error/no-baseline outcome, or any unknown
    outcome or expectation code settles as ``NOT_CHECKED`` and never passes.
    """
    subject = Subject("case", case.name or "case")
    if case.untestable:
        return settle(
            Obligations.fix([subject], source="verify_draft"),
            [Result(subject, Status.NOT_CHECKED, reason="untestable")],
        )
    if not case.applicable:
        return settle(
            Obligations.fix([subject], source="verify_draft"),
            [Result.held(subject, Observed("not_applicable"), reason="not_applicable")],
        )
    if case.expect == "confirmed":
        obligations = [subject]
        results = [
            Result.from_code(
                subject,
                case.outcome,
                held=frozenset({"CONFIRMED"}),
                refuted=frozenset({"REFUTED"}),
            )
        ]
        if case.recheck:
            recheck_subject = Subject("recheck", case.name or "case")
            obligations.append(recheck_subject)
            results.append(
                Result.from_code(
                    recheck_subject,
                    case.recheck,
                    held=frozenset({"CONFIRMED", "NONE"}),
                    refuted=frozenset({"REFUTED"}),
                )
            )
        return settle(Obligations.fix(obligations, source="verify_draft"), results)
    if case.expect == "refuted":
        final_subject = Subject("final", case.name or "case")
        final_code = case.recheck or case.outcome
        return settle(
            Obligations.fix([subject, final_subject], source="verify_draft"),
            [
                Result.from_code(
                    subject,
                    case.outcome,
                    held=frozenset(
                        {"CONFIRMED", "REFUTED", "UNCERTAIN", "INDETERMINATE"}
                    ),
                    refuted=frozenset(),
                ),
                Result.from_code(
                    final_subject,
                    final_code,
                    held=frozenset({"REFUTED", "UNCERTAIN", "INDETERMINATE"}),
                    refuted=frozenset({"CONFIRMED", "NONE"}),
                ),
            ],
        )
    return settle(
        Obligations.fix([subject], source="verify_draft"),
        [Result(subject, Status.NOT_CHECKED, reason=f"unknown expect {case.expect!r}")],
    )


class Qualifier:
    """Runs one draft through the admission suite on the sandbox."""

    def __init__(
        self,
        sandbox: SandboxConfig,
        store: Store,
        app: App,
        demo: Demo,
        *,
        recheck_s: float = ADMISSION_RECHECK_S,
        workdir: Optional[Path] = None,
        seed: int = 0,
        bookkeeping: Sequence[str] = (),
    ) -> None:
        self.sandbox = sandbox
        self.store = store
        self.app = app
        self.demo = demo
        self.recheck_s = recheck_s
        self.workdir = workdir or Path(tempfile.mkdtemp(prefix="verify-draft-"))
        self.seed = seed
        #: ``table.column`` a person said is bookkeeping (the one question).
        self.bookkeeping = set(bookkeeping)
        self.masked: list[dict[str, str]] = []
        #: The HTTP statuses of the last write the app refused in :meth:`run`.
        self.refused_statuses: list[int] = []
        os.environ.setdefault("OPENADAPT_LOCAL_SEAL", "off")

    # -- one write under the contract

    def _params(self, values: Mapping[str, Any], actor: str) -> dict[str, Any]:
        out = {k: (None if v is None else text(v)) for k, v in values.items()}
        out["actor_id"] = self.sandbox.actor(actor).id
        return out

    def _fast(self, contract: Mapping[str, Any]) -> dict[str, Any]:
        data = copy.deepcopy(dict(contract))
        data.pop("provenance", None)
        db = data["frame"]["database"]
        if db.get("recheck_after_s") is not None:
            db["recheck_after_s"] = min(float(db["recheck_after_s"]), self.recheck_s)
        return data

    def run(
        self,
        contract: Mapping[str, Any],
        case: Case,
        *,
        values: Mapping[str, Any],
        actor: str = "agent",
        before: Optional[Callable[[], Optional[str]]] = None,
        fault: Optional[Callable[[Rows, Rows], None]] = None,
        revert: Optional[Callable[[Rows, Rows], None]] = None,
        rechecks: bool = False,
    ) -> Optional[tuple[Rows, Rows]]:
        """Restore, baseline, write, plant, verify (and recheck). Fills
        ``case``; returns (before, after) store reads, or None when the
        write itself could not be made."""

        from openadapt_flow.verifier import (
            Actor,
            Contract,
            Ledger,
            MemoryLedger,
            VerifierError,
            WriteBlocked,
            begin_write,
            recheck,
            verify_write,
        )

        started = time.monotonic()
        draft_problem: list[DraftProblem] = []

        def _execute() -> Optional[tuple[Rows, Rows]]:
            try:
                data = self._fast(contract)
                c = Contract.from_dict(data, name="draft")
                params = self._params(values, actor)
                self.store.restore(self.demo.base)
                ledger = Ledger(MemoryLedger())
                try:
                    pending = begin_write(
                        c,
                        params,
                        actor=Actor("agent", f"verify-draft-{actor}"),
                        ledger=ledger,
                        write_key=uuid.uuid4().hex,
                        workdir=self.workdir,
                    )
                except (WriteBlocked, VerifierError) as exc:
                    case.outcome = "NO_BASELINE"
                    case.reasons = [str(exc)]
                    return None
                if before is not None:
                    note = before()
                    if note:
                        case.untestable = True  # the setup step itself failed
                        case.note = note
                        return None
                statuses = self.app.send(
                    self.demo.writes, actor, swap_map(self.demo.params, values)
                )
                if any(s >= 400 for s in statuses):
                    # For a clean write: the values were invalid (the caller
                    # skips them). For anything else the case can't be built.
                    self.refused_statuses = list(statuses)
                    case.applicable = False
                    case.untestable = case.group != "clean"
                    case.note = f"the app refused the write (HTTP {max(statuses)})"
                    return None
                pre = self.demo.base
                post = self.store.read_moved(pre)
                if fault is not None:
                    fault(pre, post)
                    post = self.store.read_moved(pre)
                result = verify_write(pending, seal=False)
                case.outcome = result.outcome.value
                case.reasons = list(result.reasons)
                case.moved = list(
                    ((result.evidence or {}).get("frame") or {}).get("moves") or []
                )[:20]
                if rechecks and result.outcome is Outcome.CONFIRMED:
                    if result.recheck is None:
                        case.recheck = "NONE"
                        case.reasons.append(
                            "no recheck was scheduled: set frame.database.recheck_after_s"
                        )
                    else:
                        if revert is not None:
                            revert(pre, post)
                        rr = recheck(
                            c,
                            result,
                            params=params,
                            actor=Actor("agent", f"verify-draft-{actor}"),
                            wait=True,
                            ledger=ledger,
                        )
                        case.recheck = str(getattr(rr.outcome, "value", rr.outcome))
                        case.reasons += list(rr.reasons)
                return pre, post
            except DraftProblem as exc:
                draft_problem.append(exc)
                raise FailClosed(str(exc)) from exc

        try:
            obs = observe(_execute)
            if isinstance(obs, Errored):
                case.outcome = case.outcome or "ERROR"
                case.reasons.append(f"{obs.exc_type}: {obs.reason}")
                return None
            if isinstance(obs, Observed):
                return obs.value  # type: ignore[return-value]
            return None
        except FailClosed:
            if draft_problem:
                raise draft_problem[0] from None
            raise
        finally:
            case.seconds = round(time.monotonic() - started, 2)

    @staticmethod
    def _judge(case: Case) -> None:
        case.passed = judge_case(case).passes

    # -- the suite

    def qualify(
        self,
        contract: Mapping[str, Any],
        payloads: Sequence[Mapping[str, Any]],
        *,
        faults: Sequence[str] = FAULTS,
        clean: int = CLEAN_PROBES,
        base_values: Optional[Mapping[str, Any]] = None,
        attribute: bool = True,
    ) -> Admission:
        started = time.monotonic()
        self.masked = []
        cases: list[Case] = []
        problems: list[str] = []
        demo = self.demo
        if demo.main is None:
            problems.append("the demonstration moved no row holding a task value")
            return Admission(False, check_sha256(contract), cases, [], problems)
        problems += self._pins(contract)
        values = [self._values(p) for p in payloads]
        values = [v for v in values if v]

        # 1. clean writes, with rechecks
        accepted: list[dict[str, Any]] = []
        for i, v in enumerate(values):
            if len(accepted) >= clean:
                break
            case = Case(
                "clean",
                f"clean-{len(accepted) + 1}",
                f"the write with payload {i + 1}",
                "confirmed",
            )
            self.run(contract, case, values=v, rechecks=True)
            if not case.applicable:
                continue  # the app refused these values: not a probe
            self._judge(case)
            cases.append(case)
            accepted.append(v)
            if not case.passed and len(accepted) == 1:
                # A check that stops a clean write can't be judged on faults
                # yet: say so now rather than spend the round.
                problems.append(
                    "stopped after the first clean write was not confirmed: fix that first "
                    "(no fault was planted this round)"
                )
                self.store.restore(demo.base)
                return Admission(
                    False,
                    check_sha256(contract),
                    cases,
                    [],
                    problems,
                    [],
                    round(time.monotonic() - started, 2),
                )
        if len(accepted) < clean:
            problems.append(
                f"only {len(accepted)} of the payloads were accepted by the app; {clean} clean writes are needed"
            )

        # 2. planted faults
        base_values = dict(base_values or demo.params)
        for name in faults:
            cases += self._fault(contract, name, base_values, accepted)

        # 3. causal attribution (two actors by two payloads)
        attribution: list[Attribution] = []
        probe_cases: list[Case] = []
        if attribute:
            attribution, probe_cases = self._attribute(
                contract, [base_values] + accepted[:1]
            )
        cases += probe_cases
        self.store.restore(demo.base)  # leave the sandbox as it was found
        for c in cases:
            if c.group != "clean":
                self._judge(c)
        admitted = (
            not problems
            and all(c.passed for c in cases)
            and not any(a.problem for a in attribution)
            and not self.masked
        )
        return Admission(
            admitted,
            check_sha256(contract),
            cases,
            attribution,
            problems,
            list(self.masked),
            round(time.monotonic() - started, 2),
        )

    def _pins(self, contract: Mapping[str, Any]) -> list[str]:
        """Literals in the effects that pin the demonstrated record: a value
        of its row that other rows of its table don't share. Such a check
        holds for that one record only."""

        m = self.demo.main
        if m is None:
            return []
        rows = list(self.demo.base.get(m.table, {}).values()) + list(
            self.demo.after.get(m.table, {}).values()
        )
        own = {text(v) for v in (m.before or {}).values()} | {
            text(v) for v in (m.after or {}).values()
        }
        common = set()
        for col in m.after or {}:
            values = {text(r.get(col)) for r in rows}
            if len(values) == 1:
                common |= values
        out = []
        for effect in contract.get("effects") or []:
            selectors = [effect.get("match") or {}] + list(
                effect.get("new_records") or []
            )
            for sel in selectors:
                for field_name, expr in sel.items():
                    if isinstance(expr, dict) and "literal" in expr:
                        val = text(expr["literal"])
                        if val and val in own and val not in common:
                            out.append(
                                f"the effect pins {field_name}={val!r}, a value of the demonstrated "
                                "record: bind it to a task parameter (or derive it in the read) so the "
                                "check holds for any record the task names"
                            )
        return out

    def _values(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(self.demo.params)
        changed = False
        # A value the demonstration shares with another parameter can't be
        # swapped in the trace without changing both: it is not varied.
        shared = {
            k for k in out for j in out if k != j and text(out[k]) == text(out[j])
        }
        for k, v in payload.items():
            if k in shared:
                continue
            if k in out and v is not None and text(v) != text(out[k]):
                out[k] = v
                changed = True
        return out if changed else {}

    # -- fault operators

    def _target(
        self, pre: Rows, post: Rows, values: Mapping[str, Any]
    ) -> tuple[Optional[Move], dict[str, str]]:
        return main_move(moves(pre, post), values)

    def _fault(
        self,
        contract: Mapping[str, Any],
        name: str,
        values: dict[str, Any],
        accepted: Sequence[Mapping[str, Any]],
    ) -> list[Case]:
        demo = self.demo
        table = demo.main.table if demo.main else ""
        out: list[Case] = []
        mask = excluded(contract)

        if name == "wrong_value":
            cols = sorted(demo.param_columns)[:4] or []
            if not cols:
                c = Case(
                    "fault",
                    "wrong_value",
                    "no written column holds a task value",
                    "refuted",
                    applicable=False,
                )
                return [c]
            for col in cols:
                case = Case(
                    "fault",
                    f"wrong_value:{col}",
                    f"{table}.{col} holds another value",
                    "refuted",
                )

                def plant(pre: Rows, post: Rows, col: str = col) -> None:
                    m, _ = self._target(pre, post, values)
                    if m is None:
                        raise RuntimeError(
                            "the write moved no row holding a task value"
                        )
                    self.store.update(m.table, m.key, {col: _perturb(m.a[col])})

                self.run(contract, case, values=values, fault=plant)
                out.append(case)
            return out

        if name == "wrong_row":
            case = Case("fault", "wrong_row", "", "refuted")

            def plant_row(pre: Rows, post: Rows) -> None:
                m, cols = self._target(pre, post, values)
                if m is None:
                    raise RuntimeError("the write moved no row holding a task value")
                t = self.store.tables[m.table]
                if m.change == "changed":
                    others = [k for k in post[m.table] if k != m.key]
                    if not others:
                        raise _Untestable("the table holds no other record")
                    other = others[self.seed % len(others)]
                    moved = {c: m.a[c] for c in m.columns}
                    self.store.update(m.table, other, moved)
                    self.store.update(m.table, m.key, {c: m.b[c] for c in m.columns})
                    case.detail = f"the update landed on another {m.table} row"
                    return
                fk = [c for c in m.columns if c in t.fks and m.a.get(c) is not None]
                if fk:
                    col = fk[0]
                    ref = self.store.tables[t.fks[col]]
                    choices = [
                        k
                        for k in post.get(ref.name, {})
                        if len(ref.pk) == 1 and text(k[0]) != text(m.a[col])
                    ]
                    if choices:
                        self.store.update(
                            m.table, m.key, {col: choices[self.seed % len(choices)][0]}
                        )
                        case.detail = f"the new {m.table} row points at another {ref.name} ({col})"
                        return
                ids = [
                    c
                    for c, p in cols.items()
                    if _IDENTIFIER.search(c) or _IDENTIFIER.search(p)
                ]
                if not ids:
                    raise _Untestable("the new row names no parent record")
                self.store.update(m.table, m.key, {ids[0]: _perturb(m.a[ids[0]])})
                case.detail = f"the new {m.table} row is filed under another {ids[0]}"

            self._planted(contract, case, values, plant_row)
            return [case]

        if name == "duplicate":
            case = Case(
                "fault", "duplicate", f"a second copy of the {table} record", "refuted"
            )

            def plant_dup(pre: Rows, post: Rows) -> None:
                m, _ = self._target(pre, post, values)
                if m is None:
                    raise RuntimeError("the write moved no row holding a task value")
                self.store.insert(m.table, self.store.new_key_row(m.table, m.a, post))

            self._planted(contract, case, values, plant_dup)
            return [case]

        if name == "sibling_edit":
            case = Case("fault", "sibling_edit", "", "refuted")

            def plant_sib(pre: Rows, post: Rows) -> None:
                m, _ = self._target(pre, post, values)
                order = (
                    [m.table] + [t for t in self.store.tables if t != m.table]
                    if m
                    else list(self.store.tables)
                )
                hidden: list[str] = []
                for tname in order:
                    rows = [
                        (k, r)
                        for k, r in post.get(tname, {}).items()
                        if not (m and tname == m.table and k == m.key)
                    ]
                    t = self.store.tables[tname]
                    for key, row in rows:
                        for col in t.columns:
                            if col in t.pk or col in t.fks or row.get(col) is None:
                                continue
                            if isinstance(row[col], (bytes, memoryview, dict, list)):
                                continue
                            ex = mask.get(tname, set())
                            if "*" in ex or col in ex:
                                if f"{tname}.{col}" not in self.bookkeeping:
                                    hidden.append(f"{tname}.{col}")
                                continue
                            if not t.pk:
                                continue
                            self.store.update(tname, key, {col: _perturb(row[col])})
                            case.detail = (
                                f"another {tname} row's {col} edited beside the write"
                            )
                            return
                if hidden:
                    for h in hidden[:3]:
                        self.masked.append({"fault": "sibling_edit", "column": h})
                    raise _Untestable(
                        "every editable column of the other records is excluded: "
                        + ", ".join(hidden[:3])
                    )
                raise _Untestable("the store holds no other record to edit")

            self._planted(contract, case, values, plant_sib)
            return [case]

        if name == "late_revert":
            case = Case(
                "fault",
                "late_revert",
                f"the {table} write undone after the check",
                "refuted",
            )

            def undo(pre: Rows, post: Rows) -> None:
                m, _ = self._target(pre, post, values)
                if m is None:
                    raise RuntimeError("the write moved no row holding a task value")
                if m.change == "added":
                    self.store.delete(m.table, m.key)
                else:
                    self.store.update(m.table, m.key, {c: m.b[c] for c in m.columns})

            self.run(contract, case, values=values, revert=undo, rechecks=True)
            if case.outcome == Outcome.CONFIRMED.value and not case.recheck:
                case.recheck = "NONE"
            return [case]

        if name == "lost_update":
            case = Case("fault", "lost_update", "", "refuted")
            if demo.change != "changed":
                case.applicable, case.note = (
                    False,
                    "the write creates a record; nothing can be overwritten",
                )
                return [case]
            if "other" not in self.sandbox.actors:
                case.untestable, case.note = (
                    True,
                    "the sandbox has no second user (actors.other)",
                )
                return [case]
            other_values = accepted[0] if accepted else None
            if other_values is None:
                case.untestable, case.note = (
                    True,
                    "no accepted payload for the other user",
                )
                return [case]

            other_saved: list[Rows] = []

            def first_save() -> Optional[str]:
                st = self.app.send(
                    demo.writes, "other", swap_map(demo.params, other_values)
                )
                if any(s >= 400 for s in st):
                    return f"the other user's save was refused (HTTP {max(st)})"
                other_saved.append(self.store.read_moved(demo.base))
                return None

            case.detail = (
                "the other user saves the record, then the agent's save overwrites it"
            )
            self.refused_statuses = []
            self.run(contract, case, values=values, before=first_save)
            refused = [s for s in self.refused_statuses if s >= 400]
            if case.note.startswith("the app refused"):
                # fail-open class: absent-evidence-as-success. Only a conflict
                # status that left the other user's save in the store shows
                # the app prevents lost updates itself. An outage or an
                # expired session (503, 401) observed nothing: untestable.
                kept = bool(other_saved) and (
                    bool(moves(demo.base, other_saved[0]))
                    and not moves(other_saved[0], self.store.read_moved(demo.base))
                )
                if refused and set(refused) <= {409, 412} and kept:
                    case.untestable = False
                    case.note = (
                        "the app refused the stale save with a conflict and kept "
                        "the other user's save: it prevents lost updates itself"
                    )
                else:
                    case.untestable = True
                    case.note = (
                        f"the stale save failed (HTTP {max(refused or [0])}) "
                        "without a conflict that kept the other user's save"
                    )
            return [case]
        raise ValueError(f"unknown fault {name}")

    def _planted(
        self,
        contract: Mapping[str, Any],
        case: Case,
        values: Mapping[str, Any],
        plant: Callable[[Rows, Rows], None],
    ) -> None:
        def guarded(pre: Rows, post: Rows) -> None:
            try:
                plant(pre, post)
            except _NotApplicable as exc:
                case.applicable = False
                case.note = str(exc)
                raise
            except _Untestable as exc:
                case.untestable = True
                case.note = str(exc)
                raise

        try:
            self.run(contract, case, values=values, fault=guarded)
        finally:
            if not case.applicable or case.untestable:
                case.outcome = ""
                case.reasons = [
                    r
                    for r in case.reasons
                    if "_NotApplicable" not in r and "_Untestable" not in r
                ]

    # -- causal attribution

    def _attribute(
        self, contract: Mapping[str, Any], payloads: Sequence[Mapping[str, Any]]
    ) -> tuple[list[Attribution], list[Case]]:
        actors = ["agent"] + (["other"] if "other" in self.sandbox.actors else [])
        if len(payloads) < 2:
            return [], []
        runs: dict[tuple[str, int], list[Move]] = {}
        cases = []
        if "other" not in self.sandbox.actors:
            # fail-open class: absent-evidence-as-success. Without a second
            # user the actor axis is never probed, so nothing shows which
            # moves follow the actor: untestable, never a smaller pass.
            missing = Case(
                "probe",
                "attribution:other",
                "the writes by a second user",
                "confirmed",
            )
            missing.untestable = True
            missing.note = "the sandbox has no second user (actors.other)"
            cases.append(missing)
        # Each actor with each payload, plus the first one again: what moves
        # between two identical writes moves on its own (a clock, a new id).
        plan = [(a, i) for a in actors for i in (0, 1)] + [(actors[0], 2)]
        for actor, i in plan:
            values = payloads[min(i, 1) if i < 2 else 0]
            label = f"p{i + 1}" if i < 2 else "p1-again"
            case = Case(
                "probe",
                f"attribution:{actor}:{label}",
                f"the write by {actor} with payload {1 if i == 2 else i + 1}",
                "confirmed",
            )
            got = self.run(contract, case, values=values, actor=actor)
            cases.append(case)
            if got is not None:
                runs[(actor, i)] = moves(*got)
        # Every (table, column) that moved, with its new value per run.
        cells: dict[tuple[str, str], dict[tuple[str, int], str]] = {}
        for run, found in runs.items():
            for m in found:
                if m.change == "removed":
                    cells.setdefault((m.table, "-"), {})[run] = "removed"
                    continue
                cols = m.columns if m.change == "changed" else sorted(m.after or {})
                for col in cols:
                    cells.setdefault((m.table, col), {})[run] = text(
                        (m.after or {}).get(col)
                    )
                if m.change == "added":
                    cells.setdefault((m.table, "+"), {})[run] = "added"
        for seen in cells.values():  # a run that didn't move the cell
            for run in runs:
                seen.setdefault(run, "<unmoved>")
        mask = excluded(contract)
        out: list[Attribution] = []
        for (table, col), seen in sorted(cells.items()):
            cause = _cause(seen, actors)
            ex = mask.get(table, set())
            is_ex = "*" in ex or col in ex or (col == "+" and "+" in ex)
            a = Attribution(table, col, cause, is_ex)
            if is_ex and cause == "payload" and col != "+":
                a.problem = "excluded, but it moves with the values written: the exclusion hides the write"
            out.append(a)
        observed_cells = {(a.table, a.column) for a in out}
        observed_tables = {t for t, _ in observed_cells}
        for table, masked_cols in mask.items():
            for col in masked_cols:
                if col == "*":
                    unexcused = table not in observed_tables
                    what = "a skipped table"
                elif col == "+":
                    unexcused = (table, "+") not in observed_cells
                    what = "an append-only table"
                else:
                    unexcused = (table, col) not in observed_cells
                    what = "excluded"
                if unexcused:
                    out.append(
                        Attribution(
                            table,
                            col,
                            "never moved",
                            True,
                            f"{what}, but it never moved in any probe write: nothing to excuse",
                        )
                    )
        return out, cases


def _cause(seen: Mapping[tuple[str, int], str], actors: Sequence[str]) -> str:
    """payload, actor, fresh, clock or constant, from the 2 x 2 runs and
    the repeated first run (index 2)."""

    if (actors[0], 2) in seen and (actors[0], 0) in seen:
        if seen.get((actors[0], 0)) != seen.get((actors[0], 2)):
            values = [v for v in seen.values()]
            return "clock" if _clockish(values) else "fresh"
    by_payload = any(
        seen.get((a, 0)) != seen.get((a, 1))
        for a in actors
        if (a, 0) in seen or (a, 1) in seen
    )
    by_actor = len(actors) > 1 and any(
        seen.get((actors[0], i)) != seen.get((actors[1], i)) for i in (0, 1)
    )
    values = [v for v in seen.values()]
    if (
        len(set(values)) == len(values)
        and len(values) == 2 * len(actors)
        and _clockish(values)
    ):
        return "clock"
    if by_payload and not by_actor:
        return "payload"
    if by_actor and not by_payload:
        return "actor"
    if by_payload and by_actor:
        # both: a clock or counter, unless the values follow the payload
        return "clock" if _clockish(values) else "payload"
    return "constant"


def _clockish(values: Sequence[str]) -> bool:
    return all(
        re.match(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", v or "")
        or re.fullmatch(r"\d{9,}(\.\d+)?", v or "")
        for v in values
    )


class _NotApplicable(Exception):
    pass


class _Untestable(Exception):
    pass


def digest(value: Any) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, default=str).encode()
        ).hexdigest()
    )
