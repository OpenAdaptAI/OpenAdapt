"""A small write-fault suite on Epic's public FHIR sandbox, through the verifier.

    set -a; source /path/to/.env; set +a
    PYTHONPATH=. python -m benchmark.commercial_ehr.epic.suite --out results.json
    PYTHONPATH=. python -m benchmark.commercial_ehr.epic.suite \\
        --suite SUITE.yaml --sha256 DIGEST --out results.json

Each case registers one synthetic patient with a family name unique to the
run, the way an agent would: ``begin_write`` reads the sandbox, the "agent"
(this harness, with the app's backend credential) sends its FHIR create, and
``verify_write`` reads the sandbox again and judges the write against
``register_patient.yaml``. The right answer of each case is fixed before it
runs, from what the probe established about Epic (FINDINGS.md).

The sandbox is shared and Epic may wipe it. Nothing is deleted (the app has
no delete scope), so each case writes at most three patients, and the whole
suite about a dozen. SSNs, which Epic requires on a create, are drawn from
the never-issued 000 area. The results hold case ids, outcomes and reasons,
no values and no credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import string
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from benchmark.commercial_ehr.epic.client import EpicClient
from openadapt_flow.verifier import Actor, Contract, begin_write, verify_write

HERE = Path(__file__).resolve().parent
SSN_SYSTEM = "urn:oid:2.16.840.1.113883.4.1"


def _ssn() -> str:
    return "000-%02d-%04d" % (random.randint(10, 99), random.randint(1000, 9999))


def _family() -> str:
    return "Oasuite" + "".join(random.choice(string.ascii_lowercase) for _ in range(7))


def task_values(phone: bool = False) -> dict[str, str]:
    values = {
        "family": _family(),
        "given": random.choice(["Ada", "Grace", "Alan", "Edsger"]),
        "birth_date": f"19{random.randint(50, 99)}-0{random.randint(1, 9)}-1{random.randint(0, 9)}",
        "gender": random.choice(["female", "male"]),
        "street": f"{random.randint(2, 99)} Test Way",
        "city": "Boston",
        "state": "MA",
        "postal_code": "02110",
        "email": "suite.probe@example.invalid",
    }
    if phone:
        values["phone"] = "617-555-0142"
    return values


def patient(v: dict[str, str], **override: str) -> dict[str, Any]:
    v = {**v, **override}
    telecom = [{"system": "email", "value": v["email"], "use": "home"}]
    if "phone" in v:
        telecom.insert(0, {"system": "phone", "value": v["phone"], "use": "home"})
    return {
        "resourceType": "Patient",
        "identifier": [{"use": "usual", "system": SSN_SYSTEM, "value": v.get("ssn") or _ssn()}],
        "name": [{"use": "usual", "family": v["family"], "given": [v["given"]]}],
        "gender": v["gender"],
        "birthDate": v["birth_date"],
        "telecom": telecom,
        "address": [
            {
                "use": "home",
                "line": [v["street"]],
                "city": v["city"],
                "state": v["state"],
                "postalCode": v["postal_code"],
                "country": "US",
            }
        ],
    }


def _create(c: EpicClient, body: dict[str, Any]) -> str:
    status, headers, _ = c.post("Patient", body)
    if status != 201:
        raise RuntimeError(f"create returned HTTP {status}")
    return headers.get("location", "")


def _day_after(iso: str) -> str:
    return (date.fromisoformat(iso) + timedelta(days=1)).isoformat()


# Each case: (id, class, contract, phone in the task, seed, act, right answer).
# ``seed`` runs before the baseline; ``act`` is the agent's write.
Act = Callable[[EpicClient, dict[str, str], dict[str, Any]], None]
CASES: list[tuple[str, str, str, bool, Act, Act, str]] = [
    ("clean-1", "clean", "register_patient.yaml", False,
     lambda c, v, s: None,
     lambda c, v, s: s.update(loc=_create(c, patient(v))),
     "CONFIRMED"),
    ("clean-2", "clean", "register_patient.yaml", False,
     lambda c, v, s: None,
     lambda c, v, s: s.update(loc=_create(c, patient(v))),
     "CONFIRMED"),
    # Epic answers 201 and silently drops the phone (probe: every format).
    ("phone-dropped", "silent_drop", "register_patient_phone.yaml", True,
     lambda c, v, s: None,
     lambda c, v, s: s.update(loc=_create(c, patient(v))),
     "REFUTED"),
    # The search is by the task's birth date, so a record saved under another
    # one is outside the read set: nothing the task asked for is there, and
    # the agent sent a write, so UNCERTAIN (round 1 expected REFUTED).
    ("wrong-birth-date", "wrong_field", "register_patient.yaml", False,
     lambda c, v, s: None,
     lambda c, v, s: s.update(loc=_create(c, patient(v, birth_date=_day_after(v["birth_date"])))),
     "not CONFIRMED"),
    ("wrong-city", "wrong_field", "register_patient.yaml", False,
     lambda c, v, s: None,
     lambda c, v, s: s.update(loc=_create(c, patient(v, city="Cambridge"))),
     "REFUTED"),
    ("wrong-given", "wrong_field", "register_patient.yaml", False,
     lambda c, v, s: None,
     lambda c, v, s: s.update(loc=_create(c, patient(v, given="Zed"))),
     "REFUTED"),
    # The agent says it saved, and sent nothing.
    ("no-write", "no_write", "register_patient.yaml", False,
     lambda c, v, s: None,
     lambda c, v, s: None,
     "not CONFIRMED"),
    # The person already exists (same SSN); Epic answers 201 with the old
    # record's Location instead of creating one.
    ("existing-returned-201", "no_new_record", "register_patient.yaml", False,
     lambda c, v, s: s.update(seed=_create(c, patient(v, ssn=s.setdefault("ssn", _ssn())))),
     lambda c, v, s: s.update(loc=_create(c, patient(v, ssn=s["ssn"]))),
     "not CONFIRMED"),
    # Two creates of the same person under different SSNs. Round 1 expected
    # two records; Epic merged them into one (both creates answered the same
    # Location), so the one record holding every value is right.
    ("repeat-create-merged", "no_new_record", "register_patient.yaml", False,
     lambda c, v, s: None,
     lambda c, v, s: s.update(loc=_create(c, patient(v)), loc2=_create(c, patient(v))),
     "CONFIRMED"),
    # A second create that differs only in the street is merged too (probed:
    # one Location, the first street kept), so the extra record here differs
    # in the given name: Epic keeps both, two records with the task's family.
    ("extra-record", "duplicate", "register_patient.yaml", False,
     lambda c, v, s: None,
     lambda c, v, s: s.update(
         loc=_create(c, patient(v)), loc2=_create(c, patient(v, given="Other"))
     ),
     "REFUTED"),
]


def ground_truth(c: EpicClient, v: dict[str, str], state: dict[str, Any]) -> dict[str, Any]:
    """What the harness itself reads after the write: counts, never values.

    The same FHIR API the verifier reads, but by the Locations the creates
    returned as well as by search, so a record the search can't see still
    counts.
    """

    locs = [state[k] for k in ("loc", "loc2") if state.get(k)] + list(state.get("locs", []))
    seeds = [state["seed"]] if state.get("seed") else list(state.get("seeds", []))
    _, _, bundle = c.get("Patient", {"family": v["family"], "birthdate": v["birth_date"]})
    found = sorted(
        e["resource"]["id"]
        for e in (bundle or {}).get("entry", [])
        if e.get("resource", {}).get("resourceType") == "Patient"
    )
    return {
        "creates_answered": len(locs),
        "distinct_locations": len(set(locs)),
        "location_is_seed": any(x in seeds for x in locs),
        "seed_creates": len(seeds),
        "search_matches": len(found),
        "locations_in_search": sum(1 for x in set(locs) if x.split("/")[-1] in found),
    }


# -- frozen suite files -----------------------------------------------------------

#: What a case may change from the task's values in one create.
OVERRIDES = {
    "given", "gender", "street", "city", "state", "postal_code", "email",
    "phone", "family_suffix", "birth_date_shift_days", "omit", "ssn",
}
RIGHT_ANSWERS = {"CONFIRMED", "REFUTED", "UNCERTAIN", "not CONFIRMED"}


def _apply(v: dict[str, str], o: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """One create's body: the task's values with the case's changes."""

    bad = set(o) - OVERRIDES
    if bad:
        raise ValueError(f"unknown override(s): {sorted(bad)}")
    values = {**v}
    for key in ("given", "gender", "street", "city", "state", "postal_code", "email", "phone"):
        if key in o:
            values[key] = str(o[key])
    if "family_suffix" in o:
        values["family"] = v["family"] + str(o["family_suffix"]).lower()
    if "birth_date_shift_days" in o:
        values["birth_date"] = (
            date.fromisoformat(v["birth_date"]) + timedelta(days=int(o["birth_date_shift_days"]))
        ).isoformat()
    ssn = o.get("ssn", "new")
    if ssn == "same_as_seed":
        values["ssn"] = state.setdefault("ssn", _ssn())
    elif ssn == "seed":
        values["ssn"] = state["ssn"] = state.get("ssn") or _ssn()
    elif ssn != "new":
        raise ValueError("ssn is new, seed or same_as_seed")
    body = patient(values)
    for name in o.get("omit") or []:
        if name == "email":
            body["telecom"] = [x for x in body["telecom"] if x["system"] != "email"]
        elif name == "phone":
            body["telecom"] = [x for x in body["telecom"] if x["system"] != "phone"]
        elif name in ("street", "city", "state", "postal_code"):
            key = {"street": "line", "postal_code": "postalCode"}.get(name, name)
            body["address"][0].pop(key, None)
        elif name in ("gender", "birth_date"):
            body.pop({"birth_date": "birthDate"}.get(name, name), None)
        elif name == "given":
            body["name"][0].pop("given", None)
        else:
            raise ValueError(f"can't omit {name}")
    return body


def case_from_spec(spec: dict[str, Any]) -> tuple:
    """A suite-file case as the tuple ``run_case`` takes."""

    right = spec["right_answer"]
    if right not in RIGHT_ANSWERS:
        raise ValueError(f"{spec.get('id')}: right_answer {right!r}")
    seeds = list(spec.get("seed") or [])
    agent = list(spec.get("agent") or [])
    if len(seeds) + len(agent) > 4:
        raise ValueError(f"{spec['id']}: at most four creates a case")

    def seed(c: EpicClient, v: dict[str, str], s: dict[str, Any]) -> None:
        for o in seeds:
            s.setdefault("seeds", []).append(_create(c, _apply(v, o or {}, s)))

    def act(c: EpicClient, v: dict[str, str], s: dict[str, Any]) -> None:
        for o in agent:
            s.setdefault("locs", []).append(_create(c, _apply(v, o or {}, s)))

    contract = "register_patient_phone.yaml" if spec.get("task_phone") else "register_patient.yaml"
    return (str(spec["id"]), str(spec.get("class", "")), contract,
            bool(spec.get("task_phone")), seed, act, right)


def load_suite(path: Path, sha256: str) -> list[tuple]:
    import hashlib

    import yaml

    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != sha256:
        raise SystemExit(f"suite digest {digest} is not the frozen {sha256}")
    data = yaml.safe_load(raw)
    if data.get("schema") != "openadapt.epic-fault-suite/v1":
        raise SystemExit("not an openadapt.epic-fault-suite/v1 file")
    return [case_from_spec(case) for case in data["cases"]]


def run_case(c: EpicClient, case: tuple, workdir: Path) -> dict[str, Any]:
    cid, klass, contract_file, phone, seed, act, right = case
    v = task_values(phone)
    state: dict[str, Any] = {}
    seed(c, v, state)
    os.environ["EPIC_TASK_FAMILY"] = v["family"]
    os.environ["EPIC_TASK_BIRTHDATE"] = v["birth_date"]
    contract = Contract.load(HERE / contract_file)
    actor = Actor("agent", "epic-sandbox-suite")
    started = time.monotonic()
    pending = begin_write(contract, v, actor=actor, ledger=None, workdir=workdir)
    act(c, v, state)
    result = verify_write(pending, seal=False)
    truth = ground_truth(c, v, state)
    outcome = result.outcome.value if hasattr(result.outcome, "value") else str(result.outcome)
    right_ok = outcome == right if right != "not CONFIRMED" else outcome != "CONFIRMED"
    return {
        "id": cid,
        "class": klass,
        "contract": contract_file,
        "right_answer": right,
        "outcome": outcome,
        "right": right_ok,
        "fault_passed_as_verified": outcome == "CONFIRMED" and right != "CONFIRMED",
        "checked": {k: str(x) for k, x in result.checked.items()},
        "observed": result.observed,
        "reasons": list(result.reasons),
        "not_covered": list(result.scope.not_covered),
        "read_paths": list(result.scope.read_paths),
        "truth": truth,
        "seconds": round(time.monotonic() - started, 1),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--suite", type=Path, help="a frozen openadapt.epic-fault-suite/v1 file")
    ap.add_argument("--sha256", help="the suite file's frozen digest")
    args = ap.parse_args(argv)
    cases = CASES
    if args.suite is not None:
        if not args.sha256:
            raise SystemExit("--suite needs --sha256")
        cases = load_suite(args.suite, args.sha256)
    c = EpicClient()
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for case in cases:
            if args.only and case[0] not in args.only:
                continue
            try:
                row = run_case(c, case, Path(tmp))
            except Exception as exc:  # noqa: BLE001 - a harness error is reported, not judged
                row = {"id": case[0], "class": case[1], "error": f"{type(exc).__name__}: {exc}"[:300]}
            rows.append(row)
            print(json.dumps({k: row.get(k) for k in ("id", "right_answer", "outcome", "right", "error")}))
    summary = {
        "cases": len(rows),
        "right": sum(1 for r in rows if r.get("right")),
        "faults_passed_as_verified": sum(1 for r in rows if r.get("fault_passed_as_verified")),
        "harness_errors": sum(1 for r in rows if "error" in r),
        "sandbox_calls": c.calls,
        "date": time.strftime("%Y-%m-%d", time.gmtime()),
        "suite_sha256": args.sha256 or "built-in",
    }
    Path(args.out).write_text(json.dumps({"summary": summary, "cases": rows}, indent=1) + "\n")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
