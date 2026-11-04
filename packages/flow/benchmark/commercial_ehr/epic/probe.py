"""Check, on Epic's public FHIR sandbox, every read-path fact the contract uses.

    set -a; source /path/to/.env; set +a
    PYTHONPATH=. python -m benchmark.commercial_ehr.epic.probe --out probe.json

Read-only checks first, against one of Epic's published sandbox patients;
then two synthetic patients and one allergy, written with the app's backend
credential, to see what Epic does with a write. The output is one JSON
object of named facts, without values or credentials; FINDINGS.md says what
each one means for the coverage claim. About 25 calls.
"""

from __future__ import annotations

import argparse
import json
import random
import string
from typing import Any

from benchmark.commercial_ehr.epic.client import EpicClient
from benchmark.commercial_ehr.epic.suite import SSN_SYSTEM, _ssn

#: One of Epic's published sandbox patients (fhir.epic.com, "Sandbox Test Data").
KNOWN = {"family": "Lopez", "given": "Camila", "birthdate": "1987-09-12"}


def _entries(body: Any, mode: str | None = None) -> list[dict[str, Any]]:
    out = []
    for e in (body or {}).get("entry") or []:
        if mode is None or (e.get("search") or {}).get("mode") == mode:
            out.append(e.get("resource") or {})
    return out


def _types(body: Any) -> list[str]:
    return sorted(
        f"{(e.get('search') or {}).get('mode')}:{(e.get('resource') or {}).get('resourceType')}"
        for e in (body or {}).get("entry") or []
    )


def read_only(c: EpicClient) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    status, headers, _ = c.get("metadata", {"_summary": "true"})
    facts["metadata_date_header"] = status == 200 and bool(headers.get("date"))
    _, _, body = c.get("Patient", KNOWN)
    pid = _entries(body, "match")[0]["id"]
    facts["patient_meta_returned"] = bool(_entries(body, "match")[0].get("meta"))
    _, _, body = c.get("Patient", {"family": KNOWN["family"]})
    facts["patient_search_one_value_matches"] = len(_entries(body, "match"))
    facts["empty_search_outcome_severity"] = sorted(
        {i.get("severity") for r in _entries(body, "outcome") for i in r.get("issue", [])}
    )
    facts["patient_history_status"] = c.get(f"Patient/{pid}/_history")[0]
    facts["auditevent_search_status"] = c.get("AuditEvent", {"date": "ge2026-01-01"})[0]
    status, _, body = c.get("Patient", {"_lastUpdated": "ge2026-01-01"})
    facts["patient_lastupdated_status"] = status
    _, _, body = c.get(
        "Observation",
        {"patient": pid, "category": "vital-signs", "_lastUpdated": "ge2099-01-01"},
    )
    # A filter in the future must return nothing; any match means Epic
    # ignored the parameter.
    facts["observation_lastupdated_future_matches"] = len(_entries(body, "match"))
    _, _, body = c.get("Patient", {"_id": pid, "_revinclude": "Provenance:target"})
    facts["revinclude_provenance"] = _types(body)
    _, _, body = c.get("Patient", {"_id": pid, "_revinclude": "AuditEvent:entity"})
    facts["revinclude_auditevent"] = _types(body)
    return facts


def writes(c: EpicClient) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    family = "Oaprobe" + "".join(random.choice(string.ascii_lowercase) for _ in range(7))
    body = {
        "resourceType": "Patient",
        "name": [{"use": "usual", "family": family, "given": ["Ada"]}],
        "gender": "female",
        "birthDate": "1971-03-14",
        "telecom": [
            {"system": "phone", "value": "617-555-0142", "use": "home"},
            {"system": "email", "value": "probe@example.invalid", "use": "home"},
        ],
    }
    facts["create_without_ssn_status"] = c.post("Patient", body)[0]
    body["identifier"] = [{"use": "usual", "system": SSN_SYSTEM, "value": _ssn()}]
    status, headers, _ = c.post("Patient", body)
    facts["create_status"] = status
    loc = headers.get("location", "")
    _, _, stored = c.get(loc)
    systems = sorted(t.get("system") for t in stored.get("telecom", []))
    facts["telecom_systems_stored"] = systems  # sent: email and phone
    facts["name_entries_stored"] = sorted(n.get("use") for n in stored.get("name", []))
    facts["elements_added_by_epic"] = sorted(
        set(stored) - set(body) - {"id", "meta"}
    )
    facts["meta_security_added"] = sorted(
        s.get("code") for s in (stored.get("meta") or {}).get("security", [])
    )
    _, _, found = c.get(
        "Patient",
        {"family": family, "birthdate": "1971-03-14", "_revinclude": "Provenance:target"},
    )
    provs = _entries(found, "include")
    facts["created_patient_provenance"] = [
        {
            "recorded_present": bool(p.get("recorded")),
            "agents": sorted(
                (a.get("type") or {}).get("text") or "" for a in p.get("agent", [])
            ),
        }
        for p in provs
    ]
    body["identifier"] = [{"use": "usual", "system": SSN_SYSTEM, "value": _ssn()}]
    body["address"] = [{"use": "home", "line": ["9 Other Road"], "city": "Boston"}]
    status, headers, _ = c.post("Patient", body)
    facts["same_person_new_ssn_create"] = {
        "status": status,
        "same_location": headers.get("location") == loc,
    }
    allergy = {
        "resourceType": "AllergyIntolerance",
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical", "code": "active"}]},
        "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification", "code": "confirmed"}]},
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": "91936005"}], "text": "Penicillin"},
        "patient": {"reference": loc},
    }
    facts["allergy_confirmed_status"] = c.post("AllergyIntolerance", allergy)[0]
    allergy["verificationStatus"]["coding"][0]["code"] = "unconfirmed"
    status, headers, _ = c.post("AllergyIntolerance", allergy)
    facts["allergy_unconfirmed_status"] = status
    aloc = headers.get("location", "")
    facts["allergy_read_by_location"] = c.get(aloc)[0] if aloc else None
    _, _, listed = c.get("AllergyIntolerance", {"patient": loc.split("/")[-1]})
    facts["allergy_search_codes"] = sorted(
        ((r.get("code") or {}).get("coding") or [{}])[0].get("code", "")
        for r in _entries(listed, "match")
    )  # 91936005 if the created allergy is listed
    facts["allergy_search_lists_created"] = any(
        r.get("id") == aloc.split("/")[-1] for r in _entries(listed, "match")
    )
    _, _, body = c.get("Observation", {"patient": loc.split("/")[-1], "category": "vital-signs"})
    status, _, out = c.post(
        "Observation",
        {
            "resourceType": "Observation",
            "status": "final",
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs"}]}],
            "code": {"coding": [{"system": "http://loinc.org", "code": "29463-7"}]},
            "subject": {"reference": loc},
            "effectiveDateTime": "2026-10-29T12:00:00Z",
            "valueQuantity": {"value": 61.2, "unit": "kg", "system": "http://unitsofmeasure.org", "code": "kg"},
        },
    )
    facts["observation_create_without_encounter"] = {
        "status": status,
        "missing": [x for i in (out or {}).get("issue", []) for x in i.get("expression", [])],
    }
    return facts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--read-only", action="store_true")
    args = ap.parse_args(argv)
    c = EpicClient()
    c.token()
    facts: dict[str, Any] = {"granted_scopes": sorted(c.scope.split())}
    facts.update(read_only(c))
    if not args.read_only:
        facts.update(writes(c))
    facts["sandbox_calls"] = c.calls
    with open(args.out, "w") as fh:
        json.dump(facts, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print(json.dumps(facts, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
