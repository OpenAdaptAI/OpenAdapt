"""Fresh agent runs on openIMIS, judged by three engines, per fault class.

    python -m verification_coverage.openimis_run --run-id <id> --per-fault 5

The openIMIS counterpart of ``openemr_run.py``: a computer-use agent (Claude
Sonnet 5.5) enters a health-facility claim through the openIMIS UI while a
fault is injected in the fixture's Postgres database (a trigger before the
save, or a poller after it). Each item is judged by the same three
``verify-run`` engines, each with its own baseline.

openIMIS stamps every claim row with the user who last wrote it
(``AuditUserID``) and when (``ValidityFrom``), so the ``after`` engine's
frame uses ``record_field`` attribution, where OpenEMR's uses its audit log.
A change whose modified-by names another user and whose ``ValidityFrom``
moved is a concurrent edit; anything else outside the declared claim is
collateral.

Fault classes as in ``openemr_run.py``, adapted: the trigger faults blank or
change the new claim's explanation, a removal sets ``ValidityTo`` (openIMIS
never deletes rows), and the misread changes the explanation the agent is
told while the claim form document keeps the true one.

The openIMIS stack is the pinned reference stack in ``openimis_claims``
(fetched and hash-checked at run time; nothing AGPL is stored here). All data
is synthetic.
"""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
sys.path.insert(0, str(BENCH))

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
from scoreboard.tasks import openimis  # noqa: E402

from verification_coverage.openemr_run import (  # noqa: E402
    CLEAN,
    ENGINES,
    FAULTS,
    engine_env,
    verify,
)

READER_ENV = "VC_IMIS_READER_PASSWORD"
OTHER_USER_ID = 2  # a demo-dataset user other than the actor (checked at run time)
MISREAD_FIELD = "explanation"

CLAIMS_SQL = (
    'SELECT c."ClaimID" AS claim_id, c."ClaimCode" AS claim_no, '
    'i."CHFID" AS insurance_no, c."Explanation" AS explanation, '
    'c."ClaimStatus" AS claim_status, hf."HFCode" AS hf_code, '
    'd."ICDCode" AS diagnosis, CAST(c."Claimed" AS TEXT) AS claimed, '
    'c."AuditUserID" AS audit_user, CAST(c."ValidityFrom" AS TEXT) AS validity_from '
    'FROM "tblClaim" c JOIN "tblInsuree" i ON i."InsureeID" = c."InsureeID" '
    'JOIN "tblHF" hf ON hf."HfID" = c."HFID" '
    'LEFT JOIN "tblICDCodes" d ON d."ICDID" = c."ICDID" '
    'WHERE c."ValidityTo" IS NULL ORDER BY c."ClaimID"'
)

FIELDS = (
    "item",
    "fault",
    "claimed_success",
    "ground_truth",
    *(f"{e}_outcome" for e in ENGINES),
    *(f"{e}_checked" for e in ENGINES),
    "presave_check",
    "fault_applied",
    "model_calls",
    "cost_usd",
    "host_load_1m",
    "final_text",
)


def verify_yaml(db_port: int, bot_id: int, *, frame: bool) -> str:
    match = (
        "    match:\n"
        "      claim_no: {param: claim_no}\n"
        "      insurance_no: {param: insurance_no}\n"
    )
    effects = (
        "  - kind: record_written\n" + match + "    count_new_only: true\n"
        "    timeout_s: 20\n"
    )
    for name, value in (
        ("explanation", "{param: explanation}"),
        ("claim_status", "'2'"),
        ("hf_code", openimis.HEALTH_FACILITY_CODE),
        ("diagnosis", "A389"),
    ):
        effects += (
            "  - kind: field_equals\n" + match + f"    field: {name}\n"
            f"    value: {value}\n    timeout_s: 20\n"
        )
    doc = (
        "schema_version: openadapt.verify/v1\n"
        "verifier:\n"
        "  kind: sql\n"
        "  sql_driver: pg8000.dbapi\n"
        "  sql_connect_args:\n"
        "    host: 127.0.0.1\n"
        f"    port: {db_port}\n"
        f"    user: {openimis.READER_ROLE}\n"
        "    database: IMIS\n"
        f"  sql_password_env: {READER_ENV}\n"
        f"  sql_query: '{CLAIMS_SQL}'\n"
        "effects:\n" + effects
    )
    if frame:
        doc += (
            "frame:\n"
            "  identity_field: claim_id\n"
            "  attribution:\n"
            "    kind: record_field\n"
            "    actor_field: audit_user\n"
            "    version_field: validity_from\n"
            f"    self: ['{bot_id}']\n"
        )
    return doc


def claim_form_text(values: dict[str, str]) -> str:
    return (
        "Health facility claim (synthetic test document)\n"
        f"Insurance No: {values['insurance_no']}\n"
        f"Claim No: {values['claim_no']}\n"
        f"Explanation: {values['explanation']}\n"
    )


def provenance(doc_sha: str, values: dict[str, str]) -> dict[str, Any]:
    params = {}
    for line, (name, label) in enumerate(
        (
            ("insurance_no", "Insurance No"),
            ("claim_no", "Claim No"),
            ("explanation", "Explanation"),
        ),
        start=2,
    ):
        params[name] = {
            "value": values[name],
            "source": {
                "kind": "document",
                "sha256": doc_sha,
                "line": line,
                "label": label,
            },
            "extractor": "model:simulated-first-reader",
        }
    return {"schema_version": "openadapt.param-provenance/v1", "params": params}


def _code(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9]{1,8}", value):
        raise ValueError("unexpected claim code")
    return value


def psql(sql: str) -> str:
    return openimis.fixture()._psql(sql)


def valid_claims() -> dict[int, dict[str, str]]:
    out = psql(
        'SELECT "ClaimID", "ClaimCode", coalesce("Explanation", \'\'), "AuditUserID" '
        'FROM "tblClaim" WHERE "ValidityTo" IS NULL ORDER BY "ClaimID";'
    )
    rows: dict[int, dict[str, str]] = {}
    for line in out.strip().splitlines():
        cid, code, expl, audit = line.split("|", 3)
        rows[int(cid)] = {"code": code, "explanation": expl, "audit_user": audit}
    return rows


def install_trigger(fault: str, claim_no: str) -> None:
    body = {
        "partial": "NEW.\"Explanation\" := '';",
        "wrong_value": 'NEW."Explanation" := NEW."Explanation" || \' (altered)\';',
    }[fault]
    psql(
        "CREATE OR REPLACE FUNCTION vc_fault() RETURNS trigger AS $$ BEGIN "
        f'IF NEW."ClaimCode" = \'{_code(claim_no)}\' AND NEW."Explanation" IS NOT NULL '
        f"AND NEW.\"Explanation\" <> '' THEN {body} END IF; RETURN NEW; END $$ LANGUAGE plpgsql;\n"
        'DROP TRIGGER IF EXISTS vc_fault ON "tblClaim";\n'
        'CREATE TRIGGER vc_fault BEFORE INSERT OR UPDATE ON "tblClaim" '
        "FOR EACH ROW EXECUTE FUNCTION vc_fault();\n"
    )


def drop_trigger() -> None:
    psql(
        'DROP TRIGGER IF EXISTS vc_fault ON "tblClaim"; DROP FUNCTION IF EXISTS vc_fault();'
    )


class Poller(threading.Thread):
    """Applies one fault once the new claim is written."""

    def __init__(self, fault: str, claim_no: str, victim: int, bot_id: int) -> None:
        super().__init__(daemon=True)
        self.fault, self.claim_no, self.victim, self.bot_id = (
            fault,
            claim_no,
            victim,
            bot_id,
        )
        self.applied = ""
        self.stop = threading.Event()

    def run(self) -> None:
        new = ""
        while not self.stop.is_set():
            new = psql(
                'SELECT "ClaimID" FROM "tblClaim" WHERE "ValidityTo" IS NULL AND '
                f"\"ClaimCode\" = '{_code(self.claim_no)}' AND \"Explanation\" <> '' LIMIT 1;"
            ).strip()
            if new:
                break
            time.sleep(0.5)
        if not new:
            return
        claim = int(new)
        time.sleep(1.0)  # let the save's own follow-up writes finish
        if self.fault == "duplicate":
            psql(
                'CREATE TEMP TABLE vc_dup AS SELECT * FROM "tblClaim" '
                f'WHERE "ClaimID" = {claim};\n'
                'UPDATE vc_dup SET "ClaimID" = nextval(pg_get_serial_sequence('
                "'\"tblClaim\"', 'ClaimID')), \"ClaimUUID\" = gen_random_uuid();\n"
                'INSERT INTO "tblClaim" SELECT * FROM vc_dup;\n'
            )
        elif self.fault == "no_persist":
            time.sleep(2.0)
            psql(
                f'UPDATE "tblClaim" SET "ValidityTo" = now() WHERE "ClaimID" = {claim};'
            )
        elif self.fault == "collateral_loss":
            psql(
                f'UPDATE "tblClaim" SET "ValidityTo" = now() WHERE "ClaimID" = {self.victim};'
            )
        elif self.fault.startswith(("collateral_edit", "concurrent_edit")):
            who = (
                f', "AuditUserID" = {self.bot_id}, "ValidityFrom" = now()'
                if self.fault == "collateral_edit_bot"
                else f', "AuditUserID" = {OTHER_USER_ID}, "ValidityFrom" = now()'
                if self.fault == "concurrent_edit_other"
                else ""
            )
            psql(
                'UPDATE "tblClaim" SET "Explanation" = coalesce("Explanation", \'\') '
                f"|| ' (edited)'{who} WHERE \"ClaimID\" = {self.victim};"
            )
        self.applied = f"{self.fault} at {time.strftime('%H:%M:%S')}"


def ground_truth(before: dict[int, dict[str, str]], values: dict[str, str]) -> str:
    after = valid_claims()
    if any(cid not in after for cid in before):
        return "collateral_loss"
    if any(after[cid] != row for cid, row in before.items()):
        return "collateral_edit"
    return openimis.check(values)


def bot_user_id() -> int:
    out = psql(
        'SELECT "UserID" FROM "tblUsers" WHERE "LoginName" = '
        f"'{openimis.ACTOR_USER}' AND \"ValidityTo\" IS NULL;"
    ).strip()
    if not out:
        raise SystemExit("cannot find the actor's openIMIS user id")
    return int(out.splitlines()[0])


def main() -> int:
    from openadapt_flow.provenance import (
        ProvenanceFile,
        cross_check,
        default_extractor,
        document_sha256,
        read_sources,
    )

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--per-fault", type=int, default=5)
    parser.add_argument("--max-cost", type=float, default=0.75)
    parser.add_argument("--faults", default=",".join(FAULTS))
    parser.add_argument("--first-item", type=int, default=1)
    parser.add_argument("--tag", default="VC")
    for engine in ENGINES:
        parser.add_argument(f"--engine-{engine}", required=True, type=Path)
    args = parser.parse_args()
    engines = {e: getattr(args, f"engine_{e}") for e in ENGINES}

    out = HERE / "results" / "openimis" / args.run_id
    work = schema.CACHE / "verification_coverage" / "openimis" / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    rows_path = out / "items.csv"
    done = (
        {int(r["item"]) for r in csv.DictReader(rows_path.open())}
        if rows_path.is_file()
        else set()
    )
    fx = openimis.fixture()
    openimis.bootstrap(fx)
    bot_id = bot_user_id()
    if bot_id == OTHER_USER_ID:
        raise SystemExit("the 'other user' id is the actor's own id; pick another")
    password = openimis._reader_password(fx)
    yamls = {
        "e1365": verify_yaml(fx.db_port, bot_id, frame=False),
        "main": verify_yaml(fx.db_port, bot_id, frame=False),
        "after": verify_yaml(fx.db_port, bot_id, frame=True),
    }
    import subprocess

    heads = {
        e: subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for e, root in engines.items()
    }
    schema.write_meta(
        out,
        task="openimis",
        arm="verification_coverage",
        model="claude-sonnet-5-5",
        engines=heads,
        verify_yaml=yamls["main"],
        verify_yaml_after=yamls["after"],
        faults=args.faults.split(","),
        per_fault=args.per_fault,
        fault_injection="fixture database only: a trigger or a poller (openimis_run.py)",
    )
    plan = list(
        enumerate(
            (f for f in args.faults.split(",") for _ in range(args.per_fault)),
            start=args.first_item,
        )
    )
    env = {READER_ENV: password}
    try:
        for i, fault in plan:
            if i in done:
                continue
            if spent() + args.max_cost > SPEND_CAP_USD:
                raise SystemExit("spend cap reached")
            drop_trigger()
            truth_values = openimis.item_values(i, args.tag)
            told = dict(truth_values)
            if fault == "misread_source":
                told[MISREAD_FIELD] = (
                    told[MISREAD_FIELD]
                    .replace("same day", "next day")
                    .replace("Follow-up", "Followed up")
                    .replace("Rash", "Rush")
                    .replace("Child", "Chlid")
                    .replace("Second", "Secnd")
                )
            before = valid_claims()
            victim = min(cid for cid, row in before.items() if row["explanation"])
            item_dir = work / f"item-{i:03d}"
            shutil.rmtree(item_dir, ignore_errors=True)
            item_dir.mkdir(parents=True)
            doc = item_dir / "claim.txt"
            doc.write_text(claim_form_text(truth_values))
            prov_doc = provenance(document_sha256(doc), told)
            (item_dir / "provenance.json").write_text(json.dumps(prov_doc))
            prov = ProvenanceFile.model_validate(prov_doc)
            presave = cross_check(prov, read_sources([doc]), default_extractor(prov))
            params = dict(told)
            baselines = {}
            for engine, root in engines.items():
                cfg = item_dir / f"verify-{engine}.yaml"
                cfg.write_text(yamls[engine])
                baselines[engine] = item_dir / f"baseline-{engine}.json"
                cap = verify(
                    root,
                    engine_env(root, env),
                    params,
                    "--verify",
                    str(cfg),
                    "--capture-baseline",
                    str(baselines[engine]),
                )
                if cap.returncode != 0:
                    raise SystemExit(f"{engine} baseline failed: {cap.stderr[-800:]}")
            poller = None
            if fault in ("partial", "wrong_value"):
                install_trigger(fault, told["claim_no"])
            elif fault not in ("ok", "misread_source"):
                poller = Poller(fault, told["claim_no"], victim, bot_id)
                poller.start()
            ledger = out / "ledgers" / f"item-{i:03d}.json"
            load0 = schema.load_1m()
            ctx = multiprocessing.get_context("spawn")
            box = ctx.Queue()
            proc = ctx.Process(
                target=_agent_item,
                args=(
                    openimis.NAME,
                    openimis.claim_form_url(),
                    openimis.instruction(told),
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
                poller.join(timeout=10.0)
                poller.stop.set()
            drop_trigger()
            view = _ledger_view(ledger, result)
            add_spend(
                view.cost_usd, f"verification_coverage openimis {args.run_id} item {i}"
            )
            claimed = reported_success(view, result.get("error"))  # type: ignore[arg-type]
            truth = ground_truth(before, truth_values)
            if fault == "concurrent_edit_other" and truth == "collateral_edit":
                truth = "correct"
            if fault == "misread_source" and truth == "wrong":
                truth = "wrong_vs_source"
            record = item_dir / "agent-run.json"
            record.write_text(
                json.dumps(
                    {
                        "executor_kind": "agent",
                        "executor_id": "claude-sonnet-5-5",
                        "claimed_success": claimed,
                    }
                )
            )
            row: dict[str, Any] = {
                "item": i,
                "fault": fault,
                "claimed_success": claimed,
                "ground_truth": truth,
                "presave_check": presave.status,
                "fault_applied": poller.applied if poller is not None else fault,
                "model_calls": len(view.calls),
                "cost_usd": round(view.cost_usd, 6),
                "host_load_1m": load0,
                "final_text": (result.get("error") or view.final_text)[:400],
            }
            for engine, root in engines.items():
                share = item_dir / f"share-{engine}"
                shutil.rmtree(share, ignore_errors=True)
                extra = (
                    [
                        "--provenance",
                        str(item_dir / "provenance.json"),
                        "--source",
                        str(doc),
                    ]
                    if engine == "after"
                    else []
                )
                ver = verify(
                    root,
                    engine_env(root, env),
                    params,
                    "--verify",
                    str(item_dir / f"verify-{engine}.yaml"),
                    "--run-record",
                    str(record),
                    "--authorization-ref",
                    f"VERIFY-COVERAGE-IMIS-{args.run_id}-{i:03d}",
                    "--baseline",
                    str(baselines[engine]),
                    "--out",
                    str(share),
                    *extra,
                )
                receipt_file = share / "effect-receipt.json"
                receipt = (
                    json.loads(receipt_file.read_text())["receipt"]
                    if receipt_file.is_file() and ver.returncode in (0, 1)
                    else {}
                )
                row[f"{engine}_outcome"] = receipt.get("outcome", "refused")
                row[f"{engine}_checked"] = (
                    json.dumps(receipt["checked"], sort_keys=True)
                    if receipt.get("checked")
                    else ""
                )
                if not receipt:
                    (item_dir / f"{engine}.stderr").write_text(ver.stderr[-4000:])
            new = not rows_path.is_file()
            with rows_path.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)
            print(
                f"vc-imis {i:3d} {fault:22s} truth={truth:16s} "
                + " ".join(f"{e}={row[f'{e}_outcome']}" for e in ENGINES)
                + f" presave={presave.status} ${view.cost_usd:.3f}",
                flush=True,
            )
    finally:
        drop_trigger()
    meta = json.loads((out / "meta.json").read_text())
    meta["complete"] = True
    (out / "meta.json").write_text(json.dumps(meta, indent=2, default=str) + "\n")
    return 0


__all__ = ["CLEAN", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
