"""Complete-read declarations and lossless baselines over real HTTP/SQLite."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from benchmark.multiapp_common import NullBackend, NullVision, SurfaceRoutedVerifier
from openadapt_flow.ir import ActionKind, ApiBinding, Step, Workflow
from openadapt_flow.runtime.actuators import ApiActuator
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    RestRecordVerifier,
    ValueExpr,
)
from openadapt_flow.runtime.effects.effect import EffectState, EffectVerdict, Verdict
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.verification import VerificationTier


@contextmanager
def controlled_service(tmp_path):
    """Serve complete native SQLite rows over one actual loopback endpoint."""
    database = tmp_path / "records.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        connection.commit()
    finally:
        connection.close()
    observations = {"posts": 0, "mode": "ok"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _reply(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/records":
                self._reply(404, {"error": "unknown_path"})
                return
            if observations["mode"] == "http-error":
                self._reply(503, {"error": "unavailable"})
                return
            if observations["mode"] == "malformed":
                self._reply(200, {"records": {"id": 1}})
                return
            connection = sqlite3.connect(database)
            try:
                connection.row_factory = sqlite3.Row
                records = [
                    dict(row) for row in connection.execute("SELECT * FROM records")
                ]
            finally:
                connection.close()
            if observations["mode"] == "duplicate":
                records = [{"id": 1, "value": "v"}, {"id": 1, "value": "v"}]
            self._reply(200, {"records": records})

        def do_POST(self):
            if self.path != "/write":
                self._reply(404, {"error": "unknown_path"})
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            observations["posts"] += 1
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT INTO records(value) VALUES (?)", (body["value"],)
                )
                connection.commit()
            finally:
                connection.close()
            self._reply(200, {"ok": True})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", observations
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()


@pytest.fixture
def http_client():
    with requests.Session() as client:
        client.trust_env = False
        yield client


def reader(base_url, declaration, http_client):
    kwargs = {} if declaration == "absent" else {"records_complete": declaration}
    return RestRecordVerifier(
        base_url,
        records_path="/records",
        records_key="records",
        session=http_client,
        timeout_s=1,
        poll_interval_s=0.01,
        **kwargs,
    )


@pytest.mark.parametrize("declaration", [True, False, None, "absent"])
@pytest.mark.parametrize("mode", ["ok", "http-error", "malformed", "duplicate"])
def test_actual_http_sqlite_scope_boundary(tmp_path, declaration, mode, http_client):
    with controlled_service(tmp_path) as (base_url, observations):
        observations["mode"] = mode
        verifier = reader(base_url, declaration, http_client)
        before = verifier.capture_pre_state()
        expected_readable = declaration is True and mode == "ok"
        assert before.reachable is expected_readable
        assert (before.readback_complete is True) is expected_readable
        assert observations["posts"] == 0
        if not expected_readable:
            assert verifier.read_problem


@pytest.mark.parametrize("declaration", [True, False, None, "absent"])
@pytest.mark.parametrize("routed", [False, True])
def test_real_replayer_prewrite_and_postwrite_contract(
    tmp_path, declaration, routed, http_client
):
    with controlled_service(tmp_path) as (base_url, observations):
        verifier = reader(base_url, declaration, http_client)
        if routed:
            verifier = SurfaceRoutedVerifier(
                {"records": verifier}, default_surface="records"
            )
        effect = Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"value": ValueExpr(param="value")},
            expected_count=1,
            count_new_only=True,
            risk="irreversible",
            probe="surface=records|the complete controlled record set",
            timeout_s=0.05,
        )
        workflow = Workflow(
            name="explicit-read-contract-characterization",
            params={"value": "v"},
            steps=[
                Step(
                    id="write",
                    intent="write one controlled record",
                    action=ActionKind.KEY,
                    key="Enter",
                    risk="irreversible",
                    api_binding=ApiBinding(
                        method="POST",
                        url_template="/write",
                        body_template={"value": "{value}"},
                        effects=[effect],
                        on_unavailable="halt",
                        timeout_s=1,
                    ),
                )
            ],
        )
        bundle = tmp_path / "bundle"
        workflow.save(bundle)
        workflow = Workflow.load(bundle)
        report = Replayer(
            NullBackend(),
            vision=NullVision(),
            effect_verifier=verifier,
            api_actuator=ApiActuator(base_url, session=http_client, timeout_s=1),
            poll_interval_s=0.01,
        ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run")
        assert len(report.results) == 1
        result = report.results[0]
        if declaration is True:
            assert observations["posts"] == 1
            assert result.actuation == "api"
            assert result.effect_verified is True
            assert report.success is True
        else:
            assert observations["posts"] == 0
            assert result.effect_verified is False
            assert result.actuation is None
            assert "pre-state" in result.error


class CapturingVerifier:
    substrate = "sql"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(self, state):
        self.state = state
        self.seen = None

    def capture_pre_state(self, _context=None):
        return self.state.model_copy(deep=True)

    def verify(self, expected, before, _context=None):
        self.seen = before.model_copy(deep=True)
        return EffectVerdict(
            verdict=Verdict.INDETERMINATE,
            kind=expected.kind,
            reason="characterization does not confer authority",
        )


@pytest.mark.parametrize("complete", [True, False, None])
@pytest.mark.parametrize("target", ["left", "right"])
def test_each_surface_preserves_full_captured_state(complete, target):
    original = EffectState(
        substrate="sql",
        reachable=True,
        records=[{"id": 1, "value": "v"}],
        readback_complete=complete,
        captured_at="2026-11-08T00:00:00+00:00",
        detail={
            "scope": {"table": "records", "filter": "all"},
            "authority": {"status": "unavailable"},
        },
    )
    selected = CapturingVerifier(original)
    other = CapturingVerifier(
        EffectState(
            substrate="sql",
            reachable=False,
            readback_complete=False,
            captured_at="2026-11-07T00:00:00+00:00",
            detail={"scope": "other"},
        )
    )
    mapping = {target: selected, ("right" if target == "left" else "left"): other}
    router = SurfaceRoutedVerifier(mapping, default_surface=target)
    before = router.capture_pre_state()
    assert before.reachable is False
    effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        field="value",
        value="v",
        probe=f"surface={target}|selected record",
    )
    assert router.verification_tier_for(effect) is selected.verification_tier
    router.verify(effect, before)
    assert other.seen is None
    assert selected.seen.model_dump(mode="json") == original.model_dump(mode="json")
