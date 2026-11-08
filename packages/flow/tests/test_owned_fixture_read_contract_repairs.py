"""Owned endpoint declarations remain explicit and never override read failures."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.fault_gate import MemoryRestCopy, check_digest
from openadapt_flow.runtime.effects.rest import RestRecordVerifier
from openadapt_flow.verifier import (
    Actor,
    BaselineUnreadable,
    Contract,
    MemoryLedger,
    begin_write,
)
from openadapt_flow.verify_run import parse_verify_config
from tests._rest_read_contract_fixtures import memory_rest_effects_config


@contextmanager
def memory_store(count):
    rows = [{"id": i, "value": f"row-{i}"} for i in range(count)]
    store = MemoryRestCopy(
        rows, {"value": "new"}, records_path="/api/db", records_key="records"
    )
    store.reset()
    try:
        yield store
    finally:
        store.close()


def capture(config):
    reader = build_effect_verifier(EffectsConfig.model_validate(config))
    assert isinstance(reader, RestRecordVerifier)
    try:
        return reader.capture_pre_state()
    finally:
        if reader._session is not None:
            reader._session.close()


@pytest.mark.parametrize("count", [0, 1, 37])
def test_memory_fixture_declaration_reads_its_entire_actual_store(count):
    with memory_store(count) as store:
        state = capture(memory_rest_effects_config(store.base_url))
        assert state.reachable
        assert state.readback_complete is True
        assert state.records == store.rows()
        assert len(state.records) == count


@pytest.mark.parametrize("complete", [False, None])
def test_same_owned_endpoint_without_complete_declaration_remains_unreadable(complete):
    with memory_store(2) as store:
        config = memory_rest_effects_config(store.base_url)
        config["records_complete"] = complete
        state = capture(config)
        assert not state.reachable
        assert state.readback_complete is not True
        assert state.records == []
        assert "completeness was not established" in state.detail["read_problem"]
        assert len(store.rows()) == 2


@contextmanager
def response_server(status, body):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


@pytest.mark.parametrize(
    ("status", "body"),
    [(503, b'{"records": []}'), (200, b"not-json"), (200, b'{"records": "bad"}')],
)
def test_declared_complete_read_does_not_bless_error_or_malformed_http(status, body):
    with response_server(status, body) as base:
        state = capture(memory_rest_effects_config(base))
        assert not state.reachable
        assert state.readback_complete is not True
        assert state.records == []


@pytest.mark.parametrize("complete", [False, None])
def test_ordinary_write_refuses_before_owned_store_save(
    complete, tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    with memory_store(1) as store:
        config = memory_rest_effects_config(store.base_url)
        assert capture(config).reachable
        config["records_complete"] = complete
        contract = Contract.from_dict(
            {
                "schema_version": "openadapt.verify/v1",
                "verifier": config,
                "record": {
                    "name": "measurement",
                    "match": ["id"],
                    "fields": {"id": "id", "value": "value"},
                },
                "frame": {"identity_field": "id"},
            }
        )
        before = store.rows()
        with pytest.raises(BaselineUnreadable):
            begin_write(
                contract,
                {"id": "new", "value": "new"},
                actor=Actor("agent", "synthetic"),
                ledger=MemoryLedger(),
            )
            store.save()
        assert store.rows() == before


@pytest.mark.parametrize("complete", [False, None])
def test_coverage_digest_binds_the_explicit_completeness_declaration(complete):
    config = memory_rest_effects_config("http://127.0.0.1:1")
    doc = {
        "schema_version": "openadapt.verify/v1",
        "verifier": config,
        "effects": [
            {"kind": "field_equals", "match": {"id": 1}, "field": "value", "value": "new"}
        ],
    }
    original = parse_verify_config(doc)
    changed = parse_verify_config(
        {**doc, "verifier": {**config, "records_complete": complete}}
    )
    assert check_digest(original) != check_digest(changed)
