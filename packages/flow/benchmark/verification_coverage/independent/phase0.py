"""The frozen independent fault suite against the production record check.

    python -m verification_coverage.independent.phase0 standin --baseline-sql PATH --out DIR
    python -m verification_coverage.independent.phase0 live --out DIR [--only id,...]
    ... --suite PATH   # another suite (a private held-out one) instead of faults.yaml
    ... --contract plus   # the production contract with the proposed fixes

``--contract production`` (the default) is the check the production
configuration admits for the OpenEMR referral: the ``record:`` contract of
``benchmark/lifecycle_proof/verify.yaml`` (one ``record_written`` with
``exact_new_set`` and ``identity_field: uuid``, a ``field_equals`` per field,
the state mapped from its label), judged by ``verify-run`` with a baseline and
its default frame. No attribution and no provenance: the production
configuration declares neither.

``--contract plus`` is the candidate contract,
``benchmark/lifecycle_proof/verify.frame.yaml`` (reads of the side tables, a
required history row, the provider checked, audit attribution keyed by
``pid``), with the provenance round trip against the referral.

``--contract whole`` is the whole-database candidate,
``benchmark/lifecycle_proof/verify.database.yaml``: the frame diffs every
table and column, with a reviewed allow-list, instead of the tables a
contract reads.

``standin`` reads an SQLite copy of the pinned fixture's baseline (writes
emulated by ``standin.py``). ``live`` runs on the pinned OpenEMR fixture
(the bot's save through the form, other writes through each user's REST
token, ``live.py``); it needs the fixture lease.

Each case is scored against SPEC.md: a fault case is ``caught`` when the
check does not issue ``verified``; a clean case (the clean save and the two
legitimate concurrent updates) is ``ok`` when it does. No model calls.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

HERE = Path(__file__).resolve().parent
BENCH = HERE.parents[1]
sys.path.insert(0, str(BENCH))

from verification_coverage.independent import run, standin  # noqa: E402

VERIFY_YAML = BENCH / "lifecycle_proof" / "verify.yaml"
FRAME_YAML = BENCH / "lifecycle_proof" / "verify.frame.yaml"
DATABASE_YAML = BENCH / "lifecycle_proof" / "verify.database.yaml"
PARAMS = (
    "title",
    "fname",
    "lname",
    "DOB",
    "sex",
    "street",
    "city",
    "state_label",
    "postal_code",
    "phone_home",
    "email",
    "country_code",
    "provider",
)
#: The tables OpenEMR's New Patient path writes or a stray write could touch,
#: read beside the patient table so the frame sees them (WITNESS.md).
SIDE_TABLES = {
    "history": "history_data",
    "notes": "pnotes",
    "allergies": "lists",
    "insurance": "insurance_data",
}
#: OpenEMR's audit trail: UI writes in ``log`` (by user name), API writes in
#: ``api_log`` (by user id). The stand-in emulates ``log`` only.
AUDIT_LOG_SQL = (
    "SELECT id, user, patient_id FROM log WHERE event LIKE 'patient-record-%' "
    "AND event <> 'patient-record-select' AND patient_id > 0"
)
AUDIT_LIVE_SQL = (
    "SELECT CONCAT('log-', id) AS id, user, patient_id FROM log "
    "WHERE event LIKE 'patient-record-%' AND event <> 'patient-record-select' "
    "AND patient_id > 0 AND user <> '' "
    "UNION ALL SELECT CONCAT('api-', a.id) AS id, u.username AS user, a.patient_id "
    "FROM api_log a JOIN users u ON u.id = a.user_id "
    "WHERE a.method IN ('POST', 'PUT', 'PATCH', 'DELETE') AND a.patient_id > 0"
)


def production_record() -> dict[str, Any]:
    """The ``record:`` section the production configuration admits."""

    doc = yaml.safe_load(VERIFY_YAML.read_text())
    return copy.deepcopy(doc["record"])


def production_doc(patient: dict[str, Any]) -> dict[str, Any]:
    doc = yaml.safe_load(VERIFY_YAML.read_text())
    return {
        "schema_version": "openadapt.verify/v1",
        "minimum_effect_strength": doc["minimum_effect_strength"],
        "verifier": patient,
        "record": production_record(),
    }


def plus_doc(
    patient: dict[str, Any], side: dict[str, dict[str, Any]], audit: dict[str, Any]
) -> dict[str, Any]:
    """The candidate contract (``lifecycle_proof/verify.frame.yaml``).

    Its record and frame are used as written; only the reads (the patient,
    the side tables, the audit trail) are replaced with this environment's.
    """

    doc = yaml.safe_load(FRAME_YAML.read_text())
    frame = copy.deepcopy(doc["frame"])
    frame["attribution"]["read"] = audit
    return {
        "schema_version": "openadapt.verify/v1",
        "minimum_effect_strength": doc["minimum_effect_strength"],
        "verifier": {"kind": "surfaces", "surfaces": {"patient": patient, **side}},
        "record": copy.deepcopy(doc["record"]),
        "frame": frame,
    }


def whole_doc(
    patient: dict[str, Any], audit: dict[str, Any], database: dict[str, Any]
) -> dict[str, Any]:
    """The whole-database candidate (``lifecycle_proof/verify.database.yaml``).

    Its record and frame are used as written; only the reads (the patient,
    the audit trail, the database connection) are this environment's.
    """

    doc = yaml.safe_load(DATABASE_YAML.read_text())
    frame = copy.deepcopy(doc["frame"])
    frame["attribution"]["read"] = audit
    frame["database"].update(database)
    return {
        "schema_version": "openadapt.verify/v1",
        "minimum_effect_strength": doc["minimum_effect_strength"],
        "verifier": {"kind": "surfaces", "surfaces": {"patient": patient}},
        "record": copy.deepcopy(doc["record"]),
        "frame": frame,
    }


def _source(cdir: Path, told: dict[str, str], referral: dict[str, str]) -> Any:
    """Provenance as a first reader would cite the referral, and its text."""

    from openadapt_flow.provenance import document_sha256, read_sources

    doc = cdir / "referral.txt"
    doc.write_text(run.referral_text(referral))
    provenance = run.provenance_for(document_sha256(doc), told, list(PARAMS))
    return provenance, read_sources([doc])


def _verdict(
    cfg: Any, params: dict[str, str], baseline: Path, source: Any = None
) -> dict[str, Any]:
    from openadapt_flow.verify_run import verify_run

    provenance, documents = source if source is not None else (None, None)
    result = verify_run(
        cfg,
        params=params,
        run_record={"executor_kind": "agent", "executor_id": run.BOT},
        authorization_ref="phase0-independent",
        baseline_path=baseline,
        provenance=provenance,
        documents=documents,
    )
    receipt = result["receipt"]
    return {
        "outcome": receipt.get("outcome"),
        "checked": receipt.get("checked"),
        "verified": receipt.get("outcome") == "verified",
        "evidence": result.get("evidence"),
    }


def _doc(
    contract: str,
    patient: dict[str, Any],
    table: Callable[[str], dict[str, Any]],
    audit: dict[str, Any],
    database: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if contract == "production":
        return production_doc(patient)
    if contract == "whole":
        assert database is not None
        return whole_doc(patient, audit, database)
    side = {name: table(t) for name, t in SIDE_TABLES.items()}
    return plus_doc(patient, side, audit)


def standin_case(case: dict[str, Any], work: Path, contract: str) -> dict[str, Any]:
    from openadapt_flow.verify_run import capture_baseline, parse_verify_config

    cdir = work / case["id"]
    cdir.mkdir(parents=True, exist_ok=True)
    db = standin.copy(work / "baseline.sqlite", cdir / "case.sqlite")
    store = standin.Store(db)
    ids = run.seed(store)
    run.prepare_window(store, case, ids)
    told = {**run.REFERRAL, **run.told_values(case)}

    def sql(query: str) -> dict[str, Any]:
        return {"kind": "sql", "sqlite_database": str(db), "sql_query": query}

    cfg = parse_verify_config(
        _doc(
            contract,
            sql("SELECT * FROM patient_data"),
            lambda t: sql(f"SELECT * FROM {t}"),
            sql(AUDIT_LOG_SQL),
            {"dialect": "sqlite", "read": {"sqlite_database": str(db)}},
        )
    )
    source = None if contract == "production" else _source(cdir, told, run.REFERRAL)
    params = {k: told[k] for k in PARAMS}
    capture_baseline(cfg, params, cdir / "baseline.json")
    run.apply_steps(store, case, ids, told)
    store.close()
    return _verdict(cfg, params, cdir / "baseline.json", source)


def live_case(case: dict[str, Any], work: Path, contract: str) -> dict[str, Any]:
    from scoreboard.claims_openemr import TOKEN_ENV

    from openadapt_flow.verify_run import capture_baseline, parse_verify_config
    from verification_coverage.independent import live

    f = live.fx()
    cdir = work / case["id"]
    cdir.mkdir(parents=True, exist_ok=True)
    live.reset()
    ids = live.seed()
    live.prepare_window(case, ids)
    told = {**live.REFERRAL, **run.told_values(case)}
    oracle = f.token_session("oracle")
    os.environ[TOKEN_ENV] = oracle.headers["Authorization"].removeprefix("Bearer ")
    oracle.close()
    container = os.environ["PHASE0_DB_CONTAINER"]

    def sql(query: str) -> dict[str, Any]:
        return {
            "kind": "sql",
            "sql_driver": "verification_coverage.exec_mariadb",
            "sql_connect_args": {
                "container": container,
                "user": live.AUDIT_USER,
                "database": "openemr",
            },
            "sql_password_env": live.AUDIT_ENV,
            "sql_query": query,
        }

    patient = {
        "kind": "rest",
        "base_url": f.ui_base_url,
        "records_path": "/apis/default/api/patient?_limit=1000",
        "records_key": "data",
        "auth": {"bearer_env": TOKEN_ENV},
    }
    read = {k: v for k, v in sql("").items() if k not in ("kind", "sql_query")}
    cfg = parse_verify_config(
        _doc(
            contract,
            patient,
            lambda t: sql(f"SELECT * FROM {t}"),
            sql(AUDIT_LIVE_SQL),
            {"dialect": "mysql", "read": read},
        )
    )
    source = None if contract == "production" else _source(cdir, told, live.REFERRAL)
    params = {k: told[k] for k in PARAMS}
    browser = live.Browser()
    deviations: list[str] = []
    error: Optional[str] = None
    try:
        capture_baseline(cfg, params, cdir / "baseline.json")
        live.apply_steps(case, ids, told, browser, deviations)
    except Exception as exc:  # noqa: BLE001 - recorded, the case is void
        error = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        browser.close()
    time.sleep(2)
    out = _verdict(cfg, params, cdir / "baseline.json", source)
    out.update(error=error, deviations=deviations)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("standin", "live"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--baseline-sql", type=Path)
    parser.add_argument("--only", default="")
    parser.add_argument(
        "--suite", type=Path, default=HERE / "faults.yaml", help="the fault suite"
    )
    parser.add_argument(
        "--contract", choices=("production", "plus", "whole"), default="production"
    )
    args = parser.parse_args()
    suite = yaml.safe_load(args.suite.read_text())
    # A suite that describes operations in words maps them to the harness's
    # names in <suite>.ops.yaml beside it (a private suite keeps both).
    aliases_path = args.suite.with_suffix(".ops.yaml")
    if aliases_path.exists():
        doc = yaml.safe_load(aliases_path.read_text())
        aliases, set_values = doc.get("ops", {}), doc.get("set_values", {})
        for case in suite["cases"]:
            for step in case["steps"]:
                alias = aliases.get(step["op"], step["op"])
                # An alias is a name, or a name with the step's arguments.
                step.update(alias if isinstance(alias, dict) else {"op": alias})
                for key, value in (step.get("set") or {}).items():
                    step["set"][key] = set_values.get(value, value)
    args.out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="phase0-"))
    if args.mode == "standin":
        if args.baseline_sql is None:
            raise SystemExit("standin needs --baseline-sql")
        standin.build(args.baseline_sql, work / "baseline.sqlite")
        standin.keep_tables(work / "baseline.sqlite", run.SLICE)
    rows_path = args.out / "rows.json"
    rows = json.loads(rows_path.read_text()) if rows_path.exists() else []
    done = {r["id"] for r in rows}
    for case in suite["cases"]:
        if (args.only and case["id"] not in args.only.split(",")) or case["id"] in done:
            continue
        case_fn = standin_case if args.mode == "standin" else live_case
        verdict = case_fn(case, work, args.contract)
        evidence = verdict.pop("evidence", None)
        (args.out / f"{case['id']}.evidence.json").write_text(
            json.dumps(evidence, indent=1, sort_keys=True, default=str)
        )
        score = run.score(case["expect"], verdict["verified"])
        rows.append(
            {
                "id": case["id"],
                "class": case["class"],
                "expect": case["expect"],
                "contract": args.contract,
                "verdict": {**verdict, "score": score},
            }
        )
        rows_path.write_text(
            json.dumps(rows, indent=1, sort_keys=True, default=str) + "\n"
        )
        print(
            case["id"],
            verdict["outcome"],
            score,
            verdict.get("error") or "",
            flush=True,
        )
    correct = sum(r["verdict"]["score"] in ("caught", "ok") for r in rows)
    print(f"correct: {correct}/{len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
