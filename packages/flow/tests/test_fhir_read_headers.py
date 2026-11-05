"""Generic metadata headers through the real factory and HTTP transport."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.runtime.effects.fhir import FhirEffectVerifier


@pytest.fixture
def http_source():
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            seen.append(dict(self.headers))
            body = json.dumps({"resourceType": "Bundle", "type": "searchset", "total": 1,
                               "entry": [{"resource": {"resourceType": "Basic", "id": "synthetic-record"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/fhir+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def config(base: str, headers: dict[str, str] | None) -> EffectsConfig:
    return EffectsConfig(kind="fhir", base_url=base, resource_type="Basic", field_paths={"id": "id"},
                         access_token="synthetic-unit-token", fhir_read_headers=headers)


@pytest.mark.parametrize("headers", [None, {}, {"X-Record-Metadata": "complete"}])
def test_factory_forwards_metadata_and_preserves_default_auth_and_tls(http_source: Any, headers: Any) -> None:
    base, seen = http_source
    cfg = config(base, headers)
    built = build_effect_verifier(cfg, {})
    assert isinstance(built, FhirEffectVerifier)
    assert built.verify_tls is True
    assert not hasattr(built, "headers")  # Existing read-tag behavior unchanged.
    state = built.capture_pre_state()
    assert state.reachable and state.records == [{"id": "synthetic-record"}]
    assert len(seen) == 1
    assert seen[0]["Accept"] == "application/fhir+json"
    assert seen[0]["Authorization"] == "Bearer synthetic-unit-token"
    assert seen[0].get("X-Record-Metadata") == (headers or {}).get("X-Record-Metadata")
    if not headers:
        assert "fhir_read_headers" not in cfg.model_dump(mode="json")
        assert config(base, None).model_dump(mode="json") == cfg.model_dump(mode="json")


@pytest.mark.parametrize("name", ["Authorization", "Cookie", "Host", "Proxy-Authorization", "Origin", "Content-Length",
                                  "Transfer-Encoding", "Forwarded", "X-Forwarded-For", "Accept", "Cache-Control", "X-OpenAdapt-Execution-Id", "Proxy", "X-HTTP-Method-Override"])
def test_reserved_header_refuses_at_config_and_direct_adapter_before_http(http_source: Any, name: str) -> None:
    base, seen = http_source
    with pytest.raises(ValueError):
        config(base, {name: "override"})
    with pytest.raises(ValueError):
        FhirEffectVerifier(base, resource_type="Basic", field_paths={"id": "id"}, read_headers={name: "override"})
    assert seen == []


@pytest.mark.parametrize("headers", [{"bad name": "value"}, {"X-Meta": "line\nvalue"}, {"X-Meta": "line\rvalue"},
                                      {"X-Meta": "tab\tvalue"}, {"X-Meta": "delete\x7fvalue"}, {"X-Meta": "nonascii-\u00e9"},
                                      {"X-Meta": "one", "x-meta": "two"}])
def test_invalid_header_refuses_before_http(http_source: Any, headers: dict[str, str]) -> None:
    base, seen = http_source
    with pytest.raises(ValueError):
        config(base, headers)
    with pytest.raises(ValueError):
        FhirEffectVerifier(base, resource_type="Basic", field_paths={"id": "id"}, read_headers=headers)
    assert seen == []
