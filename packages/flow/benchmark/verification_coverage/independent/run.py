"""Run ``faults.yaml`` through OpenAdapt's checks and score them against SPEC.md.

    python -m verification_coverage.independent.run --baseline-sql PATH --out DIR

Each case starts from a fresh SQLite copy of the pinned OpenEMR fixture's
baseline (``standin.py``), seeded with three charts and four users. The checks
under test are built before the case runs, the way OpenAdapt builds them:

- ``shipped``: the contract the OpenEMR referral task ships with
  (``scoreboard/claims_openemr.py`` ``VERIFY_YAML``), reading the whole
  patient table, with verify-run's default frame;
- ``framed``: the same, plus the frame with OpenEMR's audit log for
  attribution (as ``verification_coverage/openemr_run.py`` declares it) and
  provenance with the referral document (the round trip, #1446);
- ``synthesized``: what ``scaffold-verifier --db --fault-gate`` proposes from a
  demonstration on its own copy (``record_check_synthesis.synthesize``).

For every check the fault gate (#1445) also runs on the authoring copy, so its
own coverage claim can be set beside the independent result.

Verdicts: a check *catches* a ``fault`` case when verify-run does not issue
``verified``; it *misses* when it does. On a ``clean`` case, anything but
``verified`` is a *false alarm*. The pre-save provenance cross-check is scored
separately (it would stop the save before it happens).
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from verification_coverage.independent import standin  # noqa: E402

BOT = "openadapt_actor"
OTHER = "clinician2"

REFERRAL = {
    "title": "Ms.",
    "fname": "Ada",
    "lname": "Quill",
    "DOB": "1984-03-07",
    "sex": "Female",
    "street": "41 Larch Way",
    "city": "Cambridge",
    "state_label": "Massachusetts",
    "postal_code": "02139",
    "phone_home": "617-555-0142",
    "email": "ada.quill@example.test",
    "country_code": "USA",
    "provider": "Dr. Iris Holt",
}
#: The demonstration the synthesized check is built from (another person).
DEMO = {
    "title": "Mr.",
    "fname": "Tobias",
    "lname": "Wren",
    "DOB": "1971-09-30",
    "sex": "Male",
    "street": "12 Quarry Lane",
    "city": "Medford",
    "state_label": "Massachusetts",
    "postal_code": "02155",
    "phone_home": "781-555-0110",
    "email": "t.wren@example.test",
    "country_code": "USA",
    "provider": "Dr. Iris Holt",
}
SEEDS = {
    "other_patient": {
        "title": "Mr.",
        "fname": "Wendell",
        "lname": "Priorchart",
        "DOB": "1962-06-14",
        "sex": "Male",
        "street": "9 Existing Road",
        "city": "Exampleton",
        "state": "MA",
        "postal_code": "02139",
        "phone_home": "617-555-0188",
        "email": "w.priorchart@example.test",
        "country_code": "USA",
    },
    "lookalike": {
        "title": "Ms.",
        "fname": "Ada",
        "lname": "Quill",
        "DOB": "1984-07-03",
        "sex": "Female",
        "street": "7 Elm Street",
        "city": "Lowell",
        "state": "MA",
        "postal_code": "01852",
        "phone_home": "978-555-0123",
        "email": "aquill84@example.test",
        "country_code": "USA",
    },
    "second_other_patient": {
        "title": "Mrs.",
        "fname": "Mabel",
        "lname": "Ortiz",
        "DOB": "1990-01-21",
        "sex": "Female",
        "street": "300 Main Street",
        "city": "Waltham",
        "state": "MA",
        "postal_code": "02451",
        "phone_home": "781-555-0164",
        "email": "m.ortiz@example.test",
        "country_code": "USA",
    },
}
CONCURRENT_PERSON = {
    "title": "Mr.",
    "fname": "Basil",
    "lname": "Fenn",
    "DOB": "1979-11-22",
    "sex": "Male",
    "street": "5 Pond Road",
    "city": "Arlington",
    "state": "MA",
    "postal_code": "02474",
    "phone_home": "781-555-0131",
    "email": "b.fenn@example.test",
    "country_code": "USA",
}
#: Referral label per parameter, in document order (provider is last).
LABELS = (
    ("title", "Title"),
    ("fname", "First name"),
    ("lname", "Last name"),
    ("DOB", "Date of birth"),
    ("sex", "Sex"),
    ("street", "Street"),
    ("city", "City"),
    ("state_label", "State"),
    ("postal_code", "Postal code"),
    ("phone_home", "Home phone"),
    ("email", "Email"),
    ("country_code", "Country"),
    ("provider", "Primary care provider"),
)
#: Parameters the shipped contract binds (state is a literal MA there).
SHIPPED_PARAMS = (
    "title",
    "fname",
    "lname",
    "DOB",
    "sex",
    "street",
    "city",
    "postal_code",
    "phone_home",
    "email",
    "country_code",
)
SHIPPED_RECORD = yaml.safe_load(
    """
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
  state: {literal: MA}
  postal_code: postal_code
  phone_home: phone_home
  country_code: country_code
"""
)
#: ``--slice``: the tables SPEC.md names, the New Patient path's side tables,
#: and the bookkeeping OpenEMR writes for any signed-in user.
SLICE = (
    "patient_data",
    "history_data",
    "insurance_data",
    "employer_data",
    "lists",
    "pnotes",
    "transactions",
    "form_encounter",
    "forms",
    "prescriptions",
    "documents",
    "openemr_postcalendar_events",
    "users",
    "facility",
    "contact",
    "recent_patients",
    "user_settings",
    "clinical_rules_log",
    "log",
    "log_comment_encrypt",
    "api_log",
    "uuid_registry",
    "uuid_mapping",
    "sequences",
)
AUDIT_QUERY = (
    "SELECT id, user, patient_id FROM log WHERE event LIKE 'patient-record-%' "
    "AND event <> 'patient-record-select' AND patient_id > 0"
)


#: Operations the held-out suite describes in words (``heldout.yaml``), each
#: implemented as written, with the chart it acts on.
APPOINTMENT_OP = (
    "create one calendar appointment (a new openemr_postcalendar_events row) for "
    "new_patient with provider Dr. Iris Holt, category 'New Patient', on the next "
    "weekday at 09:00, 30 minutes"
)
DOUBLE_HISTORY_OP = (
    "insert a second history_data row for new_patient's pid, a copy of the first "
    "one with a new id and the same date"
)
REVERT_OP = (
    "update other_patient's phone_home back to the value it had before the run "
    "(reverting clinician2's change)"
)
HELD_OUT_OPS = {
    APPOINTMENT_OP: "new_patient",
    DOUBLE_HISTORY_OP: "new_patient",
    REVERT_OP: "other_patient",
}


def step_values(step: dict[str, Any], told: dict[str, str]) -> dict[str, str]:
    """The values one save enters: ``told``, or the step's own inline values."""

    values = step.get("values")
    return {**told, **values} if isinstance(values, dict) else told


def person_values(step: dict[str, Any], ids: dict[str, int]) -> dict[str, Any]:
    """Another user's new patient: the step's person map, or the suite's default."""

    person = step.get("person")
    if not isinstance(person, dict):
        return {**CONCURRENT_PERSON, "providerID": ids["iholt"]}
    blank = dict.fromkeys(CONCURRENT_PERSON, "")
    out = {**blank, **person}
    out["state"] = "MA" if out["state"] == "Massachusetts" else out["state"]
    return {**out, "providerID": None}


def told_values(case: dict[str, Any]) -> dict[str, str]:
    """A case's misread inputs, keyed by referral field (``providerID`` in the
    suite names the referral's provider)."""

    told = dict(case.get("told", {}))
    if "providerID" in told:
        told["provider"] = told.pop("providerID")
    return told


def referral_text(values: dict[str, str]) -> str:
    lines = ["Referral (synthetic test document)", "Referred to: Example Family Clinic"]
    lines += [f"{label}: {values[name]}" for name, label in LABELS]
    return "\n".join(lines) + "\n"


def seed(store: standin.Store) -> dict[str, int]:
    ids = {
        OTHER: store.add_user(OTHER, "Casey", "Clinician", 1),
        "iholt": store.add_user("iholt", "Iris", "Holt", 1),
        "ohale": store.add_user("ohale", "Owen", "Hale", 1),
    }
    for key, values in SEEDS.items():
        ids[key] = store.register(BOT, {**values, "providerID": ids["ohale"]})
    return ids


def stored(values: dict[str, str], ids: dict[str, int]) -> dict[str, Any]:
    """What a correct New Patient save stores for these referral values."""

    out = {k: v for k, v in values.items() if k not in ("state_label", "provider")}
    out["state"] = (
        "MA" if values["state_label"] == "Massachusetts" else values["state_label"]
    )
    out["providerID"] = ids[
        "iholt" if values["provider"] == "Dr. Iris Holt" else "ohale"
    ]
    return out


# -- checks ----------------------------------------------------------------------


def shipped_doc(db: Path) -> dict[str, Any]:
    return {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "sql",
            "sqlite_database": str(db),
            "sql_query": "SELECT * FROM patient_data",
        },
        "record": copy.deepcopy(SHIPPED_RECORD),
    }


def framed_doc(db: Path) -> dict[str, Any]:
    doc = shipped_doc(db)
    doc["frame"] = {
        "identity_field": "pid",
        "attribution": {
            "kind": "audit_read",
            "actor_field": "user",
            "record_field": "patient_id",
            "self": [BOT],
            "read": {
                "kind": "sql",
                "sqlite_database": str(db),
                "sql_query": AUDIT_QUERY,
            },
        },
    }
    return doc


def _repoint(node: Any, old: str, new: str) -> Any:
    if isinstance(node, dict):
        return {k: _repoint(v, old, new) for k, v in node.items()}
    if isinstance(node, list):
        return [_repoint(v, old, new) for v in node]
    return new if node == old else node


def synthesize_check(work: Path) -> dict[str, Any]:
    """scaffold-verifier --db --fault-gate on a demonstration copy."""

    from openadapt_flow.record_check_synthesis import synthesize

    demo_db = standin.copy(work / "baseline.sqlite", work / "demo.sqlite")
    store = standin.Store(demo_db)
    ids = seed(store)
    values = stored(DEMO, ids)
    store.register(BOT, values)
    store.close()
    params = {k: str(v) for k, v in values.items() if k != "state"}
    from openadapt_flow.fault_gate import GateError

    gate_error = None
    try:
        proposal = synthesize(None, params=params, db_path=demo_db, fault_gate=True)
    except GateError as exc:
        # The gate refused to run (fail-closed); measure the check
        # scaffold-verifier proposes without it.
        gate_error = str(exc)
        proposal = synthesize(None, params=params, db_path=demo_db, fault_gate=False)
    effects = [
        e.model_copy(update={"needs_operator_confirmation": False}).model_dump(
            mode="json", exclude_defaults=True
        )
        for e in proposal.effects
    ]
    cov = proposal.coverage
    return {
        "doc": {
            "schema_version": "openadapt.verify/v1",
            "verifier": proposal.verifier,
            "effects": effects,
        },
        "demo_db": str(demo_db),
        "params": sorted(params),
        "working": proposal.working,
        "misses": proposal.misses,
        "surfaces": [s.name for s in proposal.search.surfaces],
        "closed_world": proposal.search.closed_world_basis,
        "coverage": None if cov is None else cov.model_dump(mode="json"),
        "gate_error": gate_error,
        "targets": [
            {"surface": x.name, "bound": dict(x.bound)}
            for x in proposal.search.surfaces
            if x.is_target
        ],
    }


def gate_shipped(work: Path, framed: bool) -> dict[str, Any]:
    """The fault gate (#1445) on the shipped contract, on an authoring copy."""

    from openadapt_flow.fault_gate import SqliteCopy, run_gate
    from openadapt_flow.verify_run import parse_verify_config

    src = standin.copy(work / "baseline.sqlite", work / f"gate-{framed}.sqlite")
    store = standin.Store(src)
    ids = seed(store)
    store.close()
    target = stored(DEMO, ids)
    copy_ = SqliteCopy(src, "patient_data", target, workdir=work / f"gatecopy-{framed}")
    doc = (framed_doc if framed else shipped_doc)(copy_.path)
    params = {k: str(target[k]) for k in SHIPPED_PARAMS}
    record = run_gate(parse_verify_config(doc), params, copy_)
    return record.model_dump(mode="json")


# -- one case --------------------------------------------------------------------


def apply_steps(
    store: standin.Store,
    case: dict[str, Any],
    ids: dict[str, int],
    told: dict[str, str],
) -> Optional[int]:
    new_pid: Optional[int] = None
    actor = {"bot": BOT, "clinician2": OTHER, "none": None}
    for step in case["steps"]:
        who = actor[step["actor"]]
        op = step["op"]
        if op == "register_referral":
            if step.get("phase") == "finish":
                continue
            new_pid = store.register(who, stored(step_values(step, told), ids))
        elif op == "register_patient":
            store.register(who, person_values(step, ids))
        elif op in HELD_OUT_OPS:
            pid = new_pid if HELD_OUT_OPS[op] == "new_patient" else ids["other_patient"]
            if op == APPOINTMENT_OP:
                store.add_appointment(who, pid, ids["iholt"], "New Patient")
            elif op == DOUBLE_HISTORY_OP:
                store.duplicate_history(who, pid)
            else:  # REVERT_OP: the value the chart had before the run
                field = "phone_home"
                store.update_chart(who, pid, {field: SEEDS["other_patient"][field]})
        elif op == "overwrite_chart":
            values = stored(REFERRAL, ids)
            store.update_chart(who, ids[step["target"]], values)
        else:
            pid = new_pid if step["target"] == "new_patient" else ids[step["target"]]
            if op == "update_chart":
                values = stored(REFERRAL, ids)
                changes = {
                    k: (values[k] if v == "referral" else v)
                    for k, v in step["set"].items()
                }
                store.update_chart(who, pid, changes)
            elif op == "update_history":
                store.update_history(who, pid, step["set"])
            elif op == "delete_history":
                store.delete_history(who, pid)
            elif op == "add_note":
                store.add_note(who, pid, step["body"])
            elif op == "add_allergy":
                store.add_allergy(who, pid, step["title"])
            elif op == "add_insurance":
                store.add_insurance(who, pid, step["provider"])
            else:
                raise ValueError(f"unknown op {op}")
    return new_pid


def provenance_for(doc_sha: str, told: dict[str, str], names: list[str]) -> Any:
    from openadapt_flow.provenance import ProvenanceFile

    line_of = {name: 3 + i for i, (name, _l) in enumerate(LABELS)}
    label_of = dict(LABELS)
    params = {}
    for name in names:
        if name not in line_of:
            continue
        entry = {
            "value": told[name],
            "source": {
                "kind": "document",
                "sha256": doc_sha,
                "line": line_of[name],
                "label": label_of[name],
            },
            "extractor": "model:simulated-first-reader",
        }
        if name == "DOB":
            entry["value_kind"] = "date"
        params[name] = entry
    return ProvenanceFile.model_validate(
        {"schema_version": "openadapt.param-provenance/v1", "params": params}
    )


def run_case(case: dict[str, Any], work: Path, synth: dict[str, Any]) -> dict[str, Any]:
    from openadapt_flow.intake.document import IntakeDocument  # noqa: F401
    from openadapt_flow.provenance import (
        cross_check,
        default_extractor,
        document_sha256,
        read_sources,
    )
    from openadapt_flow.verify_run import (
        capture_baseline,
        parse_verify_config,
        verify_run,
    )

    cdir = work / case["id"]
    cdir.mkdir(parents=True, exist_ok=True)
    db = standin.copy(work / "baseline.sqlite", cdir / "case.sqlite")
    store = standin.Store(db)
    ids = seed(store)
    told = {**REFERRAL, **told_values(case)}
    doc_path = cdir / "referral.txt"
    doc_path.write_text(referral_text(REFERRAL))
    documents = read_sources([doc_path])
    doc_sha = document_sha256(doc_path)
    told_stored = stored(told, ids)
    shipped_params = {k: str(told_stored[k]) for k in SHIPPED_PARAMS}
    synth_params = {k: str(told_stored[k]) for k in synth["params"]}

    checks = {
        "shipped": (shipped_doc(db), shipped_params, None),
        "framed": (
            framed_doc(db),
            shipped_params,
            provenance_for(doc_sha, told, list(SHIPPED_PARAMS)),
        ),
        "synthesized": (
            _repoint(synth["doc"], synth["demo_db"], str(db)),
            synth_params,
            provenance_for(doc_sha, told, list(SHIPPED_PARAMS)),
        ),
    }
    cfgs = {name: parse_verify_config(doc) for name, (doc, _p, _v) in checks.items()}
    for name, cfg in cfgs.items():
        capture_baseline(cfg, checks[name][1], cdir / f"baseline-{name}.json")

    prov = provenance_for(doc_sha, told, [n for n, _ in LABELS])
    pre = cross_check(prov, documents, default_extractor(prov))

    apply_steps(store, case, ids, told)
    store.close()

    row: dict[str, Any] = {
        "id": case["id"],
        "class": case["class"],
        "expect": case["expect"],
        "presave": pre.status,
        "presave_held_fields": pre.disagreements,
    }
    for name, cfg in cfgs.items():
        _doc, params, provenance = checks[name]
        result = verify_run(
            cfg,
            params=params,
            run_record={"executor_kind": "agent", "executor_id": BOT},
            authorization_ref="independent-suite",
            baseline_path=cdir / f"baseline-{name}.json",
            provenance=provenance,
            documents=documents if provenance is not None else None,
        )
        receipt = result["receipt"]
        outcome = receipt.get("terminal_outcome") or receipt.get("outcome")
        row[name] = {
            "outcome": outcome,
            "verified": outcome == "verified",
            "checked": receipt.get("checked"),
        }
        (cdir / f"evidence-{name}.json").write_text(
            json.dumps(result.get("evidence"), indent=2, sort_keys=True, default=str)
        )
    return row


def score(expect: str, verified: bool) -> str:
    if expect == "fault":
        return "missed" if verified else "caught"
    return "ok" if verified else "false_alarm"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline-sql", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=HERE / "results" / "standin")
    parser.add_argument("--only", default="")
    parser.add_argument("--work", type=Path, default=None)
    parser.add_argument(
        "--slice",
        action="store_true",
        help="keep only the tables SPEC.md names and OpenEMR's bookkeeping",
    )
    args = parser.parse_args()
    suite = yaml.safe_load((HERE / "faults.yaml").read_text())
    work = args.work or Path(tempfile.mkdtemp(prefix="indep-verif-"))
    work.mkdir(parents=True, exist_ok=True)
    if not (work / "baseline.sqlite").exists():
        standin.build(args.baseline_sql, work / "baseline.sqlite")
        if args.slice:
            standin.keep_tables(work / "baseline.sqlite", SLICE)
    cache = work / "synth.json"
    if cache.exists():
        synth = json.loads(cache.read_text())
    else:
        synth = synthesize_check(work)
        cache.write_text(json.dumps(synth, default=str))
    gates = {
        "shipped": gate_shipped(work, False),
        "framed": gate_shipped(work, True),
        "synthesized": synth["coverage"],
    }
    rows = []
    for case in suite["cases"]:
        if args.only and case["id"] not in args.only.split(","):
            continue
        row = run_case(case, work, synth)
        for name in ("shipped", "framed", "synthesized"):
            row[name]["score"] = score(case["expect"], row[name]["verified"])
        row["presave_score"] = score(case["expect"], row["presave"] == "agree")
        rows.append(row)
        print(
            case["id"],
            {
                n: (row[n]["outcome"], row[n]["score"])
                for n in ("shipped", "framed", "synthesized")
            },
            "presave",
            row["presave"],
            flush=True,
        )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "rows.json").write_text(
        json.dumps(rows, indent=2, sort_keys=True) + "\n"
    )
    meta = {k: v for k, v in synth.items() if k not in ("doc", "demo_db")}
    meta["doc"] = _repoint(synth["doc"], synth["demo_db"], "<case database>")
    (args.out / "checks.json").write_text(
        json.dumps(
            {"synthesized": meta, "gates": gates}, indent=2, sort_keys=True, default=str
        )
        + "\n"
    )
    print("work dir:", work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
