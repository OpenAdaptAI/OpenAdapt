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
that vanished; a changed chart that is not the target is outside its
contract, and ``collateral_edit`` measures that.
"""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing
import os
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
TRIGGERS = {
    "partial": "SET NEW.phone_home = ''",
    "wrong_value": "SET NEW.postal_code = '02140'",
}
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

VERIFY_YAML = """\
schema_version: openadapt.verify/v1
verifier:
  kind: rest
  base_url: {api}
  records_path: /apis/default/api/patient?_limit=1000
  records_key: data
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


def _rows() -> dict[str, dict[str, str]]:
    return {p["id"]: p for p in openemr.patients()}


def ground_truth(before: dict[str, dict[str, str]], values: dict[str, str]) -> str:
    """``correct`` | ``missing`` | ``duplicate`` | ``wrong`` | ``collateral_*``."""

    after = _rows()
    if any(pid not in after for pid in before):
        return "collateral_loss"
    if any(after[pid] != row for pid, row in before.items()):
        return "collateral_edit"
    return openemr.check(values, set(before))


class Poller(threading.Thread):
    """Applies one poller fault once the new chart is written."""

    def __init__(self, fault: str, before: dict[str, dict[str, str]], email: str):
        super().__init__(daemon=True)
        self.fault = fault
        self.max_id = max(int(i) for i in before)
        self.victim = min(int(i) for i in before)
        self.email = email
        self.applied = ""
        self.stop = threading.Event()

    def run(self) -> None:
        db = openemr.fixture()._db
        while not self.stop.is_set():
            new = (
                db(
                    "SELECT id, pid FROM openemr.patient_data WHERE id > "
                    f"{self.max_id} AND email = {openemr.fixture()._sql_quote(self.email)}"
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
        self.applied = f"{self.fault} at {time.strftime('%H:%M:%S')}"


def _install_trigger(fault: str, max_id: int) -> None:
    body = TRIGGERS[fault]
    sql = []
    for when in ("INSERT", "UPDATE"):
        name = f"sb_fault_{when.lower()}"
        sql.append(f"DROP TRIGGER IF EXISTS openemr.{name};")
        sql.append(
            f"CREATE TRIGGER openemr.{name} BEFORE {when} ON openemr.patient_data "
            f"FOR EACH ROW BEGIN IF NEW.id > {max_id} OR NEW.id IS NULL OR "
            f"NEW.id = 0 THEN {body}; END IF; END;"
        )
    openemr.fixture()._db(" ".join(sql))


def _verify(env: dict[str, str], *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "openadapt_flow", "verify-run", *argv],
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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--per-fault", type=int, default=5)
    parser.add_argument("--max-cost", type=float, default=0.50)
    parser.add_argument("--faults", default=",".join(FAULTS))
    parser.add_argument("--first-item", type=int, default=901)
    args = parser.parse_args()

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
        model="claude-sonnet-5-5",
        verifier="openadapt-flow verify-run, rest on OpenEMR's patient API "
        "(read-only oracle client), with a baseline",
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
            fx.reset()
            values = openemr.item_values(i)
            before = _rows()
            item_dir = work / f"item-{i:03d}"
            item_dir.mkdir(parents=True, exist_ok=True)
            params = item_dir / "params.json"
            params.write_text(json.dumps(values))
            baseline = item_dir / "baseline.json"
            baseline.unlink(missing_ok=True)
            env = _token_env()
            cap = _verify(
                env,
                "--verify",
                str(verify_yaml),
                "--params",
                str(params),
                "--capture-baseline",
                str(baseline),
            )
            if cap.returncode != 0:
                raise SystemExit(f"baseline failed: {cap.stderr[-500:]}")
            poller = None
            if fault in TRIGGERS:
                _install_trigger(fault, max(int(k) for k in before))
            elif fault != "ok":
                poller = Poller(fault, before, values["email"])
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
            truth = ground_truth(before, values)
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
                        "executor_id": "claude-sonnet-5-5",
                        "executor_build": {
                            "agent": "scoreboard.agent.ClaudeComputerUseAgent"
                        },
                        "claimed_success": claimed,
                    }
                )
            )
            share = item_dir / "share"
            ver = _verify(
                _token_env(),
                "--verify",
                str(verify_yaml),
                "--params",
                str(params),
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
                if receipt_file.is_file()
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
