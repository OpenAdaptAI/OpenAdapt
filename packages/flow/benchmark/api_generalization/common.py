"""Shared pieces: the app adapter shape, the independent judge, the SQL read-back.

An app adapter (``apps/<name>.py``) gives the harness what it can't know
about an app: how to start it, how a scripted person fills the form, how to
read its records back, and the inputs to try. Everything the API step itself
does comes from the frozen compiler and runtime, unchanged.

Two read-backs are kept apart on purpose:

- the **engine's record check** (``Effect`` list on the save step, read by
  :class:`ExecSqlVerifier`) is what the run itself uses to confirm the save;
- the **judge** (:func:`judge`) reads the whole record table before and after
  each run with its own query, and decides whether the run left the app in
  the expected state. A run is scored only by the judge.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from openadapt_flow.runtime.effects.sql import SqlRecordVerifier


def sh(args: list[str], *, input: Optional[str] = None, timeout: float = 120, check: bool = True) -> str:
    r = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])}... failed ({r.returncode}): {r.stderr[-800:]}")
    return r.stdout


def podman(*args: str, **kw: Any) -> str:
    return sh(["podman", *args], **kw)


class ExecSqlVerifier(SqlRecordVerifier):
    """The engine's record check, read through ``podman exec`` (no DB driver on the host).

    ``fetch`` runs the adapter's fixed read-only SELECT and returns rows as
    dicts; any failure reads as unreadable (INDETERMINATE), as in the parent.
    """

    def __init__(self, fetch: Callable[[str], list[dict[str, Any]]], query: str, **kw: Any) -> None:
        super().__init__(connect=lambda: None, query=query, **kw)
        self._fetch = fetch

    def _fetch_records(self) -> Optional[list[dict[str, Any]]]:
        try:
            rows = self._fetch(self.query)
        except Exception:  # noqa: BLE001 - an unreadable record system is INDETERMINATE
            return None
        return [{k: ("" if v is None else str(v)) for k, v in r.items()} for r in rows]


@dataclass
class Case:
    """One input or condition for a workflow, and what the app should end up holding."""

    id: str
    kind: str  # normal | blank | dup_values | changed_default | expired_session | fault_reject | fault_partial | fault_duplicate | validation
    params: dict[str, str]
    expect_new: int  # records the run should leave (fault_duplicate: what the fixture causes)
    expect_fields: dict[str, str] = field(default_factory=dict)  # judge columns -> value on each new record
    fault: Optional[str] = None  # proxy mode
    note: str = ""


def judge(before: list[dict[str, Any]], after: list[dict[str, Any]], case: Case, key: str = "id") -> dict[str, Any]:
    """Compare the full table before and after one run."""

    b = {str(r[key]): r for r in before}
    a = {str(r[key]): r for r in after}
    new = [a[k] for k in a if k not in b]
    gone = [k for k in b if k not in a]
    changed = [k for k in b if k in a and json.dumps(b[k], sort_keys=True, default=str) != json.dumps(a[k], sort_keys=True, default=str)]
    mism: list[str] = []
    for r in new:
        for col, want in case.expect_fields.items():
            got = "" if r.get(col) is None else str(r.get(col))
            if got != want:
                mism.append(f"{col}: want {want!r} got {got!r}")
    ok_state = len(new) == case.expect_new and not mism and not gone and not changed
    return {
        "new_records": len(new),
        "field_mismatches": mism,
        "collateral": {"deleted": gone, "changed": changed},
        "state_ok": ok_state,
        "new_ids": [str(r[key]) for r in new],
    }


def classify(case: Case, state: dict[str, Any], engine_success: Optional[bool], transaction_outcome: Optional[str]) -> dict[str, Any]:
    """Score one run from the judge's view of the app, then the run's own claim.

    - ``incorrect_write``: the app holds something the run should not have
      caused: a wrong value, an extra record, a changed or deleted record.
      In F3 the fixture's second delivery is not counted against the run.
    - ``false_success``: the run reported success and the judge disagrees.
    - ``correct``: the app holds what it should and the run's claim matches
      (success exactly when the expected write happened, or
      RECONCILIATION_REQUIRED after a lost response in F2). In F3 the app
      may hold two records (the run must not report success) or one, when
      the app itself refuses the second delivery (then the run's claim must
      be true either way).
    - ``stopped_after_right_write``: the right record was written but the run
      did not report success (a safe stop that costs a person a look).
    - ``missed_write``: nothing wrong was written but an expected record is
      missing (an honest stop).
    """

    collateral = bool(state["collateral"]["deleted"] or state["collateral"]["changed"])
    n = state["new_records"]
    mism = bool(state["field_mismatches"])
    if case.kind == "fault_duplicate":
        right = n in (1, 2) and not mism and not collateral
        incorrect = n > 2 or mism or collateral
        correct = right and (not engine_success if n == 2 else True)
        false_success = bool(engine_success) and not (right and n == 1)
        return {"correct": correct, "incorrect_write": incorrect, "false_success": false_success,
                "stopped_after_right_write": right and n == 1 and not engine_success, "missed_write": n == 0}
    right = state["state_ok"]
    should_succeed = case.expect_new >= 1
    correct = right and bool(engine_success) == should_succeed
    if case.kind == "fault_partial" and right and transaction_outcome == "RECONCILIATION_REQUIRED":
        correct = True
    incorrect = n > case.expect_new or mism or collateral
    return {"correct": correct, "incorrect_write": incorrect,
            "false_success": bool(engine_success) and not (right and should_succeed),
            "stopped_after_right_write": right and should_succeed and not engine_success and not correct,
            "missed_write": (not incorrect) and n < case.expect_new}


class AppBase:
    """Defaults shared by the adapters. Subclasses fill in the app-specific parts."""

    name = ""
    workflow = ""
    upstream_port = 0
    proxy_port = 0
    form_path = ""
    save_path_regex = ""
    save_body_exclude = ""
    save_body_require = ""
    login_required = True
    key = "id"

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.proxy_port}"

    # lifecycle ---------------------------------------------------------------
    def up(self) -> None:
        raise NotImplementedError

    def down(self) -> None:
        raise NotImplementedError

    def wait_http(self, path: str = "/", timeout_s: float = 600) -> None:
        import urllib.request

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.upstream_port}{path}", timeout=5) as r:
                    if r.status < 500:
                        return
            except Exception:  # noqa: BLE001
                time.sleep(3)
        raise RuntimeError(f"{self.name} did not answer on {path}")

    # browser -----------------------------------------------------------------
    def login(self, page: Any) -> None:
        raise NotImplementedError

    def open_form(self, page: Any) -> None:
        page.goto(self.base + self.form_path)
        page.wait_for_load_state("load")
        page.wait_for_timeout(800)

    def record(self, rec: Any, page: Any, values: dict[str, str]) -> None:
        raise NotImplementedError

    def expire_session(self, page: Any) -> None:
        """End the signed-in session on the server (default: drop the browser's cookies)."""
        page.context.clear_cookies()

    def change_default(self) -> None:
        raise NotImplementedError

    def restore_default(self) -> None:
        raise NotImplementedError

    # records -----------------------------------------------------------------
    def rows(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def verifier(self) -> Any:
        raise NotImplementedError

    def effects(self) -> list[Any]:
        raise NotImplementedError

    # inputs ----------------------------------------------------------------------
    #: The parameter made unique per run (so repeated runs never collide with
    #: an app's own duplicate rules), and its judge column.
    unique_param = ""
    normal_values: list[dict[str, str]] = []
    blank_values: dict[str, str] = {}
    dup_values: dict[str, str] = {}
    invalid_values: dict[str, str] = {}
    invalid_note = ""
    #: Judge column whose form default ``change_default`` changes, and the
    #: value a person's save holds after the change.
    default_column = ""
    changed_default_value = ""

    def recording_values(self) -> dict[str, str]:
        return dict(self.normal_values[0])

    def expect_fields(self, values: dict[str, str]) -> dict[str, str]:
        raise NotImplementedError

    def unique(self, case: Case, tag: str) -> Case:
        if case.kind == "validation":
            return case  # the invalid value must stay exactly as written
        params = dict(case.params)
        params[self.unique_param] = f"{params[self.unique_param]} {tag}"
        fields = dict(case.expect_fields)
        if fields:
            col = self.unique_column()
            fields[col] = self.expect_fields(params)[col]
        return Case(case.id, case.kind, params, case.expect_new, fields, case.fault, case.note)

    def unique_column(self) -> str:
        cols = self.expect_fields({k: "\x00" if k == self.unique_param else "" for k in self.normal_values[0]})
        return next(k for k, v in cols.items() if v == "\x00")

    def cases(self) -> list[Case]:
        n = self.normal_values
        cases = [Case(f"N{i + 1}", "normal", v, 1, self.expect_fields(v)) for i, v in enumerate(n)]
        cases.append(Case("B1", "blank", self.blank_values, 1, self.expect_fields(self.blank_values),
                          note="optional fields left blank"))
        cases.append(Case("D1", "dup_values", self.dup_values, 1, self.expect_fields(self.dup_values),
                          note="the same value in several fields"))
        if self.default_column:
            ef = self.expect_fields(n[0]) | {self.default_column: self.changed_default_value}
            cases.append(Case("C1", "changed_default", n[0], 1, ef,
                              note=f"the app's default for {self.default_column} changed after recording"))
        if self.login_required:
            cases.append(Case("S1", "expired_session", n[1], 0, {}, note="session ended on the server before the run"))
        cases.append(Case("F1", "fault_reject", n[2], 0, {}, fault="reject", note="503 before the app sees the save"))
        cases.append(Case("F2", "fault_partial", n[0], 1, self.expect_fields(n[0]), fault="partial",
                          note="the app commits, the response is lost (502)"))
        cases.append(Case("F3", "fault_duplicate", n[1], 2, self.expect_fields(n[1]), fault="duplicate",
                          note="the save reaches the app twice"))
        cases.append(Case("V1", "validation", self.invalid_values, 0, {}, note=self.invalid_note))
        return cases


def center(page: Any, rec: Any, selector: str) -> tuple[int, int]:
    """The control's center, scrolling (as a recorded wheel event) until it is in view."""

    loc = page.locator(selector).first
    last_y = None
    for _ in range(8):
        box = loc.bounding_box()
        if box is None:
            raise RuntimeError(f"not rendered: {selector}")
        h = page.viewport_size["height"]
        cy = box["y"] + box["height"] / 2
        if 20 <= cy <= h - 20 and (box["y"] >= 0 and box["y"] + box["height"] <= h or box["y"] == last_y):
            return int(box["x"] + box["width"] / 2), int(cy)
        last_y = box["y"]
        page.mouse.move(640, 400)  # the wheel scrolls what is under the pointer; a move is not an event
        rec.scroll(0, 300 if cy > h / 2 else -300)
    raise RuntimeError(f"could not scroll to {selector}")


def rec_text(rec: Any, page: Any, selector: str, value: str, param: str, *, clear: bool = False) -> None:
    rec.click(*center(page, rec, selector))
    if clear:
        rec.press("Control+a")
    rec.type_text(value, param=param)


def rec_select(rec: Any, page: Any, selector: str, label: str, param: str) -> None:
    """A person picks an option by typing its label into the focused select.

    No Enter: on a closed select, Enter submits the form in Chromium.
    """

    rec.click(*center(page, rec, selector))
    rec.type_text(label, param=param)


def rec_click(rec: Any, page: Any, selector: str) -> None:
    rec.click(*center(page, rec, selector))


def mysql_rows(container: str, user: str, password: str, db: str, query: str) -> list[dict[str, Any]]:
    """Rows from MariaDB/MySQL through the container's own client (tab-separated with a header)."""

    out = podman("exec", "-i", container, "mariadb", f"-u{user}", f"-p{password}", "-B", db, input=query)
    lines = out.splitlines()
    if not lines:
        return []
    cols = lines[0].split("\t")

    def unesc(v: str) -> Any:
        if v == "NULL":
            return None
        return v.replace("\\n", "\n").replace("\\t", "\t").replace("\\0", "\0").replace("\\\\", "\\")

    return [dict(zip(cols, [unesc(v) for v in ln.split("\t")])) for ln in lines[1:]]


def rec_select2_click(rec: Any, page: Any, select_id: str, label: str) -> None:
    """A select2 widget: open it and click the option with this label (a fixed choice, not a parameter)."""

    rec_click(rec, page, f"#select2-{select_id}-container")
    page.wait_for_timeout(300)
    opt = page.locator(".select2-results__option", has_text=label)
    exact = [i for i in range(opt.count()) if opt.nth(i).inner_text().strip() == label]
    if len(exact) != 1:
        raise RuntimeError(f"select2 option {label!r}: {len(exact)} exact matches")
    box = opt.nth(exact[0]).bounding_box()
    rec.click(int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
