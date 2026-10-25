"""The record duplicate check against live OpenEMR: search semantics and paging.

    python -m benchmark.dup_check.live --out DIR

Needs the fixture lease and ``OPENEMR_FIXTURE_STATE`` / ``OPENEMR_UI_PORT`` /
``OPENEMR_API_PORT``. No model calls. Resets the fixture first, then seeds 23
synthetic charts through OpenEMR's REST API, 13 of them born on one day,
so a small page size forces several pages.

It records what OpenEMR's channels actually do (``probe.json``):

- REST ``/api/patient``: whether ``_limit`` caps a page, whether a reply
  carries ``links.next`` or a total, the body's keys, and whether ``lname``
  matches exactly or by prefix;
- FHIR ``Patient``: whether ``_count`` pages with ``Bundle.link[next]`` and
  ``Bundle.total``, whether ``family`` matches by prefix, and whether a stored
  birth name comes back as an old name;

and then runs ``check_duplicate`` through each channel on six referrals with
known answers (``decisions.json``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from openadapt_flow.runtime.checks.duplicate_record import (  # noqa: E402
    FhirPatientSource,
    RestPatientSource,
    check_duplicate,
)

DOB = "1977-05-14"
SCOPE = "openid api:oemr api:fhir user/patient.crus user/Patient.read user/Patient.rs"


def _people() -> list[dict[str, str]]:
    base = {
        "title": "Ms.",
        "sex": "Female",
        "street": "1 PROBE ROAD",
        "city": "EXAMPLETON",
        "state": "MA",
        "postal_code": "02139",
        "country_code": "USA",
    }
    out = []
    for i in range(13):  # one shared birth day: forces pages at a small size
        out.append(
            {
                **base,
                "fname": f"Shared{i}",
                "lname": f"Dayborn{i}",
                "DOB": DOB,
                "phone_home": f"555-06{i:02d}",
                "email": f"shared.{i}@example.invalid",
            }
        )
    for i in range(6):  # one last-name stem: prefix or exact matching
        out.append(
            {
                **base,
                "fname": f"Stem{i}",
                "lname": f"Hartwell{'' if i == 0 else i}",
                "DOB": f"1960-01-{i + 10:02d}",
                "phone_home": f"555-07{i:02d}",
                "email": f"stem.{i}@example.invalid",
            }
        )
    out.append(
        {
            **base,
            "fname": "Rosalind",
            "lname": "Okafor",
            "DOB": "1990-04-09",
            "phone_home": "617-555-0142",
            "email": "r.okafor@example.invalid",
        }
    )
    out.append(
        {
            **base,
            "fname": "Robert",
            "lname": "Ashby",
            "DOB": "1950-02-03",
            "phone_home": "555-0800",
            "email": "r.ashby@example.invalid",
        }
    )
    out.append(
        {
            **base,
            "fname": "Ada",
            "lname": "Quill",
            "DOB": "1984-03-07",
            "phone_home": "555-0801",
            "email": "a.quill@example.invalid",
        }
    )
    out.append(
        {
            **base,
            "fname": "Mabel",
            "lname": "Ortiz",
            "DOB": "1990-01-21",
            "phone_home": "555-0802",
            "email": "m.ortiz@example.invalid",
        }
    )
    return out


#: (referral, expected decision)
REFERRALS = [
    ({"fname": "Ada", "lname": "Quill", "DOB": "1984-03-07"}, "exact"),
    (
        {"fname": "Rosalind", "lname": "Jesswood", "DOB": "1990-04-09"},
        "close",
    ),  # married
    ({"fname": "Bob", "lname": "Ashby", "DOB": "1950-02-03"}, "close"),  # nickname
    (
        {"fname": "Ada", "lname": "Quill", "DOB": "1984-07-03"},
        "close",
    ),  # swapped day/month
    ({"fname": "Nadia", "lname": "Fresh", "DOB": DOB}, "none"),  # shares only the day
    ({"fname": "Tomas", "lname": "Hartwell", "DOB": "1999-12-01"}, "none"),  # namesake
]


def main() -> int:
    from verification_coverage.independent import live  # the fixture helpers

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    fx = live.fx()
    live.reset()
    clients = json.loads(fx.clients_path.read_text())
    live.sql(
        f"UPDATE openemr.oauth_clients SET scope = {live.q(SCOPE)} "
        f"WHERE client_id = {live.q(clients['actor_client_id'])};"
    )
    import httpx

    values = fx._runtime_values()
    session = httpx.Client(verify=False, timeout=30)
    resp = session.post(
        f"{fx.api_base_url}/oauth2/default/token",
        data={
            "grant_type": "password",
            "client_id": clients["actor_client_id"],
            "scope": SCOPE,
            "user_role": "users",
            "username": values["OPENEMR_ACTOR_USER"],
            "password": values["OPENEMR_ACTOR_PASSWORD"],
        },
    )
    if resp.status_code != 200:
        raise SystemExit(f"token: HTTP {resp.status_code}")
    token = resp.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    api = f"{fx.api_base_url}/apis/default/api"
    fhir = f"{fx.api_base_url}/apis/default/fhir"
    for person in _people():
        r = session.post(f"{api}/patient", json=person, headers=auth)
        if r.status_code not in (200, 201):
            raise SystemExit(f"seed: HTTP {r.status_code} {r.text[:200]}")
    # A married chart keeps her birth name, as OpenEMR's demographics allow.
    live.sql(
        "UPDATE openemr.patient_data SET birth_fname = 'Rosalind', "
        "birth_lname = 'Jesswood' WHERE email = 'r.okafor@example.invalid'"
    )

    probe: dict[str, Any] = {}

    def rest(params: dict[str, Any]) -> dict[str, Any]:
        r = session.get(f"{api}/patient", params=params, headers=auth)
        body = (
            r.json()
            if r.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        data = body.get("data") if isinstance(body, dict) else None
        return {
            "status": r.status_code,
            "keys": sorted(body) if isinstance(body, dict) else type(body).__name__,
            "rows": len(data) if isinstance(data, list) else None,
            "links": body.get("links") if isinstance(body, dict) else None,
            "lnames": sorted({d.get("lname") for d in data or []})
            if isinstance(data, list)
            else None,
        }

    probe["rest_dob_all"] = rest({"DOB": DOB})
    probe["rest_dob_limit5"] = rest({"DOB": DOB, "_limit": 5})
    probe["rest_dob_limit5_offset5"] = rest({"DOB": DOB, "_limit": 5, "_offset": 5})
    probe["rest_lname_exact"] = rest({"lname": "Hartwell"})
    probe["rest_no_match"] = rest({"DOB": "1801-01-01"})

    def fhir_search(params: dict[str, Any]) -> dict[str, Any]:
        pages, total, ids, url, q = 0, None, [], f"{fhir}/Patient", dict(params)
        links_seen = []
        while url and pages < 20:
            r = session.get(
                url, params=q, headers={**auth, "Accept": "application/fhir+json"}
            )
            pages += 1
            if r.status_code != 200:
                return {"status": r.status_code, "text": r.text[:200]}
            b = r.json()
            total = b.get("total", total)
            ids += [e["resource"]["id"] for e in b.get("entry", []) or []]
            nxt = [
                link["url"]
                for link in b.get("link", [])
                if link.get("relation") == "next"
            ]
            links_seen.append([link.get("relation") for link in b.get("link", [])])
            url, q = (nxt[0] if nxt else None), None
        return {
            "pages": pages,
            "total": total,
            "read": len(ids),
            "unique": len(set(ids)),
            "link_relations": links_seen[:3],
        }

    probe["fhir_birthdate"] = fhir_search({"birthdate": DOB})
    probe["fhir_birthdate_count5"] = fhir_search({"birthdate": DOB, "_count": 5})
    probe["fhir_family_prefix"] = fhir_search({"family": "Hartwell"})
    r = session.get(
        f"{fhir}/Patient",
        params={"family": "Jesswood"},
        headers={**auth, "Accept": "application/fhir+json"},
    )
    b = r.json() if r.status_code == 200 else {}
    probe["fhir_family_birth_name"] = {
        "status": r.status_code,
        "total": b.get("total"),
        "names": [e["resource"].get("name") for e in b.get("entry", []) or []][:2],
    }
    (args.out / "probe.json").write_text(
        json.dumps(probe, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(probe, indent=1, sort_keys=True)[:4000])

    decisions = []
    channels = {
        "fhir_page5": lambda: FhirPatientSource(
            fhir,
            token=lambda: token,
            verify_tls=False,
            session=httpx.Client(verify=False, timeout=30),
        ),
        "rest_page5": lambda: RestPatientSource(
            fx.api_base_url,
            headers=auth,
            page_size=5,
            session=httpx.Client(verify=False, timeout=30),
        ),
        "rest_page500": lambda: RestPatientSource(
            fx.api_base_url,
            headers=auth,
            session=httpx.Client(verify=False, timeout=30),
        ),
    }
    for name, make in channels.items():
        for want, expected in REFERRALS:
            result = check_duplicate(make(), want)
            decisions.append(
                {
                    "channel": name,
                    "referral": want,
                    "expected": expected,
                    "decision": result.decision,
                    "reason": result.reason,
                    "pages": result.pages,
                    "candidates": [c.why for c in result.candidates],
                }
            )
            print(
                name,
                want["fname"],
                want["lname"],
                want["DOB"],
                "expected",
                expected,
                "->",
                result.decision,
                "|",
                result.reason[:90],
            )
    (args.out / "decisions.json").write_text(json.dumps(decisions, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
