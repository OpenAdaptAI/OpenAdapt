"""Real headless web setup through the first replay handoff, without inputs."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from openadapt_flow.ir import Workflow
from openadapt_flow.qualify import campaign
from openadapt_flow.qualify.environment import (
    web_environment_digest,
    web_environment_payload,
)
from tests.test_qualify_declarative import _config, _workflow


class _FirstRunReached(BaseException):
    """Stop before replay, admission, input delivery or qualification results."""


@pytest.fixture
def local_web_records():
    counts = {"pages": 0, "record_reads": 0, "resets": 0, "writes": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def _reply(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/records":
                with lock:
                    counts["record_reads"] += 1
                # The read-back route is independent of browser/DOM state.
                self._reply(json.dumps({"data": []}).encode(), "application/json")
            elif self.path == "/":
                with lock:
                    counts["pages"] += 1
                self._reply(
                    b"<!doctype html><title>Synthetic record form</title>"
                    b"<input name='email'><input name='lname'>"
                    b"<button id='create' onclick=\"fetch('/write',"
                    b"{method:'POST'})\">Create New Record</button>",
                    "text/html; charset=utf-8",
                )
            else:
                self.send_error(404)

        def do_POST(self):
            with lock:
                counts["resets" if self.path == "/reset" else "writes"] += 1
            self._reply(b"{}", "application/json")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.05), daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", counts
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_real_chromium_campaign_binds_observation_before_first_run(
    tmp_path, monkeypatch, local_web_records
):
    origin, counts = local_web_records
    config = _config(
        app={
            "name": "Synthetic form",
            "version": "1",
            "url": origin + "/",
            "headless": True,
            "reset": {"http": {"method": "POST", "url": origin + "/reset"}},
        },
        verifier={
            "kind": "rest",
            "base_url": origin,
            "records_path": "/records",
            "records_key": "data",
        },
    )
    bundle = tmp_path / "source"
    (bundle / "templates").mkdir(parents=True)
    Image.new("RGB", (80, 20), "white").save(bundle / "templates/s3.png")
    _workflow().save(bundle)
    observations = []
    handoffs = []
    real_observe = campaign.observe_campaign_environment

    def retain_real_observation(backend, **kwargs):
        # Observe the real Chromium session; neither the observer nor its
        # measured payload/digest is replaced by the test.
        observer, observed = real_observe(backend, **kwargs)
        assert backend.page.context.browser.version
        assert backend.page.evaluate("location.origin") == origin
        payload = web_environment_payload(
            backend,
            application_version=config.app.version,
            headless=config.app.headless,
        )
        assert observed.environment_digest == web_environment_digest(payload)
        observations.append((observer, observed))
        return observer, observed

    def first_run(**kwargs):
        assert len(observations) == 1
        observer, observed = observations[0]
        workflow = kwargs["workflow"]
        setup = kwargs["setup"]
        assert kwargs["ctx"].case_id == "representative"
        assert kwargs["config"].app.target == "web"
        assert setup.observer is observer
        assert setup.environment == observed
        assert workflow.qualification is not None
        boundary = workflow.qualification.environment
        assert boundary.target_kind == "web"
        assert boundary.application_identity == origin
        assert boundary.environment_digest == observed.environment_digest
        assert boundary.environment_observer_id == observer.observer_id
        assert boundary.environment_observer_contract_sha256 == observer.contract_sha256
        persisted = Workflow.load(kwargs["bundle_dir"])
        assert persisted.qualification is not None
        assert persisted.qualification.environment == boundary
        assert counts["record_reads"] > 0
        assert counts["writes"] == 0
        handoffs.append(kwargs["ctx"].case_id)
        raise _FirstRunReached

    monkeypatch.setattr(
        campaign, "observe_campaign_environment", retain_real_observation
    )
    monkeypatch.setattr(campaign, "_run", first_run)
    with pytest.raises(_FirstRunReached):
        campaign.qualify(
            bundle,
            config,
            out=tmp_path / "campaign",
            cases=["representative"],
            trials=1,
            campaign_id="web-setup-control",
            log=lambda _line: None,
        )
    assert handoffs == ["representative"]
    assert counts["pages"] > 0 and counts["resets"] > 0
    assert counts["record_reads"] > 0 and counts["writes"] == 0
    # This deliberately makes no claim about replay or the later campaign
    # result constructor: the first consumer handoff is the stopping point.
