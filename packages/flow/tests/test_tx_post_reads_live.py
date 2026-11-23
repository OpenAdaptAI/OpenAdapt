"""Read-only ``POST``s on a real app (#2389), lab only.

Runs against a local Medplum started with ``benchmark/fhir_medplum``'s
fixture when ``OAFLOW_TX_LIVE_MEDPLUM_STATE`` names the fixture's state
directory (``OAFLOW_TX_LIVE_MEDPLUM_PORT`` and ``_PROJECT`` override the port
and compose project). It is skipped otherwise. Synthetic records only.

The benchmark found that FHIR batch reads (``POST /fhir/R4``) and GraphQL
queries (``POST /fhir/R4/$graphql``) used up the tap's single mutation, so the
real save got 403. FHIR search by ``POST`` (``/Patient/_search``) is the same
class with no protocol marker, so it is learned on the sandbox instead. The
store digest hashes every table in the app's database.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.request
from pathlib import Path
from typing import Any, Optional

import pytest

STATE = os.environ.get("OAFLOW_TX_LIVE_MEDPLUM_STATE", "").strip()
PORT = int(os.environ.get("OAFLOW_TX_LIVE_MEDPLUM_PORT", "9744"))
PROJECT = os.environ.get("OAFLOW_TX_LIVE_MEDPLUM_PROJECT", "lens-medplum")

pytestmark = pytest.mark.skipif(not STATE, reason="set OAFLOW_TX_LIVE_MEDPLUM_STATE")

DIGEST_SQL = (
    "select md5(string_agg(md5(query_to_xml(format('select * from %I.%I order by 1',"
    " schemaname, tablename), true, false, '')::text), '' order by tablename))"
    " from pg_tables where schemaname='public'"
)


def _req(
    base: str,
    method: str,
    path: str,
    token: str,
    body: Optional[bytes] = None,
    ctype: str = "application/fhir+json",
) -> tuple[int, Any]:
    request = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": ctype},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as err:
        raw, status = err.read(), err.code
    try:
        return status, json.loads(raw or b"null")
    except ValueError:
        return status, None


@pytest.fixture(scope="module")
def live() -> dict[str, Any]:
    from benchmark.fhir_medplum.fixture import MedplumFixture

    fx = MedplumFixture(
        state_dir=Path(STATE), server_port=PORT, app_port=PORT + 1, project=PROJECT
    )
    token = fx.client_token("lens")
    family = "Lensprobe" + secrets.token_hex(3)
    patient = {
        "resourceType": "Patient",
        "name": [{"family": family, "given": ["Casey"]}],
        "birthDate": "1980-02-03",
        "telecom": [{"system": "email", "value": f"{family.lower()}@example.test"}],
    }
    pid = fx.create_patient(patient, token=token)
    base = fx.base_url.replace("localhost", "127.0.0.1")
    _, original = _req(base, "GET", f"/fhir/R4/Patient/{pid}", token)
    return {
        "fx": fx,
        "token": token,
        "pid": pid,
        "family": family,
        "base": base,
        "original": original,
    }


def _reads(live: dict[str, Any]) -> list[tuple[str, str, bytes, str]]:
    pid, family = live["pid"], live["family"]
    batch = {
        "resourceType": "Bundle",
        "type": "batch",
        "entry": [
            {"request": {"method": "GET", "url": f"Patient/{pid}"}},
            {"request": {"method": "GET", "url": f"Patient?family={family}"}},
        ],
    }
    gql = {"query": f'{{ PatientList(family: "{family}") {{ id name {{ family }} }} }}'}
    return [
        ("POST", "/fhir/R4", json.dumps(batch).encode(), "application/fhir+json"),
        ("POST", "/fhir/R4/$graphql", json.dumps(gql).encode(), "application/json"),
        (
            "POST",
            "/fhir/R4/Patient/_search",
            f"family={family}&_count=5".encode(),
            "application/x-www-form-urlencoded",
        ),
    ]


def _save_body(live: dict[str, Any], email: str) -> bytes:
    body = {k: v for k, v in live["original"].items() if k != "meta"}
    body["telecom"] = [{"system": "email", "value": email}]
    return json.dumps(body).encode()


def _restore(live: dict[str, Any]) -> None:
    body = {k: v for k, v in live["original"].items() if k != "meta"}
    status, _ = _req(
        live["base"],
        "PUT",
        f"/fhir/R4/Patient/{live['pid']}",
        live["token"],
        json.dumps(body).encode(),
    )
    assert status == 200


def _through_tap(tap: Any, live: dict[str, Any], calls: list) -> list[int]:
    return [
        _req(tap.base_url, m, p, live["token"], body, ctype)[0]
        for m, p, body, ctype in calls
    ]


def test_live_batch_and_graphql_reads_are_classified_as_reads(live) -> None:
    from openadapt_flow.tx.wire_tap import SessionWireTap, WirePolicy

    with SessionWireTap(live["base"], WirePolicy(max_mutations=1)) as tap:
        statuses = _through_tap(tap, live, _reads(live)[:2])
        summary = tap.summary()
    assert statuses == [200, 200]
    assert summary.read_requests == 2 and summary.mutations_allowed == 0


def test_live_probe_learns_post_search_and_refuses_the_save(live) -> None:
    from openadapt_flow.tx.read_probe import probe_read_routes
    from openadapt_flow.tx.wire_tap import SessionWireTap, WirePolicy

    pid = live["pid"]
    with SessionWireTap(
        live["base"], WirePolicy(max_mutations=None, secrets=(live["token"],))
    ) as tap:
        calls = _reads(live) + [
            (
                "PUT",
                f"/fhir/R4/Patient/{pid}",
                _save_body(live, "demo@example.test"),
                "application/fhir+json",
            ),
            # A second save, by POST to the type's _search sibling, would be a
            # different route; the save here is the PUT above.
        ]
        _through_tap(tap, live, calls)
        har = tap.export_har()
    _restore(live)

    fx = live["fx"]
    report = probe_read_routes(
        har,
        sandbox_base_url=live["base"],
        store_digest=lambda: fx.psql(DIGEST_SQL).strip(),
        store_reset=lambda: _restore(live),
        headers={"Authorization": "Bearer " + live["token"]},
    )
    by_path = {r.path: r for r in report.routes}
    assert report.store_idle
    # Only the unmarked POST read is probed; batch and GraphQL reads were
    # already reads, and the PUT is not a POST.
    assert set(by_path) == {"/fhir/R4/Patient/_search"}
    search = by_path["/fhir/R4/Patient/_search"]
    assert search.read_only, search.reason
    live["read_routes"] = report.read_routes


def test_live_save_goes_through_after_post_reads(live) -> None:
    from openadapt_flow.tx.wire_tap import SessionWireTap, WirePolicy

    if "read_routes" not in live:
        pytest.skip("needs the probe test's learned routes")
    pid = live["pid"]
    policy = WirePolicy(max_mutations=1, read_routes=live["read_routes"])
    with SessionWireTap(live["base"], policy) as tap:
        calls = _reads(live) + [
            (
                "PUT",
                f"/fhir/R4/Patient/{pid}",
                _save_body(live, "saved@example.test"),
                "application/fhir+json",
            ),
            (
                "PUT",
                f"/fhir/R4/Patient/{pid}",
                _save_body(live, "second@example.test"),
                "application/fhir+json",
            ),
        ]
        statuses = _through_tap(tap, live, calls)
        summary = tap.summary()
    # Three reads, the one save, and a second write still blocked.
    assert statuses == [200, 200, 200, 200, 403]
    assert summary.read_requests == 3
    assert summary.mutations_allowed == 1 and summary.mutations_blocked == 1
    _, stored = _req(live["base"], "GET", f"/fhir/R4/Patient/{pid}", live["token"])
    assert stored["telecom"] == [{"system": "email", "value": "saved@example.test"}]
    _restore(live)


def test_live_learned_route_does_not_cover_a_post_write(live) -> None:
    """A POST create on another path is never covered by the learned read."""
    from openadapt_flow.tx.wire_tap import matches_read_route

    if "read_routes" not in live:
        pytest.skip("needs the probe test's learned routes")
    body = json.dumps({"resourceType": "Patient"}).encode()
    assert not matches_read_route(
        live["read_routes"],
        "POST",
        "/fhir/R4/Patient",
        body,
        [("Content-Type", "application/fhir+json")],
    )


def test_live_wrapped_save_is_not_refuted_by_post_reads(live, tmp_path) -> None:
    """The benchmark's second symptom: correct saves came back REFUTED."""
    from openadapt_flow.tx.lens import TransactionalCommitEngine
    from openadapt_flow.tx.types import EndpointTemplate, EntityLensSpec, TxTask
    from openadapt_flow.tx.wire_tap import SessionWireTap, WirePolicy

    if "read_routes" not in live:
        pytest.skip("needs the probe test's learned routes")
    pid = live["pid"]
    item = "/fhir/R4/Patient/{id}"
    lens = EntityLensSpec(
        entity_name="patient",
        base_url=live["base"],
        write_mode="replace",
        get_one=EndpointTemplate(method="GET", path_template=item),
        write_endpoint=EndpointTemplate(method="PUT", path_template=item),
    )
    task_id = "live-" + secrets.token_hex(4)
    policy = WirePolicy(max_mutations=1, read_routes=live["read_routes"])
    with SessionWireTap(live["base"], policy, session_id=task_id) as tap:
        engine = TransactionalCommitEngine(
            lens,
            tmp_path / "engine",
            wire_tap=tap,
            headers={"Authorization": "Bearer " + live["token"]},
            allow_unqualified_prototype=True,
        )
        body = {k: v for k, v in live["original"].items() if k != "meta"}
        body["birthDate"] = "1981-04-05"
        calls = _reads(live) + [
            (
                "PUT",
                f"/fhir/R4/Patient/{pid}",
                json.dumps(body).encode(),
                "application/fhir+json",
            )
        ]
        result = engine.wrap_action(
            TxTask(
                task_id=task_id,
                operation="update",
                entity_id=pid,
                intended_delta={"birthDate": "1981-04-05"},
            ),
            lambda: _through_tap(tap, live, calls),
        )
    _restore(live)
    assert result.outcome == "UNCERTAIN", result.reasons
    assert set(result.reasons) == {"unqualified_observation_only"}
