"""Non-medical reference workflow: post supplier invoices into a desktop ledger.

PROTOTYPE, SYNTHETIC DATA ONLY. The same steps as the referral template, on
an accounts-payable app, to show nothing in the core is clinical:

    invoice folder -> parsed document -> clerk confirms exceptions
      -> one controller approval -> find supplier (must exist) -> enter bill
      -> close, reopen by bill id, read back -> ledger export read -> Seal

Two exports are read. Ctrl+E in the app is an export from the live session,
so it counts like a screen read. The scheduled export is a separate process
that reads saved data, so values that match it are graded ``verified``. A supplier that is not in the ledger stops the run;
creating suppliers is not part of this workflow.

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
from typing import Any

from evidence import EXECUTOR, read_field
from pipeline import People, Spec, decide, finish, intake, result_exchange
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
    req["artifacts"][0]["file_number"] = sup
    req["identities"] = [
        {
            "identity_id": "supplier",
            "role": "counterparty",
            "fields": ["supplier_id"],
            "match": {"status": "not_checked"},
        }
    ]
    choice = decide(
        req,
        people,
        question="Approve this invoice for payment?",
        options=["approve", "hold", "reject"],
        role="controller",
        output_field="approval",
        log=log,
    )
    res = result_exchange(
        req,
        artifact={
            "artifact_id": "bill",
            "source_type": "rdp_screen",
            "file_number": sup,
            "date": today,
            "doctype": "bill-posting",
        },
    )
    res["fields"].append(
        {"name": "supplier_matches", "type": "integer", "value": 0, "confidence": 1.0}
    )
    ident = {
        "identity_id": "supplier",
        "role": "counterparty",
        "fields": ["supplier_matches"],
        "match": {"status": "not_checked"},
    }
    res["identities"] = [ident]

    def halt(reason: str) -> str:
        res["outcome"], res["halt_reason"] = "halted", reason
        log(f"  HALT: {reason}")
        finish(req, res, run_dir, l1_dir, log)
        return "halted"

    app.key("Escape")
    app.wait_screen("Find supplier")
    app.type(sup)
    app.key("Return")
    screen = app.wait_screen(r"Supplier (V-\d+)|Supplier not found").group(0)
    if screen == "Supplier not found":
        ident["match"] = {"status": "new", "candidates": 0, "checked_by": EXECUTOR}
        return halt(f"supplier {sup} is not in the ledger; nothing was written")
    shown = pane_value(app.copy_pane(), "Supplier")
    res["fields"][0]["value"] = 1
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
        res["outcome"] = "committed"
        log(f"  {choice}: no bill entered")
        finish(req, res, run_dir, l1_dir, log)
        return choice

    app.key("ctrl+b")
    app.wait_screen(f"New bill {sup}")
    app.fill([inv_no, amount, due])
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
    for fname, (label, kind, want, col) in expected.items():
        res["fields"].append(
            read_field(
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
        )
    res["artifacts"][0]["file_number"] = bill
    for f in res["fields"][1:]:
        log(
            f"  read back {f['name']}: {f['value']!r} [{f['grade']}]"
            + (f"  REVIEW: {f['review_reason']}" if f.get("needs_review") else "")
        )
    lost = [
        f["name"]
        for f in res["fields"][1:]
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
    a = p.parse_args()
    a.run_dir.mkdir(parents=True, exist_ok=True)
    people = People.load(a.answers, "ap_clerk", "controller")
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
