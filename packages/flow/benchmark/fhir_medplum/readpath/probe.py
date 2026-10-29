"""Check, on live Medplum, every read-path fact the ensure-record contract uses.

    PYTHONPATH=. python -m benchmark.fhir_medplum.readpath.probe --out probe.json

Each check writes synthetic resources as the bot or the other user and reads
them back with the record-check client, the verifier's own credential. The
output is one JSON object of named facts; PROBES.md says what each one means
for the coverage claim. Run it on a fixture with no access policy on the bot
first (``unrestricted``), then it applies the policy and checks again.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from typing import Any, Optional

from benchmark.fhir_medplum.fixture import FixtureError, MedplumFixture, _http
from benchmark.fhir_medplum.readpath.suite import Actors, audit_is_read_only


def _since() -> str:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1))
    time.sleep(1.1)
    return stamp


CLINICAL = ("Patient/", "Observation/", "AuditEvent/")


def _writes(fx: MedplumFixture, token: str, since: str) -> list[dict[str, Any]]:
    """The window's write events on clinical resources: action, who, what.

    Signing in writes Login and ProjectMembership events too (by ``system``
    or the administrator); they are left out here, and the verifier counts
    them as another user's writes elsewhere.
    """

    bundle = _http(
        "GET", f"{fx.fhir_url}/AuditEvent?date=ge{since}&_count=200", token=token
    )
    out = []
    for entry in bundle.get("entry") or []:
        event = entry["resource"]
        what = [
            e.get("what", {}).get("reference") or "" for e in event.get("entity", [])
        ]
        if event.get("action") in ("C", "U", "D") and any(
            w.startswith(CLINICAL) for w in what
        ):
            out.append(
                {
                    "action": event["action"],
                    "who": [
                        a.get("who", {}).get("reference")
                        for a in event.get("agent", [])
                    ],
                    "what": what,
                }
            )
    return out


def _audit_lag(
    fx: MedplumFixture, bot: str, record: str, rounds: int = 10
) -> dict[str, Any]:
    """Seconds from each write's response until its AuditEvent can be read."""

    lags: dict[str, list[float]] = {"C": [], "U": [], "D": []}
    for i in range(rounds):
        made = _http(
            "POST",
            f"{fx.fhir_url}/Patient",
            body={"resourceType": "Patient", "name": [{"family": f"Lag{i}"}]},
            token=bot,
        )
        url = f"{fx.fhir_url}/Patient/{made['id']}"
        for action in ("C", "U", "D"):
            if action == "U":
                _http(
                    "PUT",
                    url,
                    body={
                        "resourceType": "Patient",
                        "id": made["id"],
                        "name": [{"family": "Lagged"}],
                    },
                    token=bot,
                )
            elif action == "D":
                _http("DELETE", url, token=bot)
            start = time.monotonic()
            query = (
                f"{fx.fhir_url}/AuditEvent?entity=Patient/{made['id']}&action={action}"
            )
            while (
                not _http("GET", query, token=record).get("entry")
                and time.monotonic() - start < 30
            ):
                time.sleep(0.02)
            lags[action].append(round(time.monotonic() - start, 3))
    return {
        a: {"max": max(v), "median": sorted(v)[len(v) // 2]} for a, v in lags.items()
    }


def _try(call: Any) -> Optional[str]:
    """None when the call succeeds, else the HTTP status it failed with."""

    try:
        call()
        return None
    except FixtureError as exc:
        return (
            str(exc).split(" -> ")[1].split(":")[0] if " -> " in str(exc) else str(exc)
        )


def probe(fx: MedplumFixture) -> dict[str, Any]:
    actors = Actors(fx)
    fx.restrict_agent(actors.ref("bot"), on=False)
    actors._tokens.pop("bot", None)
    facts: dict[str, Any] = {}
    record = fx.client_token("record-check")
    bot, other = actors.token("bot"), actors.token("other")
    bot_ref, other_ref = actors.ref("bot"), actors.ref("other")
    me = _http("GET", f"{fx.base_url}/auth/me", token=bot)
    facts["bot_is_super_admin"] = bool(me["project"].get("superAdmin"))
    facts["bot_is_project_admin"] = bool(me["membership"].get("admin"))
    facts["bot_admin_endpoint"] = (
        _try(lambda: _http("GET", f"{fx.base_url}/admin/super/migrations", token=bot))
        or "allowed"
    )

    with urllib.request.urlopen(f"{fx.fhir_url}/metadata?_summary=true") as resp:  # noqa: S310
        facts["date_header"] = bool(resp.headers.get("Date"))

    # Create, another user's update, a delete: logged at once, by whom.
    since = _since()
    made = _http(
        "POST",
        f"{fx.fhir_url}/Patient",
        body={
            "resourceType": "Patient",
            "name": [{"given": ["Probe"], "family": "Zed"}],
        },
        token=bot,
    )
    edit = {k: v for k, v in made.items() if k != "meta"} | {"birthDate": "1990-01-01"}
    _http("PUT", f"{fx.fhir_url}/Patient/{made['id']}", body=edit, token=other)
    gone = _http(
        "POST",
        f"{fx.fhir_url}/Patient",
        body={
            "resourceType": "Patient",
            "name": [{"family": "Forge"}],
            "meta": {"author": {"reference": other_ref}},
        },
        token=bot,
    )
    _http("DELETE", f"{fx.fhir_url}/Patient/{gone['id']}", token=bot)
    writes = _writes(fx, record, since)  # read at once: no settle time
    facts["audit_seen_on_an_immediate_read"] = sorted(
        f"{w['action']} by {'bot' if w['who'] == [bot_ref] else 'other' if w['who'] == [other_ref] else w['who']}"
        for w in writes
    )
    facts["audit_names_the_user_profile"] = {w["who"][0] for w in writes} == {
        bot_ref,
        other_ref,
    }
    read = _http("GET", f"{fx.fhir_url}/Patient/{made['id']}", token=record)
    facts["meta_author_returned"] = "author" in (read.get("meta") or {})
    history = _http("GET", f"{fx.fhir_url}/Patient/{gone['id']}/_history", token=record)
    first = (history.get("entry") or [{}])[0]
    facts["delete_in_history"] = (first.get("request") or {}).get("method") == "DELETE"
    sweep = _http(
        "GET",
        f"{fx.fhir_url}/Patient?_lastUpdated=ge{since}&_elements=id",
        token=record,
    )
    facts["sweep_sees_delete"] = gone["id"] in {
        e["resource"]["id"] for e in sweep.get("entry") or []
    }
    facts["type_history"] = (
        _try(lambda: _http("GET", f"{fx.fhir_url}/AuditEvent/_history", token=record))
        or "allowed"
    )

    facts["audit_lag_s"] = _audit_lag(fx, bot, record)

    # Writes in one request: batch, transaction, GraphQL.
    since = _since()
    for kind in ("batch", "transaction"):
        _http(
            "POST",
            fx.fhir_url,
            body={
                "resourceType": "Bundle",
                "type": kind,
                "entry": [
                    {
                        "request": {"method": "POST", "url": "Patient"},
                        "resource": {
                            "resourceType": "Patient",
                            "name": [{"family": kind}],
                        },
                    },
                    {
                        "request": {"method": "POST", "url": "Observation"},
                        "resource": {
                            "resourceType": "Observation",
                            "status": "final",
                            "code": {"text": kind},
                        },
                    },
                ],
            },
            token=bot,
        )
    _http(
        "POST",
        f"{fx.fhir_url}/$graphql",
        body={
            "query": 'mutation { PatientCreate(res: {resourceType: "Patient", '
            'name: [{family: "gql"}]}) { id } }'
        },
        token=bot,
    )
    facts["bundle_and_graphql_writes_logged"] = sorted(
        f"{w['action']} {w['what'][0].split('/')[0]} by {'bot' if w['who'] == [bot_ref] else w['who']}"
        for w in _writes(fx, record, since)
    )

    # The audit trail itself: can the agent change it, and is that logged?
    def audit_writes(token: str) -> dict[str, Optional[str]]:
        sample = _http("GET", f"{fx.fhir_url}/AuditEvent?_count=1", token=record)
        event = sample["entry"][0]["resource"]
        body = {k: v for k, v in event.items() if k not in ("id", "meta")}
        return {
            "create": _try(
                lambda: _http(
                    "POST", f"{fx.fhir_url}/AuditEvent", body=body, token=token
                )
            ),
            "update": _try(
                lambda: _http(
                    "PUT",
                    f"{fx.fhir_url}/AuditEvent/{event['id']}",
                    body=event,
                    token=token,
                )
            ),
            "delete": _try(
                lambda: _http(
                    "DELETE", f"{fx.fhir_url}/AuditEvent/{event['id']}", token=token
                )
            ),
        }

    since = _since()
    facts["unrestricted_bot_audit_writes"] = audit_writes(bot)
    facts["audit_writes_are_logged"] = any(
        "AuditEvent/" in (w["what"][0] or "") for w in _writes(fx, record, since)
    )
    policy = fx.restrict_agent(bot_ref)
    actors._tokens.pop("bot", None)
    facts["policy"] = f"AccessPolicy/{policy}"
    facts["restricted_bot_audit_writes"] = audit_writes(actors.token("bot"))
    facts["restricted_bot_audit_read_only"] = audit_is_read_only(fx, actors, "bot")
    facts["restricted_bot_can_write"] = {
        rtype: _try(
            lambda body=body: _http(
                "POST",
                f"{fx.fhir_url}/{body['resourceType']}",
                body=body,
                token=actors.token("bot"),
            )
        )
        or "allowed"
        for rtype, body in {
            "Patient": {"resourceType": "Patient", "name": [{"family": "Policy"}]},
            "Observation": {
                "resourceType": "Observation",
                "status": "final",
                "code": {"text": "x"},
            },
            "Task": {"resourceType": "Task", "status": "draft", "intent": "order"},
        }.items()
    }
    return facts


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    facts = probe(MedplumFixture())
    text = json.dumps(facts, indent=2, sort_keys=True) + "\n"
    open(args.out, "w").write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
