"""BYOC (bring-your-own-cloud) connector: the engine-side outbound-pull daemon.

Covers the CORE loop end to end with ZERO network and a mocked backend:

* enrollment (register -> per-connector token, persisted 0600);
* dispatch -> execute -> PHI-free callback -> ack, over a stub control plane;
* the governed policy is applied FAIL CLOSED (a dispatch missing the policy /
  the run token is refused; a required grounding rung with no key is refused);
* the callback body is PHI-FREE (no report body, no typed values, only counts +
  a storage path + the immutable bundle binding);
* cross-org isolation: the client only ever presents ITS OWN token and the loop
  only runs the org's own leased job.

The engine's own fail-closed ``run`` admission gate is exercised elsewhere; here
the child ``run`` is a fake so no GUI is launched.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from openadapt_flow.connector import (
    ConnectorClient,
    ConnectorSettings,
    ExecutionResult,
    InMemoryCustomerStorage,
    build_run_argv,
    execute_job,
    load_settings,
    parse_job,
    phi_free_callback_body,
    run_once,
    save_enrollment,
)
from openadapt_flow.connector.executor import RunOutcome
from openadapt_flow.connector.protocol import ByocGovernanceError


# --------------------------------------------------------------------------
# Fixtures: a governed dispatch payload + a fake governed-run child.
# --------------------------------------------------------------------------
def _payload(**overrides):
    payload = {
        "mode": "replay",
        "run_id": "00000000-0000-4000-8000-000000000005",
        "org_id": "org_demo",
        "workflow_id": "wf_demo_1",
        "storage": {
            "backend": "local",
            "bundle_ref": "bundles/wf_demo_1/bundle.zip",
            "report_ref": "org_demo/run_5/report.json",
        },
        "bundle_download_url": None,
        "report_path": "org_demo/run_5/report.json",
        "target_url": "https://emr.internal/app",
        "allowed_hosts": ["cdn.internal"],
        "params": {"vendor": "ACME", "target_kind": "web"},
        "secrets_ref": "emr-secrets",
        "run_token": "a" * 64,
        "bundle_version_id": "bv_1",
        "runtime_validation_id": "rv_1",
        "bundle_sha256": "b" * 64,
        "safety": {"halt.on_ambiguous": True, "effects.verify": True},
        "grounding_model": {"enabled": False, "api_key_env": ""},
    }
    payload.update(overrides)
    return payload


def _fake_success_runner(report):
    def runner(argv, run_dir: Path) -> RunOutcome:
        (run_dir / "report.json").write_text(json.dumps(report))
        return RunOutcome(returncode=0, report=report)

    return runner


SUCCESS_REPORT = {
    "workflow_name": "wf_demo_1",
    "results": [
        {"step_id": "s1", "ok": True, "intent": "click Login"},
        {"step_id": "s2", "ok": True, "intent": "type patient MRN 12345"},
    ],
    "success": True,
    "terminal_outcome": "success",
    "halt": None,
    "heal_count": 0,
    "model_calls": 0,
    "est_model_cost_usd": 0.0,
}


# --------------------------------------------------------------------------
# Governed execution + PHI-free reporting (mocked backend).
# --------------------------------------------------------------------------
def test_execute_job_success_writes_report_to_customer_storage_only():
    job = parse_job(_payload(), lease_job_id="bjob_1")
    settings = ConnectorSettings(profile=None)
    storage = InMemoryCustomerStorage(bundle_dir=Path("/tmp"))

    result = execute_job(
        job, settings, storage, runner=_fake_success_runner(SUCCESS_REPORT)
    )

    assert result.status == "success"
    assert result.metrics["steps"] == 2
    assert result.metrics["steps_ok"] == 2
    # The PHI-bearing report body went to the CUSTOMER store, never returned up.
    assert "org_demo/run_5/report.json" in storage.written
    assert storage.written["org_demo/run_5/report.json"]["success"] is True


def test_callback_body_is_phi_free():
    job = parse_job(_payload(), lease_job_id="bjob_1")
    result = ExecutionResult(
        status="success",
        metrics={"steps": 2, "steps_ok": 2, "halts": 0},
        halt=None,
        report_ref="org_demo/run_5/report.json",
    )
    body = phi_free_callback_body(job, result)

    # The immutable bundle binding the control plane verifies is echoed.
    assert body["bundle_version_id"] == "bv_1"
    assert body["runtime_validation_id"] == "rv_1"
    assert body["bundle_sha256"] == "b" * 64
    assert body["status"] == "success"
    assert body["report_path"] == "org_demo/run_5/report.json"

    # PHI-free: no report body, no results, no typed param VALUES, no target url.
    serialized = json.dumps(body)
    assert "12345" not in serialized  # the patient MRN from the report/params
    assert "ACME" not in serialized
    assert "results" not in body
    assert "emr.internal" not in serialized
    # metrics are structural counts only.
    assert set(body["metrics"]).issubset(
        {"steps", "steps_ok", "halts", "heals", "model_calls", "cost_usd"}
    )


def test_halt_maps_to_halt_status_and_present_flag():
    halt_report = {
        "results": [{"step_id": "s1", "ok": True}],
        "success": False,
        "terminal_outcome": "halt",
        "halt": {"outcome": "halt", "reason": "identity ambiguous for patient row"},
    }
    job = parse_job(_payload(), lease_job_id="bjob_1")
    settings = ConnectorSettings()
    storage = InMemoryCustomerStorage(bundle_dir=Path("/tmp"))

    def runner(argv, run_dir: Path) -> RunOutcome:
        (run_dir / "report.json").write_text(json.dumps(halt_report))
        return RunOutcome(returncode=2, report=halt_report)

    result = execute_job(job, settings, storage, runner=runner)
    assert result.status == "halt"
    body = phi_free_callback_body(job, result)
    assert body["halt"] == {"present": True}
    # The halt REASON free text never crosses to the control plane.
    assert "ambiguous" not in json.dumps(body)


# --------------------------------------------------------------------------
# Fail-closed governance.
# --------------------------------------------------------------------------
def test_dispatch_without_policy_is_refused():
    job = parse_job(_payload(safety={}), lease_job_id="bjob_1")
    with pytest.raises(ByocGovernanceError, match="safety policy"):
        job.ensure_governed()


def test_dispatch_without_run_token_is_refused():
    job = parse_job(_payload(run_token=None), lease_job_id="bjob_1")
    with pytest.raises(ByocGovernanceError, match="callback token"):
        job.ensure_governed()


def test_dispatch_carrying_our_signed_url_is_refused():
    job = parse_job(
        _payload(bundle_download_url="https://our.storage/signed"), lease_job_id="b"
    )
    with pytest.raises(ByocGovernanceError, match="our-owned bundle URL"):
        job.ensure_governed()


def test_execute_refuses_when_required_grounding_key_is_absent(monkeypatch):
    monkeypatch.delenv("BYOC_TEST_GROUNDING_KEY", raising=False)
    job = parse_job(
        _payload(
            grounding_model={
                "enabled": True,
                "api_key_env": "BYOC_TEST_GROUNDING_KEY",
                "model": "claude",
            }
        ),
        lease_job_id="bjob_1",
    )
    settings = ConnectorSettings()
    storage = InMemoryCustomerStorage(bundle_dir=Path("/tmp"))

    # A runner that would "succeed" — the governance gate must refuse BEFORE it.
    called = {"ran": False}

    def runner(argv, run_dir: Path) -> RunOutcome:
        called["ran"] = True
        return RunOutcome(returncode=0, report=SUCCESS_REPORT)

    result = execute_job(job, settings, storage, runner=runner)
    assert result.status == "failed"
    assert "api key env" in result.error
    assert called["ran"] is False  # fail closed: never ran without the rung


def test_governed_run_argv_uses_the_fail_closed_run_verb():
    job = parse_job(_payload(), lease_job_id="bjob_1")
    settings = ConnectorSettings(
        profile="/opt/openadapt/deployment.yaml", policy="clinical-write"
    )
    argv = build_run_argv(job, settings, Path("/b"), Path("/r"), Path("/r/params.json"))
    # It shells the GOVERNED `run` (fail-closed admission), never permissive replay.
    assert argv[1:4] == ["-m", "openadapt_flow", "run"]
    assert "--config" in argv and "/opt/openadapt/deployment.yaml" in argv
    assert "--policy" in argv and "clinical-write" in argv
    assert "--params-file" in argv
    assert "replay" not in argv


# --------------------------------------------------------------------------
# Enrollment + the full loop over a stub control plane (zero network).
# --------------------------------------------------------------------------
class StubControlPlane:
    """An in-process control plane over httpx.MockTransport: a real
    register/poll/ack queue, so the client drives the exact HTTP loop with no
    network. Enforces per-org token scoping (cross-org isolation)."""

    def __init__(self):
        self.tokens = {}  # token -> org_id
        self.jobs = []  # queued jobs (each {id, org_id, payload, status, leased_by})
        self.callbacks = []
        self.acks = []
        self._n = 0

    def enqueue(self, org_id, payload):
        self._n += 1
        self.jobs.append(
            {
                "id": f"bjob_{self._n}",
                "org_id": org_id,
                "payload": payload,
                "status": "queued",
                "leased_by": None,
            }
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        path = request.url.path
        if path == "/api/connector/register":
            token = f"oaconn_{'c' * 64}"
            org = body.get("org_id") or "org_demo"
            self.tokens[token] = org
            return httpx.Response(
                200, json={"connector_id": "conn_1", "org_id": org, "token": token}
            )

        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        org = self.tokens.get(token)

        if path == "/api/connector/poll":
            if org is None:
                return httpx.Response(401, json={"error": "invalid connector token"})
            for j in self.jobs:
                # ISOLATION: a connector only ever leases ITS OWN org jobs.
                if j["status"] == "queued" and j["org_id"] == org:
                    j["status"] = "leased"
                    j["leased_by"] = token
                    return httpx.Response(
                        200, json={"job": {"id": j["id"], "payload": j["payload"]}}
                    )
            return httpx.Response(204)

        if path == "/api/connector/ack":
            if org is None:
                return httpx.Response(401)
            self.acks.append({"token": token, **body})
            return httpx.Response(200, json={"ok": True})

        if path == "/api/internal/run-callback":
            self.callbacks.append({"headers": dict(request.headers), "body": body})
            return httpx.Response(200, json={"ok": True})

        return httpx.Response(404)


def test_enroll_persists_token_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path))
    cp = StubControlPlane()
    transport = httpx.MockTransport(cp.handler)
    client = ConnectorClient("https://app.test", transport=transport)
    data = client.enroll(
        enrollment_secret="s3cret", org_id="org_demo", name="front-desk"
    )
    assert data["token"].startswith("oaconn_")

    settings = ConnectorSettings(
        control_plane_url="https://app.test", token=data["token"], org_id="org_demo"
    )
    path = save_enrollment(settings)
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    # A bare `run` reads it back.
    reloaded = load_settings()
    assert reloaded.token == data["token"]
    assert reloaded.org_id == "org_demo"


def test_full_loop_dispatch_execute_callback_ack():
    cp = StubControlPlane()
    cp.enqueue("org_demo", _payload())
    transport = httpx.MockTransport(cp.handler)
    client = ConnectorClient("https://app.test", token=None, transport=transport)
    client.enroll(enrollment_secret="s", org_id="org_demo", name="n")

    settings = ConnectorSettings(
        control_plane_url="https://app.test",
        org_id="org_demo",
        token=client.token,
        poll_wait_s=0,
    )
    storage = InMemoryCustomerStorage(bundle_dir=Path("/tmp"))
    result = run_once(
        client,
        settings,
        runner=_fake_success_runner(SUCCESS_REPORT),
        storage_factory=lambda job: storage,
    )
    assert result["status"] == "success"

    # A PHI-free callback carrying the run-scoped token was posted.
    assert len(cp.callbacks) == 1
    cb = cp.callbacks[0]
    assert cb["headers"].get("x-run-token") == "a" * 64
    assert cb["body"]["status"] == "success"
    assert "12345" not in json.dumps(cb["body"])  # PHI never crossed

    # The lease was released done.
    assert len(cp.acks) == 1
    assert cp.acks[0]["status"] == "done"


def test_cross_org_isolation_connector_never_sees_another_orgs_job():
    cp = StubControlPlane()
    cp.enqueue("org_B", _payload(org_id="org_B"))  # a DIFFERENT org's job
    transport = httpx.MockTransport(cp.handler)

    # A connector enrolled for org_A.
    client = ConnectorClient("https://app.test", transport=transport)
    client.enroll(enrollment_secret="s", org_id="org_A", name="a")
    settings = ConnectorSettings(
        control_plane_url="https://app.test",
        org_id="org_A",
        token=client.token,
        poll_wait_s=0,
    )

    result = run_once(
        client,
        settings,
        runner=_fake_success_runner(SUCCESS_REPORT),
        storage_factory=lambda job: InMemoryCustomerStorage(bundle_dir=Path("/tmp")),
    )
    assert result is None  # org_A connector sees no work; org_B's job stays queued
    assert cp.jobs[0]["status"] == "queued"
    assert cp.callbacks == []
