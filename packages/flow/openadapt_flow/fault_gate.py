"""Admit a record check only after it is shown to catch the faults it claims.

A record check is built from the same place as the save, so a passing check
proves little until it has been seen to fail. This gate runs the check
against a **test copy** of the system of record and applies a fixed suite of
bad saves to the copy's **store**. The check reads them through **its own
read**, so a read whose scope can't see a fault fails the gate, not only a
judge that misses it. (Mutating the records a check already read, as
``record_check_synthesis.prove_against_faults`` does, tests the judge only.)

Each case resets the copy, takes the check's baseline, performs the clean
save, applies one fault, and runs the check end to end through
:func:`openadapt_flow.verify_run.verify_run`. The clean save must come out
``verified``; each fault must not.

=================  ===========================================  ===========
Fault class        The copy holds after the save                Claim
=================  ===========================================  ===========
``wrong_value``    the target with one declared field changed   delta
``wrong_record``   the target's values on a look-alike instead  delta
``unrelated_edit`` the clean save plus another record edited    frame
``other_table``    the clean save plus a record in another       frame
                   table edited
``side_write``     the clean save plus a row in another table    frame
                   added for the saved record
``partial``        the target with its later fields blank       delta
``duplicate``      the target twice                             delta
``missing``        no target                                    delta
``delayed``        no target until after the check's timeout    delta
=================  ===========================================  ===========

``other_table`` and ``side_write`` write outside the check's read: an edit
to another chart's history, an insurance row or a note for the new patient
(OpenAdaptAI/OpenAdapt#1506). A check that reads one table of a store that
holds others can't see them, so it is not admitted for a frame. On a store
with no other table they are not applicable.

Coverage is faults killed over faults applicable, per class, recorded in
``check_coverage.json`` (``openadapt.check-coverage/v1``) and bound to the
check by :func:`check_digest`. ``verify-run`` reads it under an
``admission:`` policy: every class of every part the check claims must be
tested and every one of its faults killed. The receipt signs the totals and
the result per class.

The mechanism, the seven operators and this public suite are public. A grown
fault corpus and deployment coverage floors stay private
(``source-policy``: ``grown_corpus``, ``deployment_thresholds``).
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.runtime.effects.effect import Effect, EffectKind

COVERAGE_SCHEMA = "openadapt.check-coverage/v1"
SUITE_ID = "openadapt.fault-suite/public"
SUITE_VERSION = "1.1.0"
COVERAGE_FILE = "check_coverage.json"

FAULT_CLASSES: tuple[str, ...] = (
    "wrong_value",
    "wrong_record",
    "unrelated_edit",
    "other_table",
    "side_write",
    "partial",
    "duplicate",
    "missing",
    "delayed",
)
_FRAME_CLASSES = frozenset({"unrelated_edit", "other_table", "side_write"})
#: Which part of the claim each fault class tests.
CLAIM_OF: dict[str, str] = {
    name: ("frame" if name in _FRAME_CLASSES else "delta") for name in FAULT_CLASSES
}
#: Every fault class that tests a part of the claim.
CLASSES_OF: dict[str, tuple[str, ...]] = {
    part: tuple(name for name in FAULT_CLASSES if CLAIM_OF[name] == part)
    for part in sorted(set(CLAIM_OF.values()))
}

#: Verifier keys that say where the store is or how to sign in, not what is
#: read. A test copy changes them; the check is the same check.
_LOCATION_KEYS = frozenset(
    {
        "base_url",
        "auth",
        "access_token",
        "access_token_env",
        "smart_backend",
        "verify_tls",
        "sqlite_database",
        "sql_driver",
        "sql_connect_args",
        "sql_password_env",
        "secret_env",
        "root",
        "session",
    }
)


class GateError(ValueError):
    """The gate could not run. Nothing it produced may admit a check."""


# -- the test copy -------------------------------------------------------------


class TestCopy(Protocol):
    """A disposable copy of the system of record, with privileged writes.

    The check reads the copy through its own verifier, built from the same
    ``verify.yaml`` with :meth:`verifier_overrides` applied. The gate writes
    through :meth:`update`, :meth:`insert` and :meth:`delete`, which bypass the
    app on purpose: a fault is what the app should never do.
    """

    #: Short name recorded in the coverage file (``sqlite``, ``rest_memory``,
    #: a fixture name).
    name: str
    #: The record field the copy's write primitives address records by.
    key_field: str

    def reset(self) -> None:
        """Restore the state before the save."""

    def save(self) -> Any:
        """Perform the clean save, as the app would. Returns the target's key."""

    def rows(self) -> list[dict[str, Any]]:
        """Every record of the slice, read with privilege."""

    def update(self, key: Any, changes: Mapping[str, Any]) -> None: ...

    def insert(self, record: Mapping[str, Any]) -> Any: ...

    def delete(self, key: Any) -> None: ...

    def verifier_overrides(self) -> dict[str, Any]:
        """Verifier keys that point the check's read at this copy."""

    # Optional, for ``other_table`` and ``side_write``. A copy without them
    # can't test a frame outside its read, so those classes are untestable.
    #
    # def edit_elsewhere(self) -> str:
    #     """Edit a record outside the slice; return what was edited."""
    #
    # def add_elsewhere(self, target: Mapping[str, Any]) -> str:
    #     """Add a row outside the slice for the saved record ``target``."""


class NotApplicable(Exception):
    """The store holds nothing this fault could write (no other table)."""


# -- results -------------------------------------------------------------------


@dataclass
class FaultCase:
    fault: str
    detail: str
    applicable: bool = True
    #: True when the check did not come out verified.
    killed: bool = False
    #: True when the fault could not be built on this copy (not credited).
    untestable: bool = False
    #: Why the fault doesn't apply to this store (it holds no other table).
    not_applicable: str = ""
    outcome: str = ""
    checked: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fault": self.fault,
            "detail": self.detail,
            "applicable": self.applicable,
            "result": (
                "not applicable"
                if not self.applicable
                else "untestable"
                if self.untestable
                else "killed"
                if self.killed
                else "SURVIVED"
            ),
            "outcome": self.outcome,
            "checked": self.checked,
        }


class ClassCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    applicable: int = 0
    killed: int = 0
    #: Applicable faults that could not be built on the test copy.
    untestable: int = 0

    def result(self) -> str:
        """``killed``, ``missed``, ``untestable`` or ``not_applicable``."""

        if self.applicable == 0:
            return "not_applicable"
        if self.untestable:
            return "untestable"
        return "killed" if self.killed == self.applicable else "missed"


class CoverageRecord(BaseModel):
    """``check_coverage.json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = COVERAGE_SCHEMA
    check_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    suite: dict[str, str]
    test_copy: str
    claims: list[str]
    clean_pass: bool
    classes: dict[str, ClassCoverage]
    killed: int
    applicable: int
    cases: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        """The clean save passed and every applicable fault was killed."""

        return (
            self.clean_pass and self.applicable > 0 and self.killed == self.applicable
        )

    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def totals(self) -> dict[str, Any]:
        """What the receipt carries: the totals and this record's digest."""

        return {
            "killed": self.killed,
            "applicable": self.applicable,
            "sha256": self.digest(),
        }

    def class_results(self) -> dict[str, str]:
        """Each claimed part's fault classes and how each came out.

        A class the record doesn't hold (coverage measured by an older suite)
        is ``missing``.
        """

        out: dict[str, str] = {}
        for part in sorted(set(self.claims)):
            for name in CLASSES_OF.get(part, ()):
                cls = self.classes.get(name)
                out[name] = "missing" if cls is None else cls.result()
        return out


class AdmissionPolicy(BaseModel):
    """``admission:`` in ``verify.yaml``.

    The public default is the rule, not a tuned number: the clean save passes
    and, for every part the check claims, every fault class of that part was
    tested and every one of its faults killed. A class missed, untestable on
    the copy, or absent from the record refuses the check whatever the
    totals say. A deployment can also require classes to be applicable
    (``require_classes``, for example every check must claim a frame) and
    refuse coverage measured only on recorded states.
    """

    model_config = ConfigDict(extra="forbid")

    #: Path to ``check_coverage.json``, relative to ``verify.yaml``.
    coverage: str
    #: Fraction of applicable faults that must be killed. 1.0 by default.
    min_coverage: float = Field(default=1.0, ge=0.0, le=1.0)
    require_classes: list[str] = Field(default_factory=list)
    #: Test-copy kinds whose coverage this policy refuses.
    refuse_test_copies: list[str] = Field(default_factory=lambda: ["recorded_states"])

    def problem(self, record: CoverageRecord, check_sha256: str) -> Optional[str]:
        """None when ``record`` admits the check ``check_sha256``."""

        if record.schema_version != COVERAGE_SCHEMA:
            return "coverage_schema"
        if record.check_sha256 != check_sha256:
            return "coverage_for_another_check"
        if record.test_copy in self.refuse_test_copies:
            return "coverage_test_copy_refused"
        if not record.clean_pass:
            return "coverage_clean_save_failed"
        if record.applicable == 0:
            return "coverage_empty"
        # Per class: a check that misses a class it claims is not admitted,
        # whatever its total (OpenAdaptAI/OpenAdapt#1506).
        for name, result in record.class_results().items():
            if result in ("missed", "untestable", "missing"):
                return f"coverage_class_{result}:{name}"
        if record.killed / record.applicable < self.min_coverage:
            return "coverage_below_policy"
        for name in self.require_classes:
            cls = record.classes.get(name)
            if cls is None or cls.applicable == 0:
                return "coverage_class_missing"
        return None


# -- the check's identity ------------------------------------------------------


def _read_shape(section: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in sorted(section.items()):
        if key in _LOCATION_KEYS:
            continue
        if key == "surfaces" and isinstance(value, Mapping):
            out[key] = {name: _read_shape(sub) for name, sub in sorted(value.items())}
        else:
            out[key] = value
    return out


def check_digest(cfg: Any) -> str:
    """SHA-256 of what the check is: its contract, its read and its frame.

    The contract alone (``verify_config_sha256``) does not name the read, and
    a narrower read with the same effects is a weaker check. Where the store
    is and how to sign in to it are left out, so a test copy runs the same
    check as production.
    """

    verifier = cfg.verifier.model_dump(mode="json", exclude_defaults=True)
    frame = cfg.frame.model_dump(mode="json", by_alias=True) if cfg.frame else None
    payload = {
        "effects": [e.model_dump(mode="json") for e in cfg.effects],
        "read": _read_shape(verifier),
        "frame": frame,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# -- fault operators -----------------------------------------------------------


def _perturb(value: Any) -> Any:
    """A value that differs from ``value`` and still looks like one."""

    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 1.0
    text = "" if value is None else str(value)
    if text and text[-1].isdigit():
        return text[:-1] + str((int(text[-1]) + 1) % 10)
    return text + "x" if text else "x"


def _declared(effects: Sequence[Effect], params: Mapping[str, object]) -> list[str]:
    """Fields the contract pins, in declaration order."""

    names: list[str] = []
    for effect in effects:
        bound = effect.resolve(params)
        for key in bound.match:
            if key not in names:
                names.append(key)
        if bound.field and bound.field not in names:
            names.append(bound.field)
    return names


def _target_row(copy_: TestCopy, key: Any) -> Optional[dict[str, Any]]:
    for row in copy_.rows():
        if str(row.get(copy_.key_field)) == str(key):
            return dict(row)
    return None


@dataclass
class _Prepared:
    """The copy after reset, baseline and clean save."""

    key: Any
    target: dict[str, Any]
    #: What an operator wrote, when it chose (a table and column).
    detail: str = ""


Apply = Callable[[TestCopy, _Prepared], Optional[Callable[[], None]]]


def _plan(
    effects: Sequence[Effect],
    params: Mapping[str, object],
    sample: dict[str, Any],
    key_field: str,
) -> list[tuple[str, str, Apply]]:
    """The suite as (class, detail, operator). Operators may return a
    callable to run after the check starts (``delayed``)."""

    fields = [f for f in _declared(effects, params) if f in sample and f != key_field]
    plan: list[tuple[str, str, Apply]] = []

    for name in fields:

        def wrong(c: TestCopy, p: _Prepared, name: str = name) -> None:
            c.update(p.key, {name: _perturb(p.target.get(name))})

        plan.append(("wrong_value", f"{name} changed", wrong))

    def wrong_record(c: TestCopy, p: _Prepared) -> None:
        twin = {k: v for k, v in p.target.items() if k != key_field}
        identity = fields[0] if fields else None
        if identity is not None:
            twin[identity] = _perturb(twin.get(identity))
        c.delete(p.key)
        c.insert(twin)

    plan.append(
        ("wrong_record", "the values saved on a look-alike record", wrong_record)
    )

    def unrelated(c: TestCopy, p: _Prepared) -> None:
        others = [r for r in c.rows() if str(r.get(key_field)) != str(p.key)]
        if not others:
            raise _Untestable("the copy holds no other record to edit")
        victim = others[0]
        name = next(
            (
                k
                for k, v in victim.items()
                if k != key_field
                and isinstance(v, (str, int, float))
                and v not in ("",)
            ),
            None,
        )
        if name is None:
            raise _Untestable("the other record has no editable field")
        c.update(victim[key_field], {name: _perturb(victim[name])})

    plan.append(("unrelated_edit", "another record in the slice edited", unrelated))

    def other_table(c: TestCopy, p: _Prepared) -> None:
        edit = getattr(c, "edit_elsewhere", None)
        if edit is None:
            raise _Untestable("the test copy can't write outside the check's read")
        p.detail = edit()

    plan.append(("other_table", "a record in another table edited", other_table))

    def side_write(c: TestCopy, p: _Prepared) -> None:
        add = getattr(c, "add_elsewhere", None)
        if add is None:
            raise _Untestable("the test copy can't write outside the check's read")
        p.detail = add(p.target)

    plan.append(
        ("side_write", "a row in another table added for the saved record", side_write)
    )

    later = [f for f in fields[len(fields) // 2 :] if f in sample] or fields[-1:]

    def partial(c: TestCopy, p: _Prepared) -> None:
        if not later:
            raise _Untestable("the contract pins no field to leave blank")
        c.update(p.key, {name: "" for name in later})

    plan.append(("partial", f"{', '.join(later)} left blank", partial))

    def duplicate(c: TestCopy, p: _Prepared) -> None:
        c.insert({k: v for k, v in p.target.items() if k != key_field})

    plan.append(("duplicate", "the target saved twice", duplicate))

    def missing(c: TestCopy, p: _Prepared) -> None:
        c.delete(p.key)

    plan.append(("missing", "nothing saved", missing))

    def delayed(c: TestCopy, p: _Prepared) -> Callable[[], None]:
        c.delete(p.key)
        record = {k: v for k, v in p.target.items() if k != key_field}
        return lambda: c.insert(record)

    plan.append(("delayed", "the target lands after the check's timeout", delayed))
    return plan


class _Untestable(Exception):
    pass


# -- the gate ------------------------------------------------------------------


def _check_timeout(effects: Sequence[Effect]) -> float:
    return max((e.timeout_s for e in effects), default=5.0)


def _point_at(verifier: Any, overrides: Mapping[str, Any]) -> Any:
    """``verifier`` with its store location replaced, surfaces included."""

    if verifier.surfaces:
        return verifier.model_copy(
            update={
                "surfaces": {
                    name: _point_at(sub, overrides)
                    for name, sub in verifier.surfaces.items()
                }
            }
        )
    return verifier.model_copy(update=dict(overrides))


def run_gate(
    cfg: Any,
    params: Mapping[str, object],
    copy_: TestCopy,
    *,
    claims: Sequence[str] = ("delta", "frame"),
    workdir: Optional[Path] = None,
    delay_margin_s: float = 1.0,
) -> CoverageRecord:
    """Run the public fault suite on ``copy_`` and return the coverage record.

    ``cfg`` is the check's parsed ``verify.yaml``. Its read is pointed at the
    copy with :meth:`TestCopy.verifier_overrides`; nothing else changes.
    """

    from openadapt_flow.verify_run import capture_baseline, verify_run

    effects = list(cfg.effects)
    if not any(
        e.kind in (EffectKind.RECORD_WRITTEN, EffectKind.FIELD_EQUALS) for e in effects
    ):
        raise GateError("the gate needs a record_written or field_equals effect")
    verifier = _point_at(cfg.verifier, copy_.verifier_overrides())
    # The gate is what produces the coverage an admission policy asks for.
    local = cfg.model_copy(update={"verifier": verifier, "admission": None})
    own = Path(tempfile.mkdtemp(prefix="fault-gate-")) if workdir is None else workdir
    own.mkdir(parents=True, exist_ok=True)
    runs = 0

    def prepare() -> tuple[Path, _Prepared]:
        nonlocal runs
        runs += 1
        copy_.reset()
        baseline = own / f"baseline-{runs}.json"
        capture_baseline(local, params, baseline)
        key = copy_.save()
        target = _target_row(copy_, key)
        if target is None:
            raise GateError("the clean save left no record the copy can find")
        return baseline, _Prepared(key, target)

    def judge(baseline: Path) -> dict[str, Any]:
        return verify_run(
            local,
            params=params,
            run_record={
                "executor_kind": "script",
                "executor_id": "openadapt-fault-gate",
            },
            authorization_ref="openadapt-fault-gate",
            baseline_path=baseline,
            private_dir=own / f"run-{runs}",
        )["receipt"]

    baseline, prepared = prepare()
    clean = judge(baseline)
    clean_pass = clean["outcome"] == "verified"
    cases = [
        FaultCase(
            "clean",
            "the save as the app makes it",
            applicable=False,
            killed=not clean_pass,
            outcome=clean["outcome"],
            checked=dict(clean.get("checked") or {}),
        )
    ]
    for fault, detail, apply in _plan(
        effects, params, prepared.target, copy_.key_field
    ):
        case = FaultCase(fault, detail, applicable=CLAIM_OF[fault] in claims)
        cases.append(case)
        if not case.applicable:
            continue
        try:
            baseline, prepared = prepare()
            later = apply(copy_, prepared)
        except NotApplicable as exc:
            case.applicable = False
            case.not_applicable = str(exc)
            case.detail += f" ({exc})"
            continue
        except _Untestable as exc:
            case.untestable = True
            case.detail += f" ({exc})"
            continue
        if prepared.detail:
            case.detail += f" ({prepared.detail})"
        timer: Optional[threading.Timer] = None
        if later is not None:
            timer = threading.Timer(_check_timeout(effects) + delay_margin_s, later)
            timer.start()
        try:
            receipt = judge(baseline)
        finally:
            if timer is not None:
                timer.join()
        case.outcome = receipt["outcome"]
        case.checked = dict(receipt.get("checked") or {})
        case.killed = receipt["outcome"] != "verified"
    copy_.reset()

    classes: dict[str, ClassCoverage] = {}
    for case in cases[1:]:
        entry = classes.setdefault(case.fault, ClassCoverage())
        if case.applicable:
            entry.applicable += 1
            if case.untestable:
                entry.untestable += 1
            elif case.killed:
                entry.killed += 1
    return CoverageRecord(
        check_sha256=check_digest(cfg),
        suite={"id": SUITE_ID, "version": SUITE_VERSION},
        test_copy=copy_.name,
        claims=sorted(set(claims)),
        clean_pass=clean_pass,
        classes=classes,
        killed=sum(c.killed for c in classes.values()),
        applicable=sum(c.applicable for c in classes.values()),
        cases=[c.as_dict() for c in cases],
    )


def write_coverage(record: CoverageRecord, out_dir: Path) -> Path:
    path = Path(out_dir) / COVERAGE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    return path


def load_coverage(path: Path) -> CoverageRecord:
    try:
        return CoverageRecord.model_validate(json.loads(Path(path).read_text()))
    except (OSError, ValueError) as exc:
        raise GateError(f"cannot read coverage record {path}: {exc}") from exc


# -- public test copies ----------------------------------------------------------


class MemoryRestCopy:
    """An in-memory record store served over HTTP: the sample test copy.

    Serves ``GET <records_path>`` as ``{records_key: [...]}``. ``seed`` is the
    state before the save, ``target`` the record the clean save writes.
    """

    name = "rest_memory"

    def __init__(
        self,
        seed: Sequence[Mapping[str, Any]],
        target: Mapping[str, Any],
        *,
        records_path: str = "/api/db",
        records_key: Optional[str] = "records",
        key_field: str = "id",
    ) -> None:
        self.key_field = key_field
        self._seed = [dict(r) for r in seed]
        self._target = dict(target)
        self._records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._next = 1000
        self.records_path = records_path
        self.records_key = records_key
        self._server, self.base_url = self._serve()

    def _serve(self) -> tuple[Any, str]:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        store = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                if self.path != store.records_path:
                    self.send_response(404)
                    self.end_headers()
                    return
                with store._lock:
                    rows = copy.deepcopy(store._records)
                body = rows if store.records_key is None else {store.records_key: rows}
                data = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args: Any) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def reset(self) -> None:
        with self._lock:
            self._records = [dict(r) for r in self._seed]

    def save(self) -> Any:
        return self.insert(self._target)

    def rows(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._records)

    def update(self, key: Any, changes: Mapping[str, Any]) -> None:
        with self._lock:
            for row in self._records:
                if str(row.get(self.key_field)) == str(key):
                    row.update(changes)

    def insert(self, record: Mapping[str, Any]) -> Any:
        with self._lock:
            self._next += 1
            row = {**dict(record), self.key_field: self._next}
            self._records.append(row)
            return self._next

    def delete(self, key: Any) -> None:
        with self._lock:
            self._records = [
                r for r in self._records if str(r.get(self.key_field)) != str(key)
            ]

    def verifier_overrides(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "records_path": self.records_path,
            "records_key": self.records_key,
        }

    def edit_elsewhere(self) -> str:
        raise NotApplicable("the store holds one collection")

    def add_elsewhere(self, target: Mapping[str, Any]) -> str:
        raise NotApplicable("the store holds one collection")


class SqliteCopy:
    """A copy of a SQLite database file, one table as the slice.

    ``source`` is copied before every case. ``save`` runs the clean save's
    SQL (or inserts ``target``). The check's read runs against the copy.

    The database's other tables are outside the slice. ``subject_field`` is
    the column they refer to the saved record by (OpenEMR's ``pid``). Unset,
    it is the one column the slice's table shares with other tables whose
    name ends in ``id`` (not the key).
    """

    name = "sqlite"

    def __init__(
        self,
        source: Path,
        table: str,
        target: Mapping[str, Any],
        *,
        key_field: str = "id",
        saved_key: Any = None,
        subject_field: Optional[str] = None,
        workdir: Optional[Path] = None,
    ) -> None:
        import re

        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", table) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]{0,63}", key_field
        ):
            raise GateError("table and key field must be plain identifiers")
        if subject_field is not None and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]{0,63}", subject_field
        ):
            raise GateError("subject field must be a plain identifier")
        self.source = Path(source)
        self.table = table
        self.key_field = key_field
        self.subject_field = subject_field
        self._target = {k: v for k, v in dict(target).items() if k != key_field}
        #: When ``source`` already holds the saved record (a database copied
        #: after the demonstration), its key: reset removes it.
        self.saved_key = saved_key
        base = (
            Path(tempfile.mkdtemp(prefix="fault-gate-db-"))
            if workdir is None
            else workdir
        )
        self.path = base / "copy.sqlite"

    def _connect(self) -> Any:
        import sqlite3

        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _columns(self, con: Any) -> set[str]:
        return {row[1] for row in con.execute(f'PRAGMA table_info("{self.table}")')}

    def reset(self) -> None:
        import shutil

        self.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.source, self.path)
        if self.saved_key is not None:
            self.delete(self.saved_key)

    def save(self) -> Any:
        return self.insert(self._target)

    def rows(self) -> list[dict[str, Any]]:
        con = self._connect()
        try:
            return [dict(r) for r in con.execute(f'SELECT * FROM "{self.table}"')]
        finally:
            con.close()

    def _names(self, con: Any, names: Sequence[str]) -> list[str]:
        known = self._columns(con)
        bad = [n for n in names if n not in known]
        if bad:
            raise GateError(f"unknown column(s) {bad} in {self.table}")
        return list(names)

    def update(self, key: Any, changes: Mapping[str, Any]) -> None:
        con = self._connect()
        try:
            names = self._names(con, list(changes))
            sets = ", ".join(f'"{n}" = ?' for n in names)
            con.execute(
                f'UPDATE "{self.table}" SET {sets} WHERE "{self.key_field}" = ?',
                [changes[n] for n in names] + [key],
            )
            con.commit()
        finally:
            con.close()

    def insert(self, record: Mapping[str, Any]) -> Any:
        con = self._connect()
        try:
            names = self._names(con, [k for k in record if k != self.key_field])
            cur = con.execute(
                f'INSERT INTO "{self.table}" ({", ".join(chr(34) + n + chr(34) for n in names)}) '
                f"VALUES ({', '.join('?' for _ in names)})",
                [record[n] for n in names],
            )
            con.commit()
            return cur.lastrowid
        finally:
            con.close()

    def delete(self, key: Any) -> None:
        con = self._connect()
        try:
            con.execute(
                f'DELETE FROM "{self.table}" WHERE "{self.key_field}" = ?', [key]
            )
            con.commit()
        finally:
            con.close()

    def verifier_overrides(self) -> dict[str, Any]:
        return {"sqlite_database": str(self.path)}

    def _others(self, con: Any) -> list[str]:
        """The database's other tables, in name order."""

        return sorted(
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
            if row[0] != self.table
        )

    @staticmethod
    def _table_columns(con: Any, table: str) -> list[str]:
        return [row[1] for row in con.execute(f'PRAGMA table_info("{table}")')]

    def edit_elsewhere(self) -> str:
        """Edit one value of one row in the first other table that has one."""

        con = self._connect()
        try:
            others = self._others(con)
            if not others:
                raise NotApplicable("the database holds no other table")
            for table in others:
                for row in con.execute(
                    f'SELECT rowid AS _r, * FROM "{table}" LIMIT 20'
                ):
                    for name in row.keys()[1:]:
                        value = row[name]
                        if name == "id" or not isinstance(value, (str, int, float)):
                            continue
                        if value == "":
                            continue
                        con.execute(
                            f'UPDATE "{table}" SET "{name}" = ? WHERE rowid = ?',
                            [_perturb(value), row["_r"]],
                        )
                        con.commit()
                        return f"{table}.{name}"
            raise _Untestable("no other table holds a row with an editable value")
        finally:
            con.close()

    def _subject(self, con: Any, others: Sequence[str]) -> Optional[str]:
        if self.subject_field is not None:
            return self.subject_field
        own = set(self._table_columns(con, self.table))
        shared = sorted(
            {
                name
                for table in others
                for name in self._table_columns(con, table)
                if name in own
                and name not in (self.key_field, "id", "uuid")
                and name.lower().endswith("id")
            }
        )
        return shared[0] if len(shared) == 1 else None

    def add_elsewhere(self, target: Mapping[str, Any]) -> str:
        """Add a row for the saved record to the first other table that
        refers to records by the subject field and accepts one."""

        import sqlite3

        con = self._connect()
        try:
            others = self._others(con)
            if not others:
                raise NotApplicable("the database holds no other table")
            subject = self._subject(con, others)
            if subject is None:
                raise _Untestable(
                    "no subject_field: which column of the other tables names "
                    "the saved record is unknown"
                )
            value = target.get(subject)
            if value in (None, ""):
                raise _Untestable(f"the saved record has no {subject}")
            for table in others:
                if subject not in self._table_columns(con, table):
                    continue
                try:
                    con.execute(
                        f'INSERT INTO "{table}" ("{subject}") VALUES (?)', [value]
                    )
                except sqlite3.DatabaseError:
                    continue  # a NOT NULL column without a default
                con.commit()
                return f"{table}.{subject}"
            raise _Untestable(f"no other table accepts a row by {subject} alone")
        finally:
            con.close()
