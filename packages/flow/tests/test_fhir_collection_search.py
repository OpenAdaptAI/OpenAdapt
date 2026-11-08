"""Collection search compatibility over real HTTP and a locked SQLite store."""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from openadapt_flow.fhir_setup import learn_fhir_read
from openadapt_flow.rest_setup import ReadProbe
from openadapt_flow.runtime.effects import Effect, EffectKind, Verdict
from openadapt_flow.runtime.effects.fhir import (
    COLLECTION_DIAGNOSTIC,
    FhirEffectVerifier,
)
from tests.test_quiescent_read_setup import LockedAuthority, capability


@pytest.fixture
def service(tmp_path):
    db = sqlite3.connect(tmp_path / "collection.sqlite", check_same_thread=False)
    db.execute("CREATE TABLE rows(id TEXT PRIMARY KEY, value TEXT)")
    state = SimpleNamespace(
        lock=threading.RLock(),
        active=False,
        owner=None,
        pending=False,
        lost=False,
        checks=0,
        calls=0,
        status=200,
        transform=lambda body, call: body,
    )

    def seed(rows):
        with state.lock:
            db.execute("DELETE FROM rows")
            db.executemany("INSERT INTO rows VALUES (?, ?)", rows)
            db.commit()

    def snapshot():
        return [
            {"id": row[0], "value": row[1]}
            for row in db.execute("SELECT id,value FROM rows ORDER BY id")
        ]

    state.seed, state.snapshot = seed, snapshot

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state.calls += 1
            rows = snapshot()
            body = {
                "resourceType": "Bundle",
                "type": "collection",
                "total": len(rows),
                "entry": [
                    {
                        "resource": {
                            "resourceType": "Observation",
                            "id": row["id"],
                            "valueString": row["value"],
                        }
                    }
                    for row in rows
                ],
                "link": [{"relation": "self", "url": state.url + "/Observation"}],
            }
            body = state.transform(body, state.calls)
            raw = json.dumps(body).encode()
            self.send_response(state.status)
            self.send_header("Content-Type", "application/fhir+json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        db.close()


def reader(service, client, *, quiescent=True, authority=None):
    return FhirEffectVerifier(
        service.url,
        resource_type="Observation",
        field_paths={"id": "id", "value": "valueString"},
        session=client,
        timeout_s=2,
        poll_interval_s=0,
        consistency="quiescent-two-pass" if quiescent else None,
        quiescence=(authority or LockedAuthority(service)) if quiescent else None,
    )


def effect():
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"id": "one"},
        field="value",
        value="new",
        timeout_s=0,
    )


@pytest.mark.parametrize("quiescent", [False, True])
def test_complete_collection_reads_and_verifies_actual_change(service, quiescent):
    service.seed([("one", "old")])
    with httpx.Client() as client:
        verifier = reader(service, client, quiescent=quiescent)
        before = verifier.capture_pre_state()
        assert before.reachable and before.records == service.snapshot()
        assert before.readback_complete is (True if quiescent else None)
        assert before.detail["read_diagnostics"] == [COLLECTION_DIAGNOSTIC]
        service.seed([("one", "new")])
        verdict = verifier.verify(effect(), before)
        assert verdict.verdict is Verdict.CONFIRMED
        assert COLLECTION_DIAGNOSTIC in verdict.reason
        assert service.calls == (4 if quiescent else 2)


@pytest.mark.parametrize("omit_entry", [False, True])
@pytest.mark.parametrize("omit_links", [False, True])
def test_explicit_empty_collection_is_readable_not_a_write(
    service, omit_entry, omit_links
):
    def transform(body, _call):
        if omit_entry:
            body.pop("entry")
        if omit_links:
            body.pop("link")
        return body

    service.transform = transform
    with httpx.Client() as client:
        verifier = reader(service, client)
        before = verifier.capture_pre_state()
        assert before.reachable and before.readback_complete is True
        assert before.records == []
        verdict = verifier.verify(effect(), before)
        assert verdict.verdict is not Verdict.CONFIRMED
        assert COLLECTION_DIAGNOSTIC in verdict.reason


def invalid_body(case, body):
    if case == "missing-total":
        body.pop("total")
    elif case.startswith("total-"):
        body["total"] = {
            "true": True,
            "false": False,
            "string": "1",
            "float": 1.0,
            "negative": -1,
            "null": None,
            "low": 0,
            "high": 2,
        }[case[6:]]
    elif case.startswith("entries-"):
        body["entry"] = {
            "null": None,
            "object": {},
            "scalar": [1],
            "missing-resource": [{}],
            "resource-null": [{"resource": None}],
        }[case[8:]]
    elif case.startswith("resource-"):
        resource = body["entry"][0]["resource"]
        part = case[9:]
        if part == "missing-type":
            resource.pop("resourceType")
        elif part == "missing-id":
            resource.pop("id")
        elif part == "empty-id":
            resource["id"] = ""
        else:
            resource["resourceType"] = {
                "mixed": "Patient",
                "null": None,
                "outcome": "OperationOutcome",
            }[part]
    elif case == "duplicate-id":
        body["entry"].append(copy.deepcopy(body["entry"][0]))
        body["total"] = 2
    elif case.startswith("search-"):
        body["entry"][0]["search"] = {
            "include": {"mode": "include"},
            "outcome": {"mode": "outcome"},
            "unknown": {"mode": "other"},
            "scalar": "match",
            "null": None,
        }[case[7:]]
    elif case.startswith("links-"):
        body["link"] = {
            "null": None,
            "object": {},
            "scalar": ["self"],
            "missing-relation": [{"url": "/Observation"}],
            "missing-url": [{"relation": "self"}],
            "blank-url": [{"relation": "self", "url": " "}],
            "integer-url": [{"relation": "self", "url": 1}],
            "space-url": [{"relation": "self", "url": "/bad path"}],
            "malformed-url": [{"relation": "self", "url": "http://[broken"}],
        }[case[6:]]
    elif case.startswith("relation-"):
        body["link"].append({"relation": case[9:], "url": "/Observation?cursor=2"})
    else:
        raise AssertionError(case)
    return body


INVALID = [
    "missing-total",
    "total-true",
    "total-false",
    "total-string",
    "total-float",
    "total-negative",
    "total-null",
    "total-low",
    "total-high",
    "entries-null",
    "entries-object",
    "entries-scalar",
    "entries-missing-resource",
    "entries-resource-null",
    "resource-mixed",
    "resource-null",
    "resource-missing-type",
    "resource-outcome",
    "resource-missing-id",
    "resource-empty-id",
    "duplicate-id",
    "search-include",
    "search-outcome",
    "search-unknown",
    "search-scalar",
    "search-null",
    "links-null",
    "links-object",
    "links-scalar",
    "links-missing-relation",
    "links-missing-url",
    "links-blank-url",
    "links-integer-url",
    "links-space-url",
    "links-malformed-url",
    "relation-next",
    "relation-previous",
    "relation-first",
    "relation-last",
    "relation-related",
    "relation-SELF",
    "relation-",
]


@pytest.mark.parametrize("case", INVALID)
@pytest.mark.parametrize("quiescent", [False, True])
def test_collection_refusals_remain_indeterminate(service, case, quiescent):
    service.seed([("one", "new")])
    with httpx.Client() as client:
        verifier = reader(service, client, quiescent=quiescent)
        before = verifier.capture_pre_state()
        assert before.reachable
        service.transform = lambda body, _call: invalid_body(case, body)
        state = verifier.capture_post_state()
        assert not state.reachable and state.records == []
        assert state.readback_complete is not True
        assert state.detail["read_problem"]
        verdict = verifier.verify(effect(), before)
        assert verdict.verdict is Verdict.INDETERMINATE


@pytest.mark.parametrize("state", ["PENDING", "INVALID"])
def test_collection_does_not_override_completion(service, state):
    service.seed([("one", "new")])
    authority = LockedAuthority(service)
    authority.completion_mode = state
    with httpx.Client() as client:
        verifier = reader(service, client, authority=authority)
        result = verifier.capture_pre_state()
        assert not result.reachable and result.readback_complete is False
        assert service.calls == 0


def test_collection_requires_actual_quiescence_authority(service):
    with httpx.Client() as client:
        verifier = reader(service, client)
        verifier.quiescence = None
        assert not verifier.capture_pre_state().reachable
        assert service.calls == 0


@pytest.mark.parametrize("status", [206, 401, 403, 500])
def test_collection_never_overrides_http_refusal(service, status):
    service.seed([("one", "new")])
    with httpx.Client() as client:
        verifier = reader(service, client)
        before = verifier.capture_pre_state()
        assert before.reachable and before.readback_complete is True
        assert service.calls == 2
        service.status = status
        assert not verifier.capture_pre_state().reachable
        assert service.calls == 3


def test_diagnostic_uses_actual_poll_not_earlier_snapshot(service):
    service.seed([("one", "new")])
    with httpx.Client() as client:
        verifier = reader(service, client)
        before = verifier.capture_pre_state()
        assert COLLECTION_DIAGNOSTIC in before.detail["read_diagnostics"]
        service.transform = lambda body, _call: dict(body, type="searchset")
        verdict = verifier.verify(effect(), before)
        assert verdict.confirmed and COLLECTION_DIAGNOSTIC not in verdict.reason
        assert "read_diagnostics" not in verifier.capture_post_state().detail


@pytest.mark.parametrize("collection_pass", [1, 2])
def test_two_pass_diagnostic_retains_both_actual_reads(service, collection_pass):
    service.seed([("one", "new")])
    service.transform = lambda body, call: dict(
        body, type="collection" if call == collection_pass else "searchset"
    )
    with httpx.Client() as client:
        state = reader(service, client).capture_pre_state()
        assert state.reachable and state.readback_complete is True
        assert state.detail["read_diagnostics"] == [COLLECTION_DIAGNOSTIC]
        assert service.calls == 2


def test_collection_two_pass_disagreement_refuses(service):
    service.seed([("one", "new")])

    def transform(body, call):
        if call % 2 == 0:
            body["entry"][0]["resource"]["valueString"] = "other"
        return body

    service.transform = transform
    with httpx.Client() as client:
        state = reader(service, client).capture_pre_state()
        assert not state.reachable and state.readback_complete is False
        assert service.calls == 2


def test_genuine_learner_uses_collection_reader_with_independent_store(service):
    service.seed([("one", "new")])
    with httpx.Client() as client:
        learned = learn_fhir_read(
            capability(service),
            base_url=service.url,
            session=client,
            probes=[ReadProbe(snapshot=service.snapshot)],
            quiescence=LockedAuthority(service),
            field_paths={"id": "id", "value": "valueString"},
        )
        assert learned.config is not None, learned.problems
        assert learned.config["resource_type"] == "Observation"
        assert learned.evidence["observations"]
        assert learned.evidence["status"] == "draft"
        assert service.calls == 2
