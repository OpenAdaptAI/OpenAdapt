"""OpenEMR 7.0.3: edit a patient's home phone through the demographics form.

The harness seeds synthetic patients by SQL (fixture setup only). The acting
session signs in as ``OE_ACT_USER``, opens the patient and saves the
demographics form (``demographics_save.php``). The read-back opens the
Patient Finder (``main/finder/dynamic_finder.php``) as ``OE_READ_USER``: one
row per patient with name, home phone, birth date and external ID, so a second
copy of the patient is counted. The history view is the audit log
(``logview/logview.php``, a GET filter form), read before and after the save.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from typing import Any

from benchmark.ui_second_read.common import (
    base,
    check,
    env,
    final_section,
    ledger_state,
    settle,
    token,
    written,
)
from openadapt_flow.runtime.effects.ui_readback import UiReadbackSpec

APP = os.environ.get("UI2R_OPENEMR_URL", "http://lab-host.invalid:9640")


HISTORY = {
    "url": APP + "/interface/logview/logview.php",
    "submit_selector": "a.btn-save",
    "ready_selector": "body",
    "row_selector": "table tbody tr:has(td:nth-child(10))",
    "entry": {
        "record": "td:nth-child(7)",
        "what": "td:nth-child(2)",
        "user": "td:nth-child(4)",
    },
    "record": {"param": "pid"},
    # From a calibration save (not a case): the entry types one correct
    # demographics save writes. Reads are ignored; patient 0 or blank is
    # bookkeeping no record owns.
    "explained": [
        "^patient-record-update$",
        "^http-request-update$",
        "^other-(insert|update)$",
    ],
    "unattributed_records": ["", "0"],
    "ignored": ["-Query$", "-select$", "^view$", "^login$", "^logout$"],
    "acting_user_env": "OE_ACT_USER",
}


def sql(statement: str) -> str:
    # Operator-supplied command for this isolated synthetic fixture. No
    # deployment address, container identity or credentials are built in.
    cmd = shlex.split(env("UI2R_OPENEMR_SQL_COMMAND"))
    done = subprocess.run(
        cmd, input=statement, capture_output=True, text=True, timeout=60
    )
    if done.returncode:
        raise RuntimeError(done.stderr[-300:])
    return done.stdout.strip()


def seed(phone: str) -> tuple[int, str, str]:
    """A synthetic patient that sorts first in the finder."""

    pubpid = f"UI2R{token(6)}"
    lname = f"Aaui{token(4).lower()}"
    pid = int(
        sql(
            "SET @p := (SELECT COALESCE(MAX(pid),0)+1 FROM patient_data); "
            "INSERT INTO patient_data (pid, pubpid, fname, lname, DOB, phone_home, "
            "uuid, date, regdate) VALUES (@p, '%s', 'Test', '%s', '1970-01-01', '%s', "
            "UNHEX(REPLACE(UUID(),'-','')), NOW(), NOW()); SELECT @p;"
            % (pubpid, lname, phone)
        ).splitlines()[-1]
    )
    return pid, pubpid, f"{lname}, Test"


def cleanup() -> None:
    sql("DELETE FROM patient_data WHERE pubpid LIKE 'UI2R%';")


def spec(**extra: Any) -> UiReadbackSpec:
    b = base(APP)
    data = {
        "record_url": b + "/interface/main/finder/dynamic_finder.php",
        "sign_in": {
            "url": b + "/interface/login/login.php?site=default",
            "username_selector": "#authUser",
            "password_selector": "#clearPass",
            "submit_selector": "#login-button",
            "signed_in_selector": "#mainBox",
            "username_env": "OE_READ_USER",
            "password_env": "OE_READ_PASS",
        },
        "ready_selector": "#pt_table:has(tbody tr[id^=pid_])",
        "row_selector": "tbody tr[id^=pid_]",
        "fields": {
            "name": {"selector": "td:nth-child(1)"},
            "phone": {"selector": "td:nth-child(2)"},
            "dob": {"selector": "td:nth-child(4)"},
            "pubpid": {"selector": "td:nth-child(5)"},
        },
        "identity": {"reader": {"selector": "title"}, "value": "Patient Finder"},
        "navigation_timeout_s": 30.0,
        # From calibrate.py for the active network profile (PREREG.md).
        "index_lag_s": float(os.environ.get("UI2R_INDEX_LAG_S", "0")),
        **extra,
    }
    if "history" in data and data["history"]:
        data["history"] = {**data["history"], "url": base(data["history"]["url"])}
    return UiReadbackSpec.model_validate(data)


class App:
    def __init__(self, out: Any) -> None:
        self.out = out

    def __enter__(self) -> "App":
        from playwright.sync_api import sync_playwright

        cleanup()
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=True)
        self.page = self.login()
        return self

    def __exit__(self, *exc: object) -> None:
        self.browser.close()
        self._pw.stop()
        cleanup()

    def login(self) -> Any:
        page = self.browser.new_context().new_page()
        page.set_default_timeout(60_000)
        for _ in range(3):  # a chaos 502 on sign-in is a harness event
            try:
                page.goto(base(APP) + "/interface/login/login.php?site=default")
                page.fill("#authUser", env("OE_ACT_USER"))
                page.fill("#clearPass", env("OE_ACT_PASS"))
                page.click("#login-button")
                page.wait_for_selector("#mainBox", state="attached")
                return page
            except Exception:  # noqa: BLE001
                time.sleep(1)
        raise RuntimeError("acting sign-in failed")

    def set_phone(
        self, pid: int, phone: str, *, save: bool = True, page: Any = None
    ) -> None:
        page = page or self.page
        page.goto(
            base(APP)
            + f"/interface/patient_file/summary/demographics.php?set_pid={pid}"
        )
        page.goto(base(APP) + "/interface/patient_file/summary/demographics_full.php")
        page.wait_for_selector("input[name=form_phone_home]", state="attached")
        page.eval_on_selector(
            "input[name=form_phone_home]", "(e, v) => { e.value = v; }", phone
        )
        if save:
            with page.expect_navigation(wait_until="domcontentloaded"):
                page.click("#submit_btn")
            if "demographics" not in page.url:
                raise RuntimeError("the acting save did not finish")


def _row(read: Any, **extra: Any) -> dict[str, Any]:
    return {
        "verdict": read.verdict,
        "reason": read.reason,
        "read_seconds": read.seconds,
        "independence": read.evidence.get("independence") or {},
        "history": read.evidence.get("history"),
        **extra,
    }


def _one(app: App, *, history: bool, act: Any, expected_phone: Any = None) -> dict:
    old, new = f"555-{token(4)}", f"555-{token(4)}"
    pid, pubpid, _name = seed(old)
    other = seed(f"555-{token(4)}")
    s = spec(history=HISTORY if history else None)
    expected = expected_phone(new) if expected_phone else new
    try:
        r = check(
            s,
            {"pid": str(pid)},
            written({"pubpid": pubpid, "phone": expected}),
            lambda: act(app, pid, new, other),
            app.page,
        )
    finally:
        cleanup()
    return _row(r)


def correct(app: App) -> dict:
    return _one(app, history=True, act=lambda a, pid, new, o: a.set_phone(pid, new))


def wrong_value(app: App) -> dict:
    return _one(
        app, history=True, act=lambda a, pid, new, o: a.set_phone(pid, new + "9")
    )


def duplicate(app: App) -> dict:
    def act(a: App, pid: int, new: str, o: Any) -> None:
        a.set_phone(pid, new)
        # planted by SQL: a second copy of the patient (a double submit)
        sql(
            "INSERT INTO patient_data (pid, pubpid, fname, lname, DOB, phone_home, "
            "uuid, date, regdate) SELECT (SELECT MAX(pid)+1 FROM patient_data), "
            "pubpid, fname, lname, DOB, phone_home, UNHEX(REPLACE(UUID(),'-','')), "
            f"NOW(), NOW() FROM patient_data WHERE pid={pid};"
        )

    return _one(app, history=True, act=act)


def wrong_record(app: App) -> dict:
    return _one(app, history=True, act=lambda a, pid, new, o: a.set_phone(o[0], new))


def missing_save(app: App) -> dict:
    return _one(
        app, history=True, act=lambda a, pid, new, o: a.set_phone(pid, new, save=False)
    )


def _collateral(a: App, pid: int, new: str, o: Any) -> None:
    a.set_phone(pid, new)
    a.set_phone(o[0], f"555-{token(4)}")  # another patient edited too


def collateral_no_history(app: App) -> dict:
    return _one(app, history=False, act=_collateral)


def collateral_history(app: App) -> dict:
    return _one(app, history=True, act=_collateral)


def same_view(app: App) -> dict:
    def act(a: App, pid: int, new: str, o: Any) -> None:
        a.set_phone(pid, new)
        a.page.goto(base(APP) + "/interface/main/finder/dynamic_finder.php")

    return _one(app, history=False, act=act)


def _provisional(app: App, late: bool) -> dict:
    old, new = f"555-{token(4)}", f"555-{token(4)}"
    pid, pubpid, _name = seed(old)
    s = spec(final=final_section())
    try:
        r = check(
            s,
            {"pid": str(pid)},
            written({"pubpid": pubpid, "phone": new}),
            lambda: app.set_phone(pid, new),
            app.page,
        )
        if late:
            other = app.login()  # a second session changes it later
            app.set_phone(pid, f"555-{token(4)}", page=other)
            other.context.close()
        settled, secs = settle(s)
    finally:
        cleanup()
    final = ledger_state(s, r.deferred) if r.deferred else None
    return _row(
        r,
        final=final,
        settle_seconds=secs,
        settled=[(x.state, x.code) for x in settled],
    )


def provisional_then_final(app: App) -> dict:
    return _provisional(app, late=False)


def late_change(app: App) -> dict:
    return _provisional(app, late=True)


CASES = [
    ("C1", "correct", "confirmed", correct),
    ("C2", "correct", "confirmed", correct),
    ("C3", "correct", "confirmed", correct),
    ("C4", "correct", "confirmed", correct),
    ("C5", "correct", "confirmed", correct),
    ("F1", "wrong value", "refuted", wrong_value),
    ("F2", "duplicate (planted by SQL)", "refuted", duplicate),
    ("F3", "wrong record", "refuted", wrong_record),
    ("F4", "missing save", "not confirmed", missing_save),
    (
        "F5c",
        "provisional, unchanged",
        "provisional then verified",
        provisional_then_final,
    ),
    ("F5", "late change", "provisional then reconciliation_required", late_change),
    (
        "F6a",
        "collateral, no history view",
        "confirmed (expected miss)",
        collateral_no_history,
    ),
    ("F6b", "collateral, history view", "refuted", collateral_history),
    ("F7", "same view", "not confirmed", same_view),
]
