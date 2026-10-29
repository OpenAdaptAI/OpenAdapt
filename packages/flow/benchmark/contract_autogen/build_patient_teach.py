"""Rebuild the Medplum Patient teach directory from #1640's recorded demonstrations.

    python -m benchmark.contract_autogen.build_patient_teach

#1640 kept each demonstration's before and after reads only as #1520's
channel flattened them: a list kept its first element, so ``telecom.value``
is the phone and the email the demonstration typed is gone. This script
rebuilds each record in the shape Medplum's new-patient form stores it
(one name, one address, a phone ContactPoint and, on the created record,
an email ContactPoint after it) and writes a teach directory for
``openadapt_flow.generate.ensure_contract``. The search parameters are the
FHIR R4 base definitions of the parameters Medplum lists for Patient. The
server facts are #1640's probe (``readpath/probe.json``). A live capture
(``capture.py``) replaces this reconstruction when the fixture is up.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "fhir_medplum" / "readpath"
OUT = HERE / "teach" / "medplum_patient_rebuilt"
READ = "ch-40a6b8bfa2e1"

R4_PATIENT_SEARCH = [
    ("given", "string", "Patient.name.given | Practitioner.name.given"),
    ("family", "string", "Patient.name.family | Practitioner.name.family"),
    ("name", "string", "Patient.name"),
    ("birthdate", "date", "Patient.birthDate | Person.birthDate | RelatedPerson.birthDate"),
    ("gender", "token", "Patient.gender | Person.gender | Practitioner.gender | RelatedPerson.gender"),
    ("telecom", "token", "Patient.telecom | Person.telecom | Practitioner.telecom | PractitionerRole.telecom | RelatedPerson.telecom"),
    ("email", "token", "Patient.telecom.where(system='email') | Person.telecom.where(system='email') | Practitioner.telecom.where(system='email') | PractitionerRole.telecom.where(system='email') | RelatedPerson.telecom.where(system='email')"),
    ("phone", "token", "Patient.telecom.where(system='phone') | Person.telecom.where(system='phone') | Practitioner.telecom.where(system='phone') | PractitionerRole.telecom.where(system='phone') | RelatedPerson.telecom.where(system='phone')"),
    ("address", "string", "Patient.address | Person.address | Practitioner.address | RelatedPerson.address"),
    ("address-city", "string", "Patient.address.city | Person.address.city | Practitioner.address.city | RelatedPerson.address.city"),
    ("address-state", "string", "Patient.address.state | Person.address.state | Practitioner.address.state | RelatedPerson.address.state"),
    ("address-postalcode", "string", "Patient.address.postalCode | Person.address.postalCode | Practitioner.address.postalCode | RelatedPerson.address.postalCode"),
    ("identifier", "token", "Patient.identifier"),
]  # fmt: skip


def rebuild(flat: dict, email: str | None) -> dict:
    telecom = [{"system": flat["telecom.system"], "value": flat["telecom.value"]}]
    if email:
        telecom.append({"system": "email", "value": email})
    return {
        "resourceType": "Patient",
        "id": flat["id"],
        "meta": {"versionId": flat["meta.versionId"]},
        "name": [{"given": [flat["name.given"]], "family": flat["name.family"]}],
        "birthDate": flat["birthDate"],
        "gender": flat["gender"],
        "telecom": telecom,
        "address": [
            {
                "line": [flat["address.line"]],
                "city": flat["address.city"],
                "state": flat["address.state"],
                "postalCode": flat["address.postalCode"],
            }
        ],
    }


def main() -> None:
    (OUT / "demos").mkdir(parents=True, exist_ok=True)
    for demo in sorted((SRC / "generated" / "demos").iterdir()):
        params = json.loads((demo / "params.json").read_text())
        before = json.loads((demo / "reads" / f"{READ}.before.json").read_text())
        after = json.loads((demo / "reads" / f"{READ}.after.json").read_text())
        old = {r["id"] for r in before}
        out = OUT / "demos" / demo.name
        out.mkdir(exist_ok=True)
        (out / "params.json").write_text(json.dumps(params, indent=1) + "\n")
        (out / "before.json").write_text(
            json.dumps([rebuild(r, None) for r in before], indent=1) + "\n"
        )
        (out / "after.json").write_text(
            json.dumps(
                [
                    rebuild(r, None if r["id"] in old else params["email"])
                    for r in after
                ],
                indent=1,
            )
            + "\n"
        )
    capability = {
        "resourceType": "CapabilityStatement",
        "rest": [
            {
                "mode": "server",
                "resource": [
                    {
                        "type": "Patient",
                        "interaction": [{"code": "search-type"}, {"code": "create"}],
                        "searchParam": [
                            {"name": n, "type": t} for n, t, _ in R4_PATIENT_SEARCH
                        ],
                    }
                ],
            }
        ],
    }
    params_bundle = {
        "resourceType": "Bundle",
        "entry": [
            {
                "resource": {
                    "resourceType": "SearchParameter",
                    "code": n,
                    "type": t,
                    "expression": e,
                    "base": sorted({p.strip().split(".")[0] for p in e.split("|")}),
                }
            }
            for n, t, e in R4_PATIENT_SEARCH
        ],
    }
    (OUT / "capability.json").write_text(json.dumps(capability, indent=1) + "\n")
    (OUT / "search_parameters.json").write_text(
        json.dumps(params_bundle, indent=1) + "\n"
    )
    (OUT / "facts.json").write_text((SRC / "probe.json").read_text())
    teach = {
        "resource_type": "Patient",
        "deployment": {
            "base_url": "${MEDPLUM_FHIR_URL}",
            "access_token_env": "MEDPLUM_RECORD_TOKEN",
            "self": ["${MEDPLUM_BOT_REF}"],
        },
    }
    (OUT / "teach.json").write_text(json.dumps(teach, indent=1) + "\n")


if __name__ == "__main__":
    main()
