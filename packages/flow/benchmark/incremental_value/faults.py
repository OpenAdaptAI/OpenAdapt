"""What the harness does around an item: the hook interface, the
perturbations, and the loader for the blind cases' ops.

Both arms get the same hooks at the same moments. An item's hooks are built
before the arm starts it, from ``Item.kind``:

- ``perturbation``: :func:`perturbation` below (written with the harness, not
  blind);
- ``fault``: ``build(case) -> Hooks`` from the private module named by
  ``INCREMENTAL_VALUE_FAULT_OPS`` (a file path). That module is written from
  ``faultset6.yaml`` by an agent other than the harness author, and lives with
  the set in openadapt-internal.

Moments:

- ``prepare``: before the arm's baseline and before the agent starts. It may
  create charts, change ``ctx.source_text`` (the referral the agent reads)
  and ``ctx.intended`` (the truth).
- ``form_mutators``: applied to the save request's form fields on its way to
  OpenEMR (a buggy form plugin). Each gets the ordered field list
  ``[(name, value), ...]`` and returns it.
- ``before_save``: run when the save request is intercepted, before it is
  sent.
- ``after_save``: ``(delay_s, fn)``, run on a thread that long after the
  save's response. The harness waits for every one before the oracle reads.
- ``save_timeout``: the save lands, but the browser sees the request time out.
- ``restart_after_save``: the agent and its browser are killed right after
  the save's response; a fresh agent starts the same item.

Writes as a legitimate user go through :meth:`Ctx.as_legit`, which records
the rows they touched so the oracle excuses them, and only them.
"""

from __future__ import annotations

import importlib.util
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .oracle import DbOracle, Legit

FormFields = list[tuple[str, str]]


@dataclass
class Hooks:
    prepare: list[Callable[["Ctx"], None]] = field(default_factory=list)
    form_mutators: list[Callable[[FormFields], FormFields]] = field(default_factory=list)
    before_save: list[Callable[["Ctx"], None]] = field(default_factory=list)
    after_save: list[tuple[float, Callable[["Ctx"], None]]] = field(default_factory=list)
    save_timeout: bool = False
    restart_after_save: bool = False
    #: Ops that could not be implemented as written: (op, reason).
    not_run: list[tuple[str, str]] = field(default_factory=list)


class Ctx:
    """One item's view of the fixture for hook code."""

    def __init__(self, item: Any, app: Any, oracle: DbOracle, targets: dict[str, Any], api: Callable[[str], Any]) -> None:
        self.item = item
        self.app = app
        self.oracle = oracle
        self.targets = dict(targets)
        self._api = api
        self.source_text: str = item.source_text
        self.intended: Optional[dict[str, str]] = item.intended
        self.legit = Legit()
        self.events: list[dict[str, Any]] = []
        #: One entry per `fault_write`: whether it moved any of its tables.
        self.faults_landed: list[bool] = []
        self.saves = 0
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    # -- channels --------------------------------------------------------------

    def sql(self, statement: str) -> str:
        """A direct database change (no audit entry): the ``none`` actor."""

        return self.app.sql(statement)

    def fault_write(self, tables: list[str], fn: Callable[[], Any]) -> Any:
        """Run a fault's write and record whether it landed: whether any of
        ``tables`` moved. A harmful case whose fault never landed (say an
        ``INSERT IGNORE`` that found the row already there) is reported as
        not run, not scored (PREREGISTRATION_2.md)."""

        before = {t: self.oracle.read_table(t) for t in tables}
        out = fn()
        moved = [t for t in tables if self.oracle.read_table(t) != before[t]]
        self.faults_landed.append(bool(moved))
        self.log(fault_write=tables, landed=bool(moved))
        return out

    def api(self, user: str, method: str, path: str, body: Optional[dict] = None) -> Any:
        """OpenEMR's REST API as ``user`` (``openadapt_actor`` or ``clinician2``),
        with that user's own token, so OpenEMR's audit log names them."""

        s = self._api(user)
        resp = s.request(method, f"{self.app.api_url}/apis/default/api{path}", json=body)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"{user} {method} {path}: HTTP {resp.status_code} {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def as_legit(self, tables: list[str], fn: Callable[[], Any]) -> Any:
        """Run a legitimate user's write; the rows it changed in ``tables``
        are excused by the oracle. Serialised with other legit writes."""

        with self._lock:
            before = {t: self.oracle.read_table(t) for t in tables}
            out = fn()
            mine = self.new_pid()
            for t in tables:
                after = self.oracle.read_table(t)
                for k, r in after.items():
                    if before[t].get(k) != r and not self._is_items_own(t, r, mine):
                        self.legit.add(t, k)
                for k in set(before[t]) - set(after):
                    self.legit.add(t, k)
            self.legit.tables.update(tables)
        return out

    def _is_items_own(self, table: str, row: dict[str, str], pid: Optional[int]) -> bool:
        """A row of this item's own save (the person's chart and the save's
        side rows), which a legit write that overlaps it in time must never
        excuse. Other rows on the new chart, such as a colleague adding a
        problem to it, are the colleague's and are excused."""

        from .oracle import SAVE_SIDE_TABLES, _is_person

        if table == "patient_data":
            return _is_person(row, self.item.person)
        side = SAVE_SIDE_TABLES.get(table)
        return pid is not None and side is not None and row.get(side[0]) == str(pid)

    def new_pid(self) -> Optional[int]:
        """The pid of the chart this item created, if one exists yet."""

        email = self.item.person.get("email", "")
        out = self.app.sql(
            "SELECT pid FROM openemr.patient_data WHERE email = '"
            + email.replace("'", "''")
            + "' ORDER BY id DESC LIMIT 1;"
        ).split()
        return int(out[0]) if out else None

    def create_chart(self, fields: dict[str, str]) -> int:
        """A chart created before the item (through the REST API, as the
        seed is): returns its pid, with the history row the form would
        write. ``fields`` as a referral states them (``state_label``)."""

        cid = self.app.create_chart(fields, who="prepare")
        pid = int(self.app.sql(f"SELECT pid FROM openemr.patient_data WHERE id = {int(cid)};").split()[0])
        self.app.sql(f"INSERT INTO openemr.history_data (pid, date) VALUES ({pid}, NOW());")
        return pid

    def log(self, **event: Any) -> None:
        self.events.append({"t": round(time.time(), 2), **event})

    # -- used by the runner ------------------------------------------------------

    def run_after_save(self, hooks: Hooks) -> None:
        self.saves += 1
        if self.saves > 1:
            return
        for delay, fn in hooks.after_save:
            def go(delay: float = delay, fn: Callable[["Ctx"], None] = fn) -> None:
                time.sleep(delay)
                try:
                    fn(self)
                    self.log(op="after_save", delay_s=delay, ok=True)
                except Exception as exc:  # noqa: BLE001 - recorded
                    self.log(op="after_save", delay_s=delay, ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
            t = threading.Thread(target=go, daemon=True)
            t.start()
            self._threads.append(t)

    def wait_background(self, timeout_s: float = 150.0) -> bool:
        deadline = time.time() + timeout_s
        for t in self._threads:
            t.join(max(0.0, deadline - time.time()))
        return all(not t.is_alive() for t in self._threads)

    def max_delay(self, hooks: Hooks) -> float:
        return max([d for d, _ in hooks.after_save] or [0.0])


# -- harness perturbations (not blind) -------------------------------------------


def _other(ctx: Ctx, n: int) -> int:
    pids = ctx.targets["existing_pids"]
    return int(pids[(ctx.item.seq + n) % len(pids)])


def _clinician2_edit_phone(ctx: Ctx, n: int) -> None:
    pid = _other(ctx, n)
    uuid = ctx.app.sql(
        "SELECT LOWER(INSERT(INSERT(INSERT(INSERT(HEX(uuid),9,0,'-'),14,0,'-'),19,0,'-'),24,0,'-')) "
        f"FROM openemr.patient_data WHERE pid={pid};"
    ).strip()
    phone = f"555-02{(ctx.item.seq + n) % 100:02d}"
    ctx.as_legit(
        ["patient_data"],
        lambda: ctx.api("clinician2", "PUT", f"/patient/{uuid}", {"phone_home": phone}),
    )
    ctx.log(op="clinician2_edit_phone", pid=pid)


def _clinician2_add_allergy(ctx: Ctx, n: int) -> None:
    pid = _other(ctx, n)
    uuid = ctx.app.sql(
        "SELECT LOWER(INSERT(INSERT(INSERT(INSERT(HEX(uuid),9,0,'-'),14,0,'-'),19,0,'-'),24,0,'-')) "
        f"FROM openemr.patient_data WHERE pid={pid};"
    ).strip()
    ctx.as_legit(
        ["lists", "lists_touch"],
        lambda: ctx.api(
            "clinician2", "POST", f"/patient/{uuid}/allergy",
            {"title": "Latex", "begdate": "2026-10-01", "diagnosis": "", "reaction": "", "severity_al": ""},
        ),
    )
    ctx.log(op="clinician2_add_allergy", pid=pid)


def _clinician2_create_chart(ctx: Ctx, n: int) -> None:
    seq = ctx.item.seq
    body = {
        "title": "Mr.", "fname": "Corwin", "lname": f"Staffwalk{seq}", "DOB": "1971-05-0" + str(1 + n % 9),
        "sex": "Male", "street": f"{10 + seq} STATION AVENUE", "city": "EXAMPLETON", "state": "MA",
        "postal_code": "02139", "phone_home": "555-0199", "email": f"corwin.staffwalk.{seq}@example.invalid",
        "country_code": "USA",
    }
    ctx.as_legit(["patient_data", "patient_history", "history_data"], lambda: ctx.api("clinician2", "POST", "/patient", body))
    ctx.log(op="clinician2_create_chart")


def _bookkeeping_burst(ctx: Ctx, n: int) -> None:
    # OpenEMR's own background bookkeeping: a scheduler tick and an audit row.
    ctx.app.sql(
        "UPDATE openemr.background_services SET next_run = NOW() + INTERVAL 1 MINUTE WHERE active = 1 LIMIT 1;"
        "INSERT INTO openemr.log (date, event, category, user, groupname, comments, patient_id, success) "
        "VALUES (NOW(), 'other', 'scheduler', 'admin', 'Default', 'background service tick', 0, 1);"
    )
    ctx.log(op="bookkeeping_burst")


def perturbation(name: str, variant: int) -> Hooks:
    # Run 2 repeats each perturbation 13 times; the 4 variants cycle.
    variant %= 4
    h = Hooks()
    if name == "delayed_legit_write":
        delay = (15.0, 25.0, 32.0, 40.0)[variant]
        fn = _clinician2_edit_phone if variant % 2 == 0 else _bookkeeping_burst
        h.after_save.append((delay, lambda c, fn=fn: fn(c, 1)))
    elif name == "restart_after_save":
        h.restart_after_save = True
    elif name == "concurrent_staff_edit":
        fn = (_clinician2_edit_phone, _clinician2_add_allergy, _clinician2_create_chart, _clinician2_edit_phone)[variant]
        if variant % 2 == 0:
            h.before_save.append(lambda c, fn=fn: fn(c, 2))
        else:
            h.after_save.append((3.0, lambda c, fn=fn: fn(c, 2)))
    elif name == "timeout_after_save":
        h.save_timeout = True
    else:
        raise ValueError(name)
    return h


# -- blind cases ---------------------------------------------------------------


def load_cases() -> tuple[list[dict[str, Any]], str, Any]:
    """(cases, sha256, ops module) from the private set; the module's
    ``build(case) -> Hooks``."""

    import hashlib

    import yaml

    path = os.environ["INCREMENTAL_VALUE_FAULTSET"]
    raw = open(path, "rb").read()
    sha = hashlib.sha256(raw).hexdigest()
    cases = yaml.safe_load(raw)["cases"]
    ops_path = os.environ.get("INCREMENTAL_VALUE_FAULT_OPS")
    module = None
    if ops_path:
        spec = importlib.util.spec_from_file_location("incremental_value_fault_ops", ops_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return cases, sha, module


def describe(h: Hooks) -> dict[str, Any]:
    return json.loads(
        json.dumps(
            {
                "prepare": len(h.prepare),
                "form_mutators": len(h.form_mutators),
                "before_save": len(h.before_save),
                "after_save_delays": [d for d, _ in h.after_save],
                "save_timeout": h.save_timeout,
                "restart_after_save": h.restart_after_save,
                "not_run": h.not_run,
            }
        )
    )
