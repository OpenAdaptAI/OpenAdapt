"""False stops and missed duplicates through live OpenEMR FHIR, on the soak's stream.

    python -m benchmark.dup_check.live_rates --out DIR [--long-items 500]

The offline measurement (``measure.py``) reads an in-memory table with FHIR
search semantics. This one asks a real OpenEMR 8.0.0.3: it resets the pinned
fixture, creates the existing charts through OpenEMR's REST API (and stores
each planted chart's birth name the way OpenEMR's demographics do), then for
every item in the planted seed-7 stream runs ``check_duplicate`` through
``FhirPatientSource`` and, when the right outcome is a new chart, creates it.
So the record check meets the same charts as offline, served by OpenEMR.

Needs the fixture lease and ``OPENEMR_FIXTURE_STATE`` / ``OPENEMR_UI_PORT`` /
``OPENEMR_API_PORT``. No model calls. Writes ``rows.json`` (per item, private
evidence) and ``summary.json`` to ``--out``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from benchmark.dup_check import measure  # noqa: E402
from openadapt_flow.runtime.checks.duplicate_record import (  # noqa: E402
    FhirPatientSource,
    check_duplicate,
)

SCOPE = "openid api:oemr api:fhir user/patient.crus user/Patient.read user/Patient.rs"
API_FIELDS = (
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


def main() -> int:
    from verification_coverage.independent import live  # the fixture helpers

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--long-items", type=int, default=500)
    parser.add_argument("--budget-s", type=float, default=3000.0)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    import httpx

    fx = live.fx()
    live.reset()
    clients = json.loads(fx.clients_path.read_text())
    live.sql(
        f"UPDATE openemr.oauth_clients SET scope = {live.q(SCOPE)} "
        f"WHERE client_id = {live.q(clients['actor_client_id'])};"
    )
    values = fx._runtime_values()
    session = httpx.Client(verify=False, timeout=30)
    tokens: dict[str, Any] = {}

    def token() -> str:
        # Tokens live an hour; take a fresh one every 30 minutes.
        if not tokens or time.monotonic() - tokens["at"] > 1800:
            r = session.post(
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
            r.raise_for_status()
            tokens.update(value=r.json()["access_token"], at=time.monotonic())
        return str(tokens["value"])

    def create(person: dict[str, str]) -> None:
        body = {k: str(person.get(k, "") or "") for k in API_FIELDS}
        body["DOB"] = measure.iso(body["DOB"])
        body["state"] = "MA"
        r = session.post(
            f"{fx.api_base_url}/apis/default/api/patient",
            json=body,
            headers={"Authorization": f"Bearer {token()}"},
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"create: HTTP {r.status_code} {r.text[:200]}")
        if person.get("prior_lname"):
            live.sql(
                "UPDATE openemr.patient_data SET birth_fname = "
                f"{live.q(person['fname'])}, birth_lname = {live.q(person['prior_lname'])} "
                f"WHERE email = {live.q(person['email'])}"
            )

    existing, items = measure.from_generator(7, args.long_items, 10)
    extra, extra_items = measure.extra_existing(existing)
    stream = measure.plant(existing, items + extra_items, 7)
    for person in existing + extra:
        create(person)
    source = FhirPatientSource(
        f"{fx.api_base_url}/apis/default/fhir",
        token=token,
        session=httpx.Client(verify=False, timeout=30),
    )
    rows = []
    for item in stream:
        if time.monotonic() - started > args.budget_s:
            raise SystemExit(
                f"stopped at the {args.budget_s:.0f} s budget after {len(rows)} items"
            )
        for person in item.before:
            create(person)
        params = {
            k: (v.strip() if isinstance(v, str) else v) for k, v in item.params.items()
        }
        params["DOB"] = measure.iso(params.get("DOB", ""))
        result = check_duplicate(source, params)
        is_dup = item.exception in measure.DUPLICATE_KINDS
        rows.append(
            {
                "item": item.item_id,
                "exception": item.exception,
                "right": "new chart"
                if item.intended is not None
                else ("no chart: duplicate" if is_dup else "no chart"),
                "record": result.decision,
                "record_why": [c.why for c in result.candidates],
                "reason": result.reason if result.decision == "unreadable" else "",
            }
        )
        if item.intended is not None:
            create(item.intended)
    summary = measure.summarize([{**r, "screen": "proceed"} for r in rows])
    summary.pop("screen", None)
    summary["unreadable_reasons"] = sorted({r["reason"] for r in rows if r["reason"]})
    summary["seconds"] = round(time.monotonic() - started)
    (args.out / "rows.json").write_text(json.dumps(rows, indent=1) + "\n")
    (args.out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
