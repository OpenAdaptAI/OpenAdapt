"""Seed the planted-fault fixture set into real systems, reconcile each unit,
and score it.

- EHR: a local Medplum (Apache-2.0), images pinned by digest
  (``benchmark/fhir_medplum``), on loopback ports 9841 (server) and 9842
  (app).
- Payer: the HL7 Da Vinci PAS reference implementation (``HL7-DaVinci/
  prior-auth``, Apache-2.0), built from a pinned commit into a local image and
  run on loopback port 9840. Nothing of it is stored in this repository.
- Clearinghouse: X12 278 and 277 files written by ``x12_fixtures.py``.

Synthetic data only. Commands::

    python benchmark/reconcile/run.py build-pas   # build the PAS image
    python benchmark/reconcile/run.py up          # start Medplum and PAS
    python benchmark/reconcile/run.py run --out DIR
    python benchmark/reconcile/run.py down        # remove containers and volumes

The reconciliation is evaluated at the run's start plus
``evaluated_at_offset_s`` (36 hours), so that due times have passed for the
units submitted days ago and not for those submitted at the start. The
stores aren't changed in between.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError

HERE = Path(__file__).resolve().parent
FLOW = HERE.parents[1]
STATE = Path.home() / "oa/scratch/pa-reconciler"
PAS_REPO = "https://github.com/HL7-DaVinci/prior-auth.git"
PAS_COMMIT = "848f28c11d8efb4e253b70cfbbc485acf9acd1a0"
PAS_IMAGE = f"localhost/openadapt-pa-recon-pas:{PAS_COMMIT[:12]}-p1"
#: One upstream race fixed before the build: ProcessClaimItemTask.start()
#: starts the item's thread, then marks it running, which overwrites the
#: success of an item that finished first, so a submit fails at random
#: (HTTP 400 after the Claim is stored). The patch marks it running first.
PAS_PATCH = (
    "src/main/java/org/hl7/davinci/priorauth/ProcessClaimItemTask.java",
    "            this.thread.start();\n            this.threadStatus = 3; // Running\n",
    "            this.threadStatus = 3; // Running\n            this.thread.start();\n",
)
PAS_NAME = "openadapt-pa-recon-pas"
PAS_PORT = 9840
EHR_PORT, EHR_APP_PORT = 9841, 9842
SUBMISSION = "urn:openadapt:synthetic:submission"
MEMBER = "urn:openadapt:synthetic:member"
CPT = "http://www.ama-assn.org/go/cpt"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


fx = _load("x12_fixtures", HERE / "x12_fixtures.py")
medplum = _load("medplum_fixture", FLOW / "benchmark/fhir_medplum/fixture.py")


def _ehr() -> Any:
    return medplum.MedplumFixture(
        state_dir=STATE / "medplum-state",
        server_port=EHR_PORT,
        app_port=EHR_APP_PORT,
        project="openadapt-pa-recon-medplum",
    )


def _http(method: str, url: str, body: Any = None, token: str = "") -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/fhir+json")
    if data is not None:
        req.add_header("Content-Type", "application/fhir+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        raw = resp.read()
    return json.loads(raw) if raw else None


def _sh(*args: str, check: bool = True) -> str:
    proc = subprocess.run(args, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SystemExit(f"{' '.join(args[:3])} failed: {proc.stderr[-2000:]}")
    return proc.stdout


# -- the stack -----------------------------------------------------------------


def build_pas() -> None:
    src = STATE / "vendor/prior-auth"
    if not src.is_dir():
        _sh("git", "clone", "-q", PAS_REPO, str(src))
    _sh("git", "-C", str(src), "fetch", "-q", "--depth", "1", "origin", PAS_COMMIT)
    _sh("git", "-C", str(src), "checkout", "-q", "-f", PAS_COMMIT)
    path, racy, fixed = PAS_PATCH
    java = src / path
    text = java.read_text()
    if text.count(racy) != 1:
        raise SystemExit(f"the PAS patch no longer applies to {path}")
    java.write_text(text.replace(racy, fixed))
    _sh("docker", "build", "-t", PAS_IMAGE, str(src))


def up() -> None:
    ehr = _ehr()
    ehr.up()
    _sh("docker", "rm", "-f", PAS_NAME, check=False)
    _sh(
        "docker",
        "run",
        "-d",
        "--name",
        PAS_NAME,
        "-p",
        f"127.0.0.1:{PAS_PORT}:9015",
        # Loopback only, synthetic data: the reference implementation's own
        # switch for running without its OAuth registration.
        "-e",
        "BYPASS_AUTH=true",
        "-e",
        "TOKEN_BASE_URI=http://localhost:9015",
        # It grants a pended request on a timer (15 s by default): sooner,
        # so the run waits less.
        "-e",
        "DELAY=2000",
        PAS_IMAGE,
    )
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        try:
            _http("GET", f"{pas_url()}/metadata")
            print("up")
            return
        except (URLError, HTTPError, OSError):
            time.sleep(10)
    raise SystemExit("the PAS reference implementation did not start")


def down() -> None:
    _sh("docker", "rm", "-f", PAS_NAME, check=False)
    ehr = _ehr()
    ehr.down(volumes=True)
    # Its clients and users lived in the volumes just removed.
    for name in ("clients.json", "secrets.json"):
        (ehr.state_dir / name).unlink(missing_ok=True)


def pas_url() -> str:
    return f"http://localhost:{PAS_PORT}/fhir"


# -- seeding ---------------------------------------------------------------------


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _patient(u: dict, member: str) -> dict:
    return {
        "resourceType": "Patient",
        "identifier": [
            {
                "type": {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                            "code": "MB",
                        }
                    ]
                },
                "system": MEMBER,
                "value": member,
            }
        ],
        "name": [{"family": u["family"], "given": ["Synthetic"]}],
        "birthDate": "1970-01-01",
    }


def _claim(
    u: dict, *, sid: str, code: str, day: str, created: str, patient_ref: str, use: str
) -> dict:
    return {
        "resourceType": "Claim",
        "identifier": [{"system": SUBMISSION, "value": sid}],
        "status": "active",
        "type": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/claim-type",
                    "code": "professional",
                }
            ]
        },
        "use": use,
        "patient": {"reference": patient_ref},
        "created": created,
        "provider": {"reference": "Organization/synthclinic"},
        "insurer": {"reference": "Organization/synthpayer"},
        "priority": {"coding": [{"code": "normal"}]},
        "insurance": [
            {
                "sequence": 1,
                "focal": True,
                "coverage": {"reference": "Coverage/synthcov"},
            }
        ],
        "item": [
            {
                "sequence": 1,
                "productOrService": {"coding": [{"system": CPT, "code": code}]},
                "servicedDate": day,
            }
        ],
    }


def _next_day(day: str) -> str:
    return (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()


def pas_submit(u: dict, *, sid: str, code: str, day: str, created: str) -> dict:
    """Submit one PAS request Bundle; the ClaimResponse the payer returns."""

    pid = f"pat{u['n']:03d}"
    claim = _claim(
        u,
        sid=sid,
        code=code,
        day=day,
        created=created,
        patient_ref=f"Patient/{pid}",
        use="preauthorization",
    )
    patient = {**_patient(u, u["member_id"]), "id": pid}
    patient["identifier"] = [{"system": MEMBER, "value": u["member_id"]}]
    entries = [
        claim,
        patient,
        {
            "resourceType": "Coverage",
            "id": "synthcov",
            "status": "active",
            "subscriberId": u["member_id"],
            "beneficiary": {"reference": f"Patient/{pid}"},
            "payor": [{"reference": "Organization/synthpayer"}],
        },
        {"resourceType": "Organization", "id": "synthpayer", "name": "Synthetic payer"},
        {
            "resourceType": "Organization",
            "id": "synthclinic",
            "name": "Synthetic clinic",
        },
    ]
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "timestamp": created,
        "entry": [
            {"fullUrl": f"urn:uuid:{e['resourceType']}-{sid}", "resource": e}
            for e in entries
        ],
    }
    reply = _http("POST", f"{pas_url()}/Claim/$submit", bundle)
    followup = False
    if reply.get("resourceType") == "Bundle":
        kinds = [e["resource"]["resourceType"] for e in reply["entry"]]
        # A CommunicationRequest asks for more documentation: the reference
        # implementation keeps that request pended, with no timed grant.
        followup = "CommunicationRequest" in kinds
        reply = reply["entry"][kinds.index("ClaimResponse")]["resource"]
    if reply.get("resourceType") != "ClaimResponse":
        raise SystemExit(f"PAS refused {sid}: {json.dumps(reply)[:400]}")
    return {**reply, "_followup": followup}


#: X12 HCR01 for a PAS disposition: the reference implementation's own
#: table (FhirUtils.dispositionToReviewAction).
HCR = {
    "Granted": "A1",
    "Partial": "A2",
    "Denied": "A3",
    "Pending": "A4",
    "Cancelled": "A6",
}


def pas_decision(member_id: str, sid: str, first: dict) -> dict:
    """The latest ClaimResponse for ``sid``: the reference implementation
    pends a request whose code its rules don't know, then grants it on a
    timer as a new ClaimResponse, unless it asked for more documentation."""

    if first.get("outcome") != "queued" or first.get("_followup"):
        return first
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        found = _http(
            "GET", f"{pas_url()}/ClaimResponse?patient.identifier={member_id}"
        )
        mine = [
            r
            for e in found.get("entry", [])
            for r in [e["resource"]]
            if r.get("resourceType") == "Bundle"
            for r in [r["entry"][0]["resource"]]
            if any(i.get("value") == sid for i in r.get("identifier", []))
        ]
        done = [r for r in mine if r.get("outcome") != "queued"]
        if done:
            return max(
                done,
                key=lambda r: datetime.fromisoformat(
                    r["created"].replace("Z", "+00:00")
                ),
            )
        time.sleep(1)
    raise SystemExit(f"PAS never decided {sid}")


def seed(fixtures: dict, start: datetime, inbox: Path, log: list) -> dict[int, dict]:
    ehr = _ehr()
    token = ehr.client_token("seeder")
    fhir = ehr.fhir_url
    inbox.mkdir(parents=True, exist_ok=True)
    notes: dict[int, dict] = {}
    control = 0

    def write_x12(name: str, kind: str, body: list[str]) -> None:
        nonlocal control
        control += 1
        (inbox / name).write_text(fx.interchange(kind, [body], control=str(control)))

    for u in fixtures["units"]:
        fault, n, sid = u["fault"], u["n"], u["submission_id"]
        pa = u["unit"] == "prior_authorization"
        created = _iso(start if fault == "in_flight" else start - timedelta(days=8))
        patient = _http("POST", f"{fhir}/Patient", _patient(u, u["member_id"]), token)
        pref = f"Patient/{patient['id']}"
        use = "preauthorization" if pa else "claim"
        work = [(sid, u["service_code"], u["service_date"])]
        if fault == "lookalike":
            work.append((f"{sid}-LA", u["near_code"], _next_day(u["service_date"])))
        note: dict[str, Any] = {"decisions": []}
        for w_sid, code, day in work:
            ehr_claim = _http(
                "POST",
                f"{fhir}/Claim",
                _claim(
                    u,
                    sid=w_sid,
                    code=code,
                    day=day,
                    created=created,
                    patient_ref=pref,
                    use=use,
                ),
                token,
            )
            if fault == "in_flight":
                continue
            # What each other system received.
            x_member, x_code, x_day = u["member_id"], code, day
            p_code, p_day = code, day
            if w_sid == sid:
                if fault == "code_clearinghouse":
                    x_code = u["near_code"]
                if fault == "date_clearinghouse":
                    x_day = _next_day(day)
                if fault == "member_clearinghouse":
                    x_member = f"SYNM9{n:04d}"
                if fault == "code_payer":
                    p_code = u["near_code"]
                if fault == "date_payer":
                    p_day = _next_day(day)
            if not pa:
                if fault == "drop_clearinghouse" and w_sid == sid:
                    continue
                status = fx.ClaimStatus(
                    w_sid, x_member, x_code, x_day, payer_claim_number=f"PCN{n:05d}"
                )
                write_x12(f"c{n:03d}-{w_sid}.x12", "277", fx.status_set([status]))
                if fault == "duplicate_clearinghouse":
                    again = fx.ClaimStatus(
                        f"{w_sid}-DUP",
                        x_member,
                        x_code,
                        x_day,
                        payer_claim_number=f"PCN{n:05d}D",
                    )
                    write_x12(
                        f"c{n:03d}-{w_sid}-dup.x12", "277", fx.status_set([again])
                    )
                continue
            if fault == "drop_payer" and w_sid == sid:
                continue
            reply = pas_submit(u, sid=w_sid, code=p_code, day=p_day, created=created)
            reply = pas_decision(u["member_id"], w_sid, reply)
            outcome, auth = reply.get("outcome"), reply.get("preAuthRef")
            disposition = reply.get("disposition")
            note["decisions"].append(outcome)
            log.append(
                {
                    "n": n,
                    "sid": w_sid,
                    "payer_outcome": outcome,
                    "payer_claim_response": reply.get("id"),
                    "payer_followup": reply.get("_followup"),
                }
            )
            if fault == "duplicate_payer" and w_sid == sid:
                pas_submit(
                    u, sid=f"{w_sid}-DUP", code=p_code, day=p_day, created=created
                )
            if not (fault == "drop_clearinghouse" and w_sid == sid):
                review = fx.Review(
                    w_sid,
                    x_member,
                    x_code,
                    x_day,
                    decision=HCR[disposition],
                    auth_number=auth,
                )
                name = f"p{n:03d}-{w_sid}.x12"
                write_x12(name, "278", fx.review_set(review))
                if fault == "duplicate_clearinghouse" and w_sid == sid:
                    (inbox / f"p{n:03d}-{w_sid}-redelivered.x12").write_bytes(
                        (inbox / name).read_bytes()
                    )
            if fault == "missed_writeback" and w_sid == sid:
                continue
            if fault == "wrong_writeback" and w_sid == sid:
                auth = f"{auth}-X"
            _http(
                "POST",
                f"{fhir}/ClaimResponse",
                {
                    "resourceType": "ClaimResponse",
                    "identifier": [{"system": SUBMISSION, "value": w_sid}],
                    "status": "active",
                    "type": ehr_claim["type"],
                    "use": "preauthorization",
                    "patient": {"reference": pref},
                    "created": _iso(datetime.now(timezone.utc)),
                    "insurer": {"display": "Synthetic payer"},
                    "request": {"reference": f"Claim/{ehr_claim['id']}"},
                    "outcome": outcome,
                    "disposition": disposition,
                    "preAuthRef": auth,
                },
                token,
            )
        notes[n] = note
    return notes


# -- reconcile and score ---------------------------------------------------------


def score(u: dict, result: dict) -> dict:
    expected = {tuple(b) for b in u["expected"]}
    got = {(b["kind"], b["leg"], b["outcome"]) for b in result["breaks"]}
    caught = expected & got
    refuted_expected = {b for b in expected if b[2] == "REFUTED"}
    false = {b for b in got - expected if b[2] == "REFUTED"}
    # Reported, not scored: an UNCERTAIN break nobody planted (a leg that
    # depends on a dropped record, for example).
    extra_uncertain = {b for b in got - expected if b[2] == "UNCERTAIN"}
    return {
        "expected": sorted(expected),
        "got": sorted(got),
        "caught": sorted(caught),
        "missed": sorted(expected - got),
        "false_breaks": sorted(false),
        "extra_uncertain_breaks": sorted(extra_uncertain),
        "planted_refuted": len(refuted_expected),
        "planted_refuted_caught": len(refuted_expected & got),
    }


def run(out: Path, only: Optional[set[int]] = None) -> None:
    sys.path.insert(0, str(FLOW))
    from openadapt_flow.reconcile import ReconcileSpec, reconcile

    body = (HERE / "fixtures.json").read_text()
    want = (HERE / "fixtures.json.sha256").read_text().split()[0]
    got = hashlib.sha256(body.encode()).hexdigest()
    if got != want:
        raise SystemExit(f"fixtures.json sha256 {got} != pre-registered {want}")
    fixtures = json.loads(body)
    if only:
        # A smoke run on a few units: its summary says so, and it is never
        # the measured run.
        fixtures["units"] = [u for u in fixtures["units"] if u["n"] in only]
    out.mkdir(parents=True, exist_ok=True)
    start = datetime.now(timezone.utc).replace(microsecond=0)
    inbox = out / "x12-inbox"
    ehr = _ehr()
    log: list = []
    t0 = time.monotonic()
    notes = seed(fixtures, start, inbox, log)
    seed_s = time.monotonic() - t0
    os.environ.update(
        {
            "RECON_EHR_FHIR": ehr.fhir_url,
            "RECON_EHR_TOKEN": ehr.client_token("reconciler"),
            "RECON_PAYER_FHIR": pas_url(),
            "RECON_X12_INBOX": str(inbox),
        }
    )
    specs = {
        "prior_authorization": ReconcileSpec.load(HERE / "prior_auth.reconcile.yaml"),
        "claim": ReconcileSpec.load(HERE / "claim.reconcile.yaml"),
    }
    now = start + timedelta(seconds=fixtures["evaluated_at_offset_s"])
    rows = []
    for u in fixtures["units"]:
        t = time.monotonic()
        result = reconcile(
            specs[u["unit"]],
            {
                "submission_id": u["submission_id"],
                "submission_ref": f"{SUBMISSION}|{u['submission_id']}",
            },
            now=now,
        )
        elapsed = time.monotonic() - t
        body = result.to_dict()
        rows.append(
            {
                "n": u["n"],
                "unit": u["unit"],
                "category": u["category"],
                "fault": u["fault"],
                "outcome": body["outcome"],
                "seconds": round(elapsed, 3),
                "payer_decisions": notes.get(u["n"], {}).get("decisions", []),
                **score(u, body),
                "result": body,
            }
        )
    summary = {
        "smoke_units": sorted(only) if only else None,
        "fixtures_sha256": want,
        "pas_commit": PAS_COMMIT,
        "medplum": medplum.MedplumFixture.__module__
        and json.loads(
            (FLOW / "benchmark/fhir_medplum/environment.lock.json").read_text()
        )["services"]["server"],
        "started_at": _iso(start),
        "evaluated_at": _iso(now),
        "seed_seconds": round(seed_s, 1),
        "units": len(rows),
        "planted_refuted": sum(r["planted_refuted"] for r in rows),
        "planted_refuted_caught": sum(r["planted_refuted_caught"] for r in rows),
        "expected_breaks": sum(len(r["expected"]) for r in rows),
        "expected_breaks_caught": sum(len(r["caught"]) for r in rows),
        "false_breaks": sum(len(r["false_breaks"]) for r in rows),
        "units_with_false_breaks": [r["n"] for r in rows if r["false_breaks"]],
        "faulty_units": sum(r["category"] == "fault" for r in rows),
        # The headline miss: a unit with a planted fault reported as agreeing.
        "faulty_units_confirmed": [
            r["n"]
            for r in rows
            if r["category"] == "fault" and r["outcome"] == "CONFIRMED"
        ],
        "faulty_units_refuted": sum(
            r["category"] == "fault" and r["outcome"] == "REFUTED" for r in rows
        ),
        "extra_uncertain_breaks": sum(len(r["extra_uncertain_breaks"]) for r in rows),
        "clean_units": sum(r["category"] in ("clean", "lookalike") for r in rows),
        "clean_units_confirmed": sum(
            r["category"] in ("clean", "lookalike") and r["outcome"] == "CONFIRMED"
            for r in rows
        ),
        "in_flight_units": sum(r["category"] == "in_flight" for r in rows),
        "in_flight_uncertain": sum(
            r["category"] == "in_flight" and r["outcome"] == "UNCERTAIN" for r in rows
        ),
        "median_reconcile_s": sorted(r["seconds"] for r in rows)[len(rows) // 2],
    }
    (out / "results.json").write_text(json.dumps(rows, indent=1) + "\n")
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    (out / "seed_log.json").write_text(json.dumps(log, indent=1) + "\n")
    print(json.dumps(summary, indent=1))


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build-pas", "up", "run", "down"])
    parser.add_argument("--out", type=Path, default=STATE / "run")
    parser.add_argument(
        "--only", default="", help="smoke run: comma-separated unit numbers"
    )
    args = parser.parse_args(argv)
    {"build-pas": build_pas, "up": up, "down": down}.get(
        args.command,
        lambda: run(args.out, {int(n) for n in args.only.split(",") if n.strip()}),
    )()


if __name__ == "__main__":
    main()
