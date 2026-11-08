"""A repeated capped collection is not a complete search, over actual HTTP."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from openadapt_flow.fhir_read import check_collection_search
from openadapt_flow.runtime.effects.fhir import (
    COLLECTION_DIAGNOSTIC,
    FhirEffectVerifier,
)
from tests.test_quiescent_read_setup import LockedAuthority


@pytest.fixture
def service():
    state = SimpleNamespace(
        lock=threading.RLock(),
        active=False,
        owner=None,
        pending=False,
        lost=False,
        checks=0,
        calls=0,
        requests=[],
        returned=[],
        mode="collection",
        self_query="",
        rows=[{"id": str(i), "value": str(i)} for i in range(3)],
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state.calls += 1
            state.requests.append(self.path)
            query = urlsplit(self.path).query
            params = parse_qs(query, keep_blank_values=True)
            kind = "collection"
            links = [{"relation": "self", "url": state.url + "/Observation"}]
            if state.mode == "searchset":
                kind = "searchset"
                cursor = int(params.get("cursor", ["0"])[0])
                rows = state.rows[cursor : cursor + 1]
                total = len(state.rows)
                if cursor + 1 < total:
                    links.append(
                        {
                            "relation": "next",
                            "url": state.url + "/Observation?cursor=" + str(cursor + 1),
                        }
                    )
            elif state.mode == "mixed" and not query:
                kind, rows, total = "searchset", [], 1
                links.append(
                    {"relation": "next", "url": state.url + "/Observation?_page=1"}
                )
            else:
                # This fixture's cap is explicit in the request or reported self
                # query. No claim about an unobserved server default is made.
                rows = state.rows[:1] if query or state.self_query else state.rows
                total = len(rows)
            if state.self_query:
                links[0]["url"] += "?" + state.self_query
            state.returned.append(len(rows))
            body = {
                "resourceType": "Bundle",
                "type": kind,
                "total": total,
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
                "link": links,
            }
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/fhir+json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def reader(service, client, *, params=None, quiescent=True, authority=None):
    return FhirEffectVerifier(
        service.url,
        resource_type="Observation",
        field_paths={"id": "id", "value": "valueString"},
        search_params=params,
        session=client,
        timeout_s=2,
        consistency="quiescent-two-pass" if quiescent else None,
        quiescence=(authority or LockedAuthority(service)) if quiescent else None,
    )


QUERY_NAMES = [
    "_count",
    "_offset",
    "_page",
    "_getpagesoffset",
    "_maxresults",
    "_limit",
    "limit",
    "offset",
    "page",
    "cursor",
    "_summary",
    "_elements",
    "_sort",
    "_id",
    "identifier",
    "_count:exact",
    "_COUNT",
    "unknown_control",
]


@pytest.mark.parametrize("name", QUERY_NAMES)
@pytest.mark.parametrize("location", ["configured", "session", "self"])
def test_capped_collection_query_cannot_certify_complete(service, name, location):
    with httpx.Client() as client:
        verifier = reader(service, client)
        baseline = verifier.capture_pre_state()
        assert baseline.reachable and baseline.readback_complete is True
        assert baseline.records == service.rows
        assert service.calls == 2
        if location == "configured":
            verifier.search_params = {name: "1"}
        elif location == "session":
            client.params = {name: "1"}
        else:
            service.self_query = name + "=1"
        observed = verifier.capture_pre_state()
    assert service.returned[-1] == 1
    assert len(service.rows) == 3
    assert not observed.reachable
    assert service.calls == 3
    assert observed.readback_complete is False
    assert observed.records == []
    assert observed.detail["read_problem"] == "quiescent complete-read validation failed"
    if location != "self":
        assert parse_qs(urlsplit(service.requests[-1]).query) == {name: ["1"]}


@pytest.mark.parametrize("quiescent", [False, True])
@pytest.mark.parametrize("size", [0, 3])
def test_empty_query_preserves_collection_compatibility(service, quiescent, size):
    service.rows = service.rows[:size]
    with httpx.Client() as client:
        observed = reader(service, client, quiescent=quiescent).capture_pre_state()
    assert observed.reachable
    assert observed.readback_complete is (True if quiescent else None)
    assert observed.records == service.rows
    assert observed.detail["read_diagnostics"] == [COLLECTION_DIAGNOSTIC]
    assert service.calls == (2 if quiescent else 1)
    assert all(not urlsplit(path).query for path in service.requests)


@pytest.mark.parametrize("value", ["", "0", "1"])
def test_ordinary_collection_also_refuses_configured_query(service, value):
    with httpx.Client() as client:
        observed = reader(
            service, client, params={"_count": value}, quiescent=False
        ).capture_pre_state()
    assert service.calls == 1 and service.returned == [1]
    assert not observed.reachable and observed.readback_complete is None
    assert "empty query" in observed.detail["read_problem"]


def test_searchset_paging_query_retains_complete_two_pass_search(service):
    service.mode = "searchset"
    with httpx.Client() as client:
        observed = reader(service, client, params={"_count": "1"}).capture_pre_state()
    assert observed.reachable and observed.readback_complete is True
    assert observed.records == service.rows
    assert service.returned == [1] * 6
    assert service.calls == 6
    assert [urlsplit(path).query for path in service.requests] == [
        "_count=1", "cursor=1", "cursor=2", "_count=1", "cursor=1", "cursor=2"
    ]
    assert "read_diagnostics" not in observed.detail


def test_collection_on_a_queried_next_page_cannot_certify_complete(service):
    service.mode = "mixed"
    with httpx.Client() as client:
        observed = reader(service, client).capture_pre_state()
    assert not observed.reachable and observed.readback_complete is False
    assert service.calls == 2 and service.returned == [0, 1]
    assert service.requests[-1].endswith("?_page=1")
    assert observed.detail["read_problem"] == "quiescent complete-read validation failed"


@pytest.mark.parametrize("completion", ["PENDING", "INVALID", "UNKNOWN"])
def test_empty_query_does_not_replace_valid_authority(service, completion):
    authority = LockedAuthority(service)
    authority.completion_mode = completion
    with httpx.Client() as client:
        observed = reader(service, client, authority=authority).capture_pre_state()
    assert not observed.reachable and observed.readback_complete is False
    assert service.calls == 0


@pytest.mark.parametrize(
    "query",
    ["_count=", "_count", "=1", "&", "%5Fcount=1", "_count=1&_count=2", "x=%26"],
)
@pytest.mark.parametrize("location", ["request", "effective", "self"])
def test_collection_query_rule_is_closed_over_url_encodings(query, location):
    body = {"total": 0, "entry": []}
    urls = ("https://example.test/Observation", "https://example.test/Observation")
    queried = urls[0] + "?" + query
    if location == "self":
        body["link"] = [{"relation": "self", "url": queried}]
    else:
        urls = (queried, urls[1]) if location == "request" else (urls[0], queried)
    with pytest.raises(ValueError, match="empty query"):
        check_collection_search(body, "Observation", request_urls=urls)


@pytest.mark.parametrize("suffix", ["", "?"])
def test_collection_query_rule_accepts_only_empty_query(suffix):
    url = "https://example.test/Observation" + suffix
    check_collection_search(
        {"total": 0, "link": [{"relation": "self", "url": url}]},
        "Observation",
        search_params={},
        request_urls=(url, url),
    )
