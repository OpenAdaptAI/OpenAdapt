"""FHIR and GraphQL verifiers must read their system of record on a clean install.

Companion to ``test_rest_verifier_transport``, which covers the same defect in
``RestRecordVerifier``. ``requests`` is a development dependency (and a
``windows`` extra); ``httpx`` is a core one. ``FhirEffectVerifier`` and
``GraphQLRecordVerifier`` both imported ``requests`` unconditionally, so on a
wheel-only install ``_get_session`` raised ``ModuleNotFoundError``. Both read
paths swallow every transport error, so the verifier read the system of record
as *unreadable* and HALTed: fail-safe (never an invented confirmation) but
unusable out of the box.

The FHIR verifier additionally passes TLS verification per request, which
``requests`` accepts and ``httpx`` does not -- ``httpx`` binds it when the
client is constructed. Pinned below so a future edit cannot reintroduce a
``TypeError`` that would read as an unreadable system of record.
"""

from __future__ import annotations

import builtins
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterator

import pytest

from openadapt_flow.runtime.effects.fhir import FhirEffectVerifier
from openadapt_flow.runtime.effects.graphql import GraphQLRecordVerifier

FHIR_BUNDLE = {
    "resourceType": "Bundle",
    "entry": [
        {
            "resource": {
                "resourceType": "Observation",
                "id": "obs-1",
                "valueString": "42",
                "code": {"text": "bp"},
            }
        }
    ],
}
GRAPHQL_BODY = {"data": [{"id": 1, "status": "posted"}]}


def _hide(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    real_import = builtins.__import__

    def fake_import(module: str, *args: Any, **kwargs: Any) -> Any:
        if module == name:
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(module, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


class _Handler(BaseHTTPRequestHandler):
    def _send(self, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._send(FHIR_BUNDLE)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._send(GRAPHQL_BODY)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def sor() -> Iterator[str]:
    """A loopback stand-in for the system of record (stdlib only)."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class TestFhirTransport:
    def test_session_falls_back_to_httpx_without_requests(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        _hide(monkeypatch, "requests")
        assert isinstance(
            FhirEffectVerifier("http://127.0.0.1:1")._get_session(), httpx.Client
        )

    def test_injected_session_is_always_preferred(self) -> None:
        sentinel = object()
        verifier = FhirEffectVerifier("http://127.0.0.1:1", session=sentinel)
        assert verifier._get_session() is sentinel

    def test_records_are_read_over_the_fallback_transport(
        self, monkeypatch: pytest.MonkeyPatch, sor: str
    ) -> None:
        """The fallback is wired end to end, not merely constructible."""
        _hide(monkeypatch, "requests")
        state = FhirEffectVerifier(sor, resource_type="Observation").capture_pre_state()
        assert state.reachable is True
        assert [record["id"] for record in state.records] == ["obs-1"]

    def test_tls_verification_is_bound_on_the_fallback_client(
        self, monkeypatch: pytest.MonkeyPatch, sor: str
    ) -> None:
        """``verify_tls`` must reach httpx at construction, never per request.

        Passing it per request raises ``TypeError`` inside ``_search``, which
        the blanket except reads as an unreadable system of record.
        """
        _hide(monkeypatch, "requests")
        verifier = FhirEffectVerifier(sor, verify_tls=False)
        assert verifier._get_session() is not None
        assert verifier._verify_per_request is False
        assert verifier.capture_pre_state().reachable is True


class TestGraphQlTransport:
    def test_session_falls_back_to_httpx_without_requests(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        _hide(monkeypatch, "requests")
        verifier = GraphQLRecordVerifier(
            "http://127.0.0.1:1/graphql", query="query Q { things { id } }"
        )
        assert isinstance(verifier._get_session(), httpx.Client)

    def test_injected_session_is_always_preferred(self) -> None:
        sentinel = object()
        verifier = GraphQLRecordVerifier(
            "http://127.0.0.1:1/graphql",
            query="query Q { things { id } }",
            session=sentinel,
        )
        assert verifier._get_session() is sentinel

    def test_records_are_read_over_the_fallback_transport(
        self, monkeypatch: pytest.MonkeyPatch, sor: str
    ) -> None:
        _hide(monkeypatch, "requests")
        verifier = GraphQLRecordVerifier(
            f"{sor}/graphql", query="query Q { things { id } }"
        )
        state = verifier.capture_pre_state()
        assert state.reachable is True
        assert [record["id"] for record in state.records] == [1]
