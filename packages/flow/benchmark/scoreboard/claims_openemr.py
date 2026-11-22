"""The agent's claims versus the read-back, on OpenEMR, with bad saves injected.

    python -m scoreboard.claims_openemr --run-id <id> --per-fault 5

The OpenEMR counterpart of ``claims.py``. OpenEMR itself is unchanged, and so
is everything in OpenAdapt: the faults are injected only in the fixture's
database (``benchmark/openemr_local``, the podman copy on 9911/9910), by a
trigger installed before the agent starts or by a poller that acts once the
new chart is written. The fixture's verified baseline is restored before
every item and after the last one.

=====================  ======================================================
``ok``                 control: a clean save
``partial``            a trigger blanks the new chart's home phone
``wrong_value``        a trigger changes the new chart's postal code
``duplicate``          the poller copies the new chart into a second chart
``no_persist``         the poller deletes the new chart 3 s after it is written
``collateral_loss``    the poller deletes another, pre-existing chart
``collateral_edit``    the poller changes another chart's home phone
=====================  ======================================================

Unlike MockMed, OpenEMR shows the saved chart read back from its own database,
so the agent can notice a partial or wrong value on the screen. A poller fault
lands after the save and is not on the screen the agent saw.

``verify-run`` reads the patient list through OpenEMR's REST API with the
fixture's read-only oracle client (``record:`` contract from
``benchmark/openemr_local/verify.yaml``). Its collateral check covers charts
that vanished and, since #1375, charts that changed.

``--heldout`` runs a second, fresh set of faults written after the first
set's results were known (Amendment 15). Three existing charts are seeded,
not one, and each fault varies what a check could overfit: the field, the
victim, and trigger versus poller.

=======================  ====================================================
``h_ok``                 control: a clean save, three existing charts
``h_partial``            a trigger blanks the new chart's street
``h_wrong_value``        a trigger moves the new chart's date of birth a day
``h_duplicate``          the poller adds a second chart with the new chart's
                         name and another email
``h_no_persist``         the poller overwrites the new chart's last name and
                         email 3 s after it is written
``h_collateral_loss``    the poller deletes the newest existing chart
``h_collateral_edit``    the poller changes one field of the middle existing
                         chart (street, date of birth, first name, city or
                         email, by item)
=======================  ====================================================

The ground truth compares every column of every existing ``patient_data``
row, not only the referral fields.
"""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from scoreboard import schema  # noqa: E402
from scoreboard.run import (  # noqa: E402
    AGENT_MODEL,
    ITEM_TIMEOUT_S,
    SPEND_CAP_USD,
    _agent_item,
    _ledger_view,
    add_spend,
    reported_success,
    spent,
)
from scoreboard.tasks import openemr  # noqa: E402

FAULTS = (
    "ok",
    "partial",
    "wrong_value",
    "duplicate",
    "no_persist",
    "collateral_loss",
    "collateral_edit",
)
HELDOUT_FAULTS = (
    "h_ok",
    "h_partial",
    "h_wrong_value",
    "h_duplicate",
    "h_no_persist",
    "h_collateral_loss",
    "h_collateral_edit",
)
TRIGGERS = {
    # column, SQL expression: one SET statement, so the mysql client needs no
    # DELIMITER.
    "partial": ("phone_home", "''"),
    "wrong_value": ("postal_code", "'02140'"),
    "h_partial": ("street", "''"),
    "h_wrong_value": ("DOB", "DATE_ADD(NEW.DOB, INTERVAL 1 DAY)"),
}
#: h_collateral_edit: the field changed on the middle existing chart, by item.
HELDOUT_EDITS = (
    ("street", "'7 ALTERED LANE'"),
    ("DOB", "'1962-06-15'"),
    ("fname", "'Wendall'"),
    ("city", "'OTHERTON'"),
    ("email", "'w.priorchart2@example.invalid'"),
)
FIELDS = (
    "item",
    "fault",
    "claimed_success",
    "ground_truth",
    "verify_effect_verdict",
    "verify_outcome",
    "verify_exit",
    "fault_applied",
    "model_calls",
    "cost_usd",
    "host_load_1m",
    "final_text",
    "verify_stderr",
)
TOKEN_ENV = "SCOREBOARD_OPENEMR_ORACLE_TOKEN"  # passed to verify-run, never stored
VERIFY_PYTHON = sys.executable  # --verify-python

VERIFY_YAML = """\
schema_version: openadapt.verify/v1
verifier:
  kind: rest
  base_url: {api}
  records_path: /apis/default/api/patient?_limit=200
  records_key: data
  records_next_path: links.next
  records_next_absent_is_last: true
  auth:
    bearer_env: {token_env}
record:
  name: patient
  match: [email, lname]
  duplicate_key: email
  fields:
    email: email
    lname: lname
    title: title
    fname: fname
    DOB: DOB
    sex: sex
    street: street
    city: city
    state: {{literal: MA}}
    postal_code: postal_code
    phone_home: phone_home
    country_code: country_code
"""


#: One existing chart, seeded after every reset through OpenEMR's own patient
#: API (the fixture's baseline holds none), so collateral faults have a
#: chart that is not the target. Made up.
EXISTING = {
    "title": "Mr.",
    "fname": "Wendell",
    "lname": "Priorchart",
    "DOB": "1962-06-14",
    "sex": "Male",
    "street": "9 EXISTING ROAD",
    "city": "EXAMPLETON",
    "state": "MA",
    "postal_code": "02139",
    "phone_home": "555-0188",
    "email": "w.priorchart@example.invalid",
    "country_code": "USA",
}


#: Two more existing charts for the held-out set. Made up.
EXISTING_MORE = (
    {
        **EXISTING,
        "title": "Ms.",
        "fname": "Odette",
        "lname": "Earliercase",
        "DOB": "1971-02-03",
        "sex": "Female",
        "street": "21 EXISTING ROAD",
        "phone_home": "555-0177",
        "email": "o.earliercase@example.invalid",
    },
    {
        **EXISTING,
        "fname": "Lucius",
        "lname": "Olderfile",
        "DOB": "1949-11-30",
        "street": "33 EXISTING ROAD",
        "phone_home": "555-0166",
        "email": "l.olderfile@example.invalid",
    },
)


def reset_and_seed(heldout: bool = False) -> None:
    fx = openemr.fixture()
    fx.reset()
    left = (
        fx._db(
            "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA = 'openemr' AND TRIGGER_NAME LIKE 'sb\\_%'"
        )
        .decode()
        .split()
    )
    if left:
        raise SystemExit(f"fault triggers survived the reset: {left}")
    charts = (EXISTING, *EXISTING_MORE) if heldout else (EXISTING,)
    session = fx.token_session("actor")
    try:
        for chart in charts:
            status = session.post(
                f"{fx.api_base_url}/apis/default/api/patient", json=chart
            ).status_code
            if status not in (200, 201):
                raise SystemExit(f"seeding an existing chart returned HTTP {status}")
    finally:
        session.close()
    if len(_rows()) != len(charts):
        raise SystemExit("the seeded charts are not all in patient_data")


def _rows() -> dict[str, dict[str, str]]:
    return {p["id"]: p for p in openemr.patients()}


def _full_rows(ids: set[str]) -> dict[str, str]:
    """Every column of the given ``patient_data`` rows, one text line each.

    Binary and text-blob columns are read as hex, so no byte in them can
    split a line or a field.
    """

    if not ids:
        return {}
    if not all(i.isdigit() for i in ids):
        raise ValueError("unexpected patient_data id")
    db = openemr.fixture()._db
    cols = []
    for line in (
        db(
            "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = 'openemr' AND TABLE_NAME = 'patient_data' "
            "ORDER BY ORDINAL_POSITION"
        )
        .decode()
        .splitlines()
    ):
        name, kind = line.split("\t")
        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise ValueError("unexpected patient_data column name")
        cols.append(
            f"HEX(`{name}`)"
            if "binary" in kind or "blob" in kind or "text" in kind
            else f"`{name}`"
        )
    out = db(
        "SELECT id, "
        + ", ".join(cols)
        + " FROM openemr.patient_data WHERE id IN ("
        + ",".join(sorted(ids))
        + ") ORDER BY id"
    )
    return {line.split("\t", 1)[0]: line for line in out.decode().splitlines() if line}


def ground_truth(
    before: dict[str, dict[str, str]],
    values: dict[str, str],
    before_full: dict[str, str] | None = None,
) -> str:
    """``correct`` | ``missing`` | ``duplicate`` | ``wrong`` | ``collateral_*``.

    With ``before_full`` (every column of the existing rows), a change to any
    column of an existing chart is a collateral edit, not only a change to a
    referral field.
    """

    after = _rows()
    if any(pid not in after for pid in before):
        return "collateral_loss"
    if any(after[pid] != row for pid, row in before.items()):
        return "collateral_edit"
    if before_full is not None and _full_rows(set(before_full)) != before_full:
        return "collateral_edit"
    return openemr.check(values, set(before))


def _email_literal(email: str) -> str:
    """A quoted SQL literal for one of this module's made-up emails."""

    if not re.fullmatch(r"[A-Za-z0-9._@-]+", email):
        raise ValueError("unexpected characters in a synthetic email")
    return f"'{email}'"


class Poller(threading.Thread):
    """Applies one poller fault once the new chart is written."""

    def __init__(
        self,
        fault: str,
        before: dict[str, dict[str, str]],
        email: str,
        item: int = 0,
    ):
        super().__init__(daemon=True)
        self.fault = fault
        self.max_id = max(int(i) for i in before)
        self.victim = min(int(i) for i in before)
        ids = sorted(int(i) for i in before)
        self.middle = ids[len(ids) // 2]
        self.item = item
        self.email = email
        self.applied = ""
        self.stop = threading.Event()

    def run(self) -> None:
        db = openemr.fixture()._db
        while not self.stop.is_set():
            new = (
                db(
                    "SELECT id, pid FROM openemr.patient_data WHERE id > "
                    f"{self.max_id} AND email = {_email_literal(self.email)}"
                )
                .decode()
                .split()
            )
            if new:
                break
            time.sleep(0.3)
        else:
            return
        row_id, pid = new[0], new[1]
        if self.fault == "duplicate":
            db(
                "CREATE TEMPORARY TABLE openemr.sb_dup AS SELECT * FROM "
                f"openemr.patient_data WHERE id = {row_id}; "
                "UPDATE openemr.sb_dup SET id = (SELECT MAX(id) + 1 FROM "
                "openemr.patient_data), pid = (SELECT MAX(pid) + 1 FROM "
                "openemr.patient_data), uuid = UNHEX(REPLACE(UUID(), '-', '')); "
                "INSERT INTO openemr.patient_data SELECT * FROM openemr.sb_dup;"
            )
        elif self.fault == "no_persist":
            time.sleep(3.0)
            db(f"DELETE FROM openemr.patient_data WHERE pid = {pid}")
        elif self.fault == "collateral_loss":
            db(f"DELETE FROM openemr.patient_data WHERE id = {self.victim}")
        elif self.fault == "collateral_edit":
            db(
                "UPDATE openemr.patient_data SET phone_home = '555-0199' "
                f"WHERE id = {self.victim}"
            )
        elif self.fault == "h_duplicate":
            db(
                "CREATE TEMPORARY TABLE openemr.sb_dup AS SELECT * FROM "
                f"openemr.patient_data WHERE id = {row_id}; "
                "UPDATE openemr.sb_dup SET id = (SELECT MAX(id) + 1 FROM "
                "openemr.patient_data), pid = (SELECT MAX(pid) + 1 FROM "
                "openemr.patient_data), uuid = UNHEX(REPLACE(UUID(), '-', '')), "
                "email = CONCAT('copy.', email); "
                "INSERT INTO openemr.patient_data SELECT * FROM openemr.sb_dup;"
            )
        elif self.fault == "h_no_persist":
            time.sleep(3.0)
            db(
                "UPDATE openemr.patient_data SET lname = CONCAT(lname, 'X'), "
                f"email = CONCAT('moved.', email) WHERE id = {row_id}"
            )
        elif self.fault == "h_collateral_loss":
            db(f"DELETE FROM openemr.patient_data WHERE id = {self.max_id}")
        elif self.fault == "h_collateral_edit":
            column, value = HELDOUT_EDITS[self.item % len(HELDOUT_EDITS)]
            db(
                f"UPDATE openemr.patient_data SET {column} = {value} "
                f"WHERE id = {self.middle}"
            )
        self.applied = f"{self.fault} at {time.strftime('%H:%M:%S')}"


def _install_trigger(fault: str, max_id: int) -> None:
    column, value = TRIGGERS[fault]
    new_row = f"(NEW.id > {max_id} OR NEW.id = 0 OR NEW.id IS NULL)"
    sql = []
    for when in ("INSERT", "UPDATE"):
        name = f"sb_fault_{when.lower()}"
        sql.append(f"DROP TRIGGER IF EXISTS openemr.{name};")
        sql.append(
            f"CREATE TRIGGER openemr.{name} BEFORE {when} ON openemr.patient_data "
            f"FOR EACH ROW SET NEW.{column} = IF({new_row}, {value}, NEW.{column});"
        )
    openemr.fixture()._db(" ".join(sql))


def _verify(
    env: dict[str, str], params: dict[str, str], *argv: str
) -> subprocess.CompletedProcess[str]:
    """Run verify-run with the item's values on stdin, never in a file.

    The values are a made-up patient, but they are patient-shaped: nothing
    writes them to disk in clear text outside the committed scoreboard rows.
    """

    return subprocess.run(
        [
            VERIFY_PYTHON,
            "-m",
            "openadapt_flow",
            "verify-run",
            "--params",
            "/dev/stdin",
            *argv,
        ],
        input=json.dumps(params),
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )


def _token_env() -> dict[str, str]:
    session = openemr.fixture().token_session("oracle")
    token = session.headers["Authorization"].removeprefix("Bearer ")
    session.close()
    return {**os.environ, TOKEN_ENV: token}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--per-fault", type=int, default=5)
    parser.add_argument("--max-cost", type=float, default=0.50)
    parser.add_argument("--faults", default=",".join(FAULTS))
    parser.add_argument("--first-item", type=int, default=901)
    parser.add_argument("--model", default=AGENT_MODEL)
    parser.add_argument(
        "--heldout",
        action="store_true",
        help="the held-out fault set (h_*), with three existing charts",
    )
    parser.add_argument(
        "--verify-python",
        default=sys.executable,
        help="the interpreter whose installed openadapt_flow runs verify-run "
        "(a released version, installed on its own)",
    )
    args = parser.parse_args()
    if args.heldout and args.faults == ",".join(FAULTS):
        args.faults = ",".join(HELDOUT_FAULTS)
    global VERIFY_PYTHON
    VERIFY_PYTHON = args.verify_python
    verify_version = subprocess.run(
        [
            VERIFY_PYTHON,
            "-c",
            "import importlib.metadata as m, openadapt_flow as f; "
            "print(m.version('openadapt-flow'), f.__file__)",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    out = schema.RAW / "openemr_referral" / "claims" / args.run_id
    work = schema.CACHE / "openemr_referral" / "claims" / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    rows_path = out / "items.csv"
    done = (
        {int(r["item"]) for r in csv.DictReader(rows_path.open())}
        if rows_path.is_file()
        else set()
    )
    fx = openemr.fixture()
    verify_yaml = work / "verify.yaml"
    verify_yaml.write_text(  # The same REST API on the UI port, over loopback HTTP: the REST verifier
        # has no TLS switch for the fixture's self-signed certificate.
        VERIFY_YAML.format(api=fx.ui_base_url, token_env=TOKEN_ENV)
    )
    schema.write_meta(
        out,
        task="openemr_referral",
        arm="claims",
        model=args.model,
        verifier="openadapt-flow verify-run, rest on OpenEMR's patient API "
        "(read-only oracle client), with a baseline",
        verify_run_engine=verify_version,
        heldout=args.heldout,
        existing_charts=3 if args.heldout else 1,
        verify_yaml=verify_yaml.read_text(),
        faults=args.faults.split(","),
        per_fault=args.per_fault,
        fault_injection="fixture database only: triggers and a poller (claims_openemr.py)",
    )
    plan = list(
        enumerate(
            (f for f in args.faults.split(",") for _ in range(args.per_fault)),
            start=args.first_item,
        )
    )
    try:
        for i, fault in plan:
            if i in done:
                continue
            if spent() + args.max_cost > SPEND_CAP_USD:
                raise SystemExit("spend cap reached")
            reset_and_seed(args.heldout)
            values = openemr.item_values(i)
            before = _rows()
            before_full = _full_rows(set(before))
            item_dir = work / f"item-{i:03d}"
            # A fresh directory per attempt: verify-run marks a baseline used
            # (baseline.json.used), and an old receipt must never be re-read.
            shutil.rmtree(item_dir, ignore_errors=True)
            item_dir.mkdir(parents=True)
            baseline = item_dir / "baseline.json"
            baseline.unlink(missing_ok=True)
            env = _token_env()
            cap = _verify(
                env,
                values,
                "--verify",
                str(verify_yaml),
                "--capture-baseline",
                str(baseline),
            )
            if cap.returncode != 0:
                raise SystemExit(f"baseline failed: {cap.stderr[-500:]}")
            poller = None
            if fault in TRIGGERS:
                _install_trigger(fault, max(int(k) for k in before))
            elif fault != "ok":
                poller = Poller(fault, before, values["email"], item=i)
                poller.start()
            ledger = out / "ledgers" / f"item-{i:03d}.json"
            load0 = schema.load_1m()
            ctx = multiprocessing.get_context("spawn")
            box = ctx.Queue()
            proc = ctx.Process(
                target=_agent_item,
                args=(
                    openemr.NAME,
                    openemr.entry_url(),
                    openemr.instruction(values),
                    str(ledger),
                    args.max_cost,
                    box,
                    args.model,
                ),
            )
            proc.start()
            proc.join(ITEM_TIMEOUT_S)
            result: dict[str, Any] = {}
            if proc.is_alive():
                proc.terminate()
                proc.join(10)
                result = {"error": "ItemTimeout"}
            elif not box.empty():
                result = box.get()
            if poller is not None:
                # A no_persist delete lands 3 s after the chart is written.
                poller.join(timeout=8.0)
                poller.stop.set()
            view = _ledger_view(ledger, result)
            add_spend(view.cost_usd, f"openemr claims {args.run_id} item {i}")
            claimed = reported_success(view, result.get("error"))  # type: ignore[arg-type]
            truth = ground_truth(before, values, before_full)
            applied = (
                poller.applied
                if poller is not None
                else (fault if fault in TRIGGERS else "")
            )
            record = item_dir / "agent-run.json"
            record.write_text(
                json.dumps(
                    {
                        "executor_kind": "agent",
                        "executor_id": args.model,
                        "executor_build": {
                            "agent": "scoreboard.agent.ClaudeComputerUseAgent"
                        },
                        "claimed_success": claimed,
                    }
                )
            )
            share = item_dir / "share"
            # A receipt left by an earlier run of this item must never be read
            # as this run's (verify-run refuses an existing --out).
            shutil.rmtree(share, ignore_errors=True)
            ver = _verify(
                _token_env(),
                values,
                "--verify",
                str(verify_yaml),
                "--run-record",
                str(record),
                "--authorization-ref",
                f"SCOREBOARD-CLAIMS-{args.run_id}-{i:03d}",
                "--baseline",
                str(baseline),
                "--out",
                str(share),
            )
            receipt_file = share / "effect-receipt.json"
            receipt = (
                json.loads(receipt_file.read_text())["receipt"]
                if receipt_file.is_file() and ver.returncode in (0, 1)
                else {}
            )
            row = {
                "item": i,
                "fault": fault,
                "claimed_success": claimed,
                "ground_truth": truth,
                "verify_effect_verdict": receipt.get("effect_verdict", "none"),
                "verify_outcome": receipt.get("outcome", "refused"),
                "verify_exit": ver.returncode,
                "fault_applied": applied,
                "model_calls": len(view.calls),
                "cost_usd": round(view.cost_usd, 6),
                "host_load_1m": load0,
                "final_text": (result.get("error") or view.final_text)[:600],
                "verify_stderr": ver.stderr.strip()[-300:] if not receipt else "",
            }
            new = not rows_path.is_file()
            with rows_path.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)
            print(
                f"claims {i:3d} {fault:16s} claimed={claimed!s:5s} truth={truth:16s} "
                f"verify={row['verify_outcome']} applied={bool(applied)} "
                f"${view.cost_usd:.3f}",
                flush=True,
            )
    finally:
        fx.reset()
    # Every planned item has a row: the build shows this run from now on.
    meta = json.loads((out / "meta.json").read_text())
    meta["complete"] = True
    (out / "meta.json").write_text(json.dumps(meta, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
