"""Non-medical reference workflow: post supplier invoices into a desktop ledger.

PROTOTYPE, SYNTHETIC DATA ONLY. The same steps as the referral template, on
an accounts-payable app, to show nothing in the core is clinical:

    invoice folder -> parsed document -> clerk confirms exceptions
      -> one controller approval -> find supplier (must exist) -> enter bill
      -> close, reopen by bill id, read back -> ledger export read -> Seal

Two exports are read. Ctrl+E in the app is an export from the live session,
so it counts as a read in the acting session. The scheduled export is a
separate process that reads saved data, so values that match it are graded
``system_of_record``. A supplier that is not in the ledger stops the run;
creating suppliers is not part of this workflow. A screen that does not appear
as expected, before or after the save, halts with a result exchange.

    python ap_invoice.py --inbox invoice_inbox --run-dir out/invoices \\
        --l1-dir out/extractions --export-dir exports --ledger-db ledger.sqlite \\
        [--app-python python3] [--answers answers.json]
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

from evidence import EXECUTOR, grade, now, read_field
from pipeline import (
    People,
    Spec,
    decide,
    finish,
    intake,
    record_no_action,
    result_exchange,
    unsettled,
)
from x11 import DriverHalt, X11App, pane_value

WORKFLOW = "example.ap_post_invoice"
SPECS = [
    Spec("supplier_id", "code", ["supplier id"], r"V-\d{4}"),
    Spec("invoice_no", "code", ["invoice no"], r"INV-\d+"),
    Spec("amount", "currency", ["amount"], r"\d+\.\d{2}"),
    Spec("due_date", "date", ["due date"], r"\d{4}-\d{2}-\d{2}"),
]


def value(ex: dict[str, Any], name: str) -> str:
    return next(f["value"] for f in ex["fields"] if f["name"] == name)


#: The check level a posted bill carries: every bill value is read back
#: through the scheduled export of saved data. A hold or reject (no_action)
#: must show nothing was written at least as well.
COMMIT_GRADE = "system_of_record"


def read_export_rows(path: Path, since: float) -> Optional[list[dict[str, str]]]:
    """Every row of an export written at or after ``since``; None if none was."""
    deadline = time.time() + 8
    while time.time() < deadline:
        if path.is_file() and path.stat().st_mtime >= since:
            return list(csv.DictReader(path.open()))
        time.sleep(0.2)
    return None


def read_export(path: Path, bill_id: str, since: float) -> dict[str, str]:
    deadline = time.time() + 8
    while time.time() < deadline:
        if path.is_file() and path.stat().st_mtime >= since:
            for row in csv.DictReader(path.open()):
                if row["bill_id"] == bill_id:
                    return row
        time.sleep(0.2)
    return {}


def scheduled_export(app_python: str, ledger_db: Path, export_dir: Path) -> None:
    """Run the ledger's scheduled export: a separate process, no UI."""
    app = Path(__file__).parent / "apps" / "mock_ledger.py"
    subprocess.run(
        [
            app_python,
            str(app),
            "--db",
            str(ledger_db),
            "--export-only",
            "--export-dir",
            str(export_dir),
        ],
        check=True,
        timeout=30,
    )


def post_one(
    app: X11App,
    inv: Path,
    people: People,
    run_dir: Path,
    l1_dir: Path,
    export_dir: Path,
    ledger_db: Path,
    app_python: str,
    log,
) -> str:
    log(f"\n== {inv.name}")
    today = date.today().isoformat()
    req = intake(
        inv,
        SPECS,
        run_dir,
        workflow=WORKFLOW,
        artifact={
            "source_type": "pdf_scan",
            "file_number": "pending",
            "date": today,
            "doctype": "invoice",
        },
        people=people,
        staff_role="ap_clerk",
        log=log,
    )
    sup, inv_no, amount, due = (
        value(req, n) for n in ("supplier_id", "invoice_no", "amount", "due_date")
    )
    file_number = sup or inv.stem
    req["artifacts"][0]["file_number"] = file_number
    req["identities"] = [
        {
            "identity_id": "supplier",
            "role": "counterparty",
            "fields": ["supplier_id"],
            "match": {"status": "not_checked"},
        }
    ]
    res = result_exchange(
        req,
        artifact={
            "artifact_id": "bill",
            "source_type": "rdp_screen",
            "file_number": file_number,
            "date": today,
            "doctype": "bill-posting",
        },
    )
    # The supplier id from the request; the supplier screen's read is added
    # once the ledger shows it.
    supplier = {"name": "supplier_id", "type": "code", "value": sup, "confidence": 1.0}
    res["fields"].append(supplier)
    ident = {
        "identity_id": "supplier",
        "role": "counterparty",
        "fields": ["supplier_id"],
        "match": {"status": "not_checked"},
    }
    res["identities"] = [ident]
    state = {"saved": False}

    def halt(reason: str) -> str:
        res["outcome"], res["halt_reason"] = "halted", reason
        log(f"  HALT: {reason}")
        finish(req, res, run_dir, l1_dir, log)
        return "halted"

    open_items = unsettled(req)
    if open_items:
        return halt(
            f"no person settled the exception on {', '.join(open_items)}; "
            "nothing was searched or written"
        )
    choice = decide(
        req,
        people,
        question="Approve this invoice for payment?",
        options=["approve", "hold", "reject"],
        role="controller",
        output_field="approval",
        log=log,
    )
    try:
        return _post(
            app,
            inv,
            res,
            supplier,
            ident,
            state,
            choice,
            sup,
            inv_no,
            amount,
            due,
            run_dir,
            l1_dir,
            export_dir,
            ledger_db,
            app_python,
            req,
            people,
            halt,
            log,
        )
    except DriverHalt as err:
        when = (
            "after the save keystroke, so the bill may or may not be saved"
            if state["saved"]
            else "before the save keystroke; nothing was saved"
        )
        app.key("Escape")
        return halt(f"screen check failed {when}: {err}")


def _no_bill(
    res: dict[str, Any],
    req: dict[str, Any],
    choice: str,
    sup: str,
    pane: str,
    run_dir: Path,
    l1_dir: Path,
    export_dir: Path,
    ledger_db: Path,
    app_python: str,
    people: People,
    halt,
    log,
) -> str:
    """A hold or reject enters no bill. Read back that none was entered: the
    supplier's bill count on screen, and in the scheduled export of saved
    data."""
    on_screen = pane_value(pane, "Open bills")
    started = time.time() - 1
    scheduled_export(app_python, ledger_db, export_dir)
    rows = read_export_rows(export_dir / "scheduled" / "bills.csv", started)
    exported = None if rows is None else str(sum(r["supplier_id"] == sup for r in rows))
    reads = [
        {
            "channel": "clipboard",
            "value": on_screen,
            "artifact_id": "bill",
            "session": "acting",
            "observed_at": now(),
        },
        {
            "channel": "export",
            "value": exported,
            "source_kind": "committed_store",
            "session": "independent",
            "observed_at": now(),
        },
    ]
    log(
        f"  {choice}: no bill entered; {sup} bills on screen {on_screen}, export {exported}"
    )
    if on_screen is None or not on_screen.isdigit():
        return halt(f"supplier {sup} screen does not show its bill count")
    if exported != on_screen:
        return halt(
            f"supplier {sup} shows {on_screen} bills, the scheduled export has "
            f"{exported}; cannot show that nothing was written"
        )
    res["fields"].append(
        {
            "name": "supplier_bills",
            "type": "integer",
            "value": int(on_screen),
            "confidence": 1.0,
            "grade": grade(reads, on_screen),
            "reads": reads,
        }
    )
    weak = record_no_action(
        req,
        res,
        reason_code="held" if choice == "hold" else "rejected",
        required_grade=COMMIT_GRADE,
        evidence=["supplier_bills"],
    )
    if weak:
        return halt(weak)
    finish(req, res, run_dir, l1_dir, log, review=people.review_decisions)
    return choice


def _post(
    app: X11App,
    inv: Path,
    res: dict[str, Any],
    supplier: dict[str, Any],
    ident: dict[str, Any],
    state: dict[str, bool],
    choice: str,
    sup: str,
    inv_no: str,
    amount: str,
    due: str,
    run_dir: Path,
    l1_dir: Path,
    export_dir: Path,
    ledger_db: Path,
    app_python: str,
    req: dict[str, Any],
    people: People,
    halt,
    log,
) -> str:
    app.key("Escape")
    app.wait_screen("Find supplier")
    app.type(sup)
    app.key("Return")
    screen = app.wait_screen(r"Supplier (V-\d+)|Supplier not found").group(0)
    if screen == "Supplier not found":
        ident["match"] = {"status": "new", "candidates": 0, "checked_by": EXECUTOR}
        return halt(f"supplier {sup} is not in the ledger; nothing was written")
    pane = app.copy_pane()
    shown = pane_value(pane, "Supplier")
    supplier["reads"] = [
        {
            "channel": "clipboard",
            "value": shown,
            "artifact_id": "bill",
            "session": "acting",
        }
    ]
    if shown != sup:
        ident["match"] = {
            "status": "ambiguous",
            "candidates": 1,
            "checked_by": EXECUTOR,
        }
        return halt(f"supplier screen shows {shown!r}, the invoice has {sup!r}")
    ident["match"] = {
        "status": "existing",
        "record_ref": sup,
        "candidates": 1,
        "checked_by": EXECUTOR,
    }
    if choice != "approve":
        return _no_bill(
            res,
            req,
            choice,
            sup,
            pane,
            run_dir,
            l1_dir,
            export_dir,
            ledger_db,
            app_python,
            people,
            halt,
            log,
        )

    app.key("ctrl+b")
    app.wait_screen(f"New bill {sup}")
    app.fill([inv_no, amount, due])
    state["saved"] = True
    app.key("ctrl+s")
    bill = app.wait_screen(r"Saved (B-\d+)").group(1)
    log(f"  entered bill {bill}: {inv_no}, {amount}, {due}")

    # Committed-record read-back, then the ledger's own export.
    app.key("Escape")
    app.wait_screen("Find supplier")
    app.key("ctrl+o")
    app.wait_screen("Open bill")
    app.type(bill)
    app.key("Return")
    try:
        app.wait_screen(f"Bill {bill}")
    except DriverHalt:
        return halt(f"bill {bill} was reported saved but cannot be reopened")
    text = app.copy_pane()
    frame = app.frame()
    (run_dir / "frames").mkdir(exist_ok=True)
    (run_dir / "frames" / f"{inv.stem}-readback.png").write_bytes(frame.png)
    started = time.time() - 1
    app.key("ctrl+e")
    live = read_export(export_dir / "bills.csv", bill, started)
    scheduled_export(app_python, ledger_db, export_dir)
    exported = read_export(export_dir / "scheduled" / "bills.csv", bill, started)
    log(
        f"  export row for {bill}: live session {'found' if live else 'missing'}, "
        f"scheduled {'found' if exported else 'missing'}"
    )
    expected = {
        "bill_id": ("Bill", "code", bill, "bill_id"),
        "invoice_no": ("Invoice no", "code", inv_no, "invoice_no"),
        "amount": ("Amount", "currency", amount, "amount"),
        "due_date": ("Due date", "date", due, "due_date"),
    }
    readback = []
    for fname, (label, kind, want, col) in expected.items():
        f = read_field(
            fname,
            kind,
            expected=want,
            clipboard=pane_value(text, label),
            ocr_line=frame.value_after(label),
            frame=frame,
            session="reopened",
            artifact_id="bill",
            screen=f"Bill {bill}",
            exports=[
                (live.get(col), "live_session"),
                (exported.get(col), "committed_store"),
            ],
        )
        res["fields"].append(f)
        readback.append(f)
    res["artifacts"][0]["file_number"] = bill
    for f in readback:
        log(
            f"  read back {f['name']}: {f['value']!r} [{f['grade']}]"
            + (f"  REVIEW: {f['review_reason']}" if f.get("needs_review") else "")
        )
    lost = [
        f["name"]
        for f in readback
        if f.get("needs_review") and "wrote" in f.get("review_reason", "")
    ]
    if lost or not exported:
        return halt(
            f"read-back of {bill} does not show {', '.join(lost) or 'an export row'}"
        )
    res["outcome"] = "committed"
    finish(req, res, run_dir, l1_dir, log)
    return "committed"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--inbox", type=Path, required=True)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--l1-dir", type=Path, required=True)
    p.add_argument("--export-dir", type=Path, required=True)
    p.add_argument("--ledger-db", type=Path, required=True)
    p.add_argument("--app-python", default="python3")
    p.add_argument("--answers", type=Path)
    p.add_argument(
        "--review-decisions",
        action="store_true",
        help="site policy: send held and rejected invoices (no_action) to review",
    )
    a = p.parse_args()
    a.run_dir.mkdir(parents=True, exist_ok=True)
    people = People.load(a.answers, "ap_clerk", "controller")
    people.review_decisions = people.review_decisions or a.review_decisions
    app = X11App("Mock Ledger")
    log_path = a.run_dir / "run.log"

    def log(msg: str) -> None:
        print(msg, flush=True)
        with log_path.open("a") as fh:
            fh.write(msg + "\n")

    log("Invoice posting (PROTOTYPE, synthetic data). Model calls: 0.")
    outcomes = {}
    for inv in sorted(a.inbox.glob("*.pdf")):
        try:
            outcomes[inv.name] = post_one(
                app,
                inv,
                people,
                a.run_dir,
                a.l1_dir,
                a.export_dir,
                a.ledger_db,
                a.app_python,
                log,
            )
        except DriverHalt as err:
            outcomes[inv.name] = "halted"
            log(f"  HALT (screen): {err}")
            app.key("Escape")
    log("\nSummary: " + ", ".join(f"{k}: {v}" for k, v in outcomes.items()))


if __name__ == "__main__":
    main()
