"""Pilot hardening of the BYOC connector: the egress allow-list, request
signing, the kill switch, cancellation, concurrency, token rotation, the local
secret store, and admitted wrap programs with an independent read-back.

The central test plants canary values everywhere a record value or secret
could hide (report text, halt reason, exception text, run parameters, the local
secret file) and asserts none of them appears in any byte the connector sent.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import io
import json
import os
import signal
import sys
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from openadapt_flow.connector import egress
from openadapt_flow.connector import executor as executor_mod
from openadapt_flow.connector.client import (
    ConnectorClient,
    ConnectorControl,
    RunsStopped,
    sign_request,
)
from openadapt_flow.connector.config import (
    ConnectorSettings,
    load_settings,
    save_enrollment,
)
from openadapt_flow.connector.daemon import (
    apply_control,
    maybe_rotate,
    run_loop,
)
from openadapt_flow.connector.executor import (
    RunOutcome,
    _subprocess_runner,
    cancel_job,
    execute_job,
    wrap_readback_evidence,
)
from openadapt_flow.connector.protocol import parse_job
from openadapt_flow.connector.secrets import LocalSecretStore, SecretStoreError
from openadapt_flow.connector.storage import InMemoryCustomerStorage
from tests.connector_v3_fixtures import build_program_case

CANARY_RECORD = "ZQX-CANARY-RECORD-7731"
CANARY_LOCAL_ENV_MARKER = "ZQX-CANARY-LOCAL-MARKER-9941"
CANARY_PARAM = "ZQX-CANARY-PARAM-ALICE"
TOKEN = "oaconn_" + "d" * 64


def _zip(entries: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in entries.items():
            archive.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), data)
    return stream.getvalue()


_CASE = None
PLAIN_BUNDLE = b""


@pytest.fixture(scope="module", autouse=True)
def _signed_transport_program(module_durable_authority, tmp_path_factory):
    global _CASE, PLAIN_BUNDLE, WRAP_BUNDLE, ADMISSION, ADMITTER_KEY, VERIFY_YAML
    _CASE = build_program_case(tmp_path_factory.mktemp("connector-pilot-program"))
    PLAIN_BUNDLE = WRAP_BUNDLE = _CASE["bundle_bytes"]
    ADMISSION = (_CASE["bundle_dir"] / "admission.json").read_bytes()
    ADMITTER_KEY = _CASE["public_key_hex"]
    VERIFY_YAML = (_CASE["bundle_dir"] / "readback/verify.yaml").read_bytes()


@pytest.fixture(autouse=True)
def _transport_has_no_external_readback(monkeypatch):
    # Keep wire/secret/lease tests offline. Dedicated v3 tests retain the
    # actual authority/crypto/Flow-parser controls; these tests fake the child.
    monkeypatch.setattr(executor_mod, "_run_flow_tool", lambda *_: 1)


def _configured_settings(**overrides):
    return ConnectorSettings(
        **{
            "control_plane_url": "https://cloud.test",
            "org_id": "org_pilot",
            "trusted_admission_keys": _CASE["public_key_hex"],
            "admission_key_dir": str(_CASE["key_dir"]),
            **overrides,
        }
    )


def _payload(bundle: bytes | None = None, **overrides):
    bundle = PLAIN_BUNDLE if bundle is None else bundle
    payload = {
        "mode": "replay",
        "run_id": "00000000-0000-4000-8000-000000000009",
        "org_id": "org_pilot",
        "workflow_id": "wf_pilot",
        "target_kind": "web",
        "storage": {
            "backend": "local",
            "bundle_ref": "bundles/wf_pilot/bundle.zip",
            "report_ref": "org_pilot/run_9/report.json",
        },
        "target_url": "https://notes.byoc-pilot.example.org/",
        "allowed_hosts": ["notes.byoc-pilot.example.org"],
        "params": {"note": CANARY_PARAM, "target_kind": "web"},
        "secrets_ref": None,
        "run_token": "a" * 64,
        "bundle_version_id": "bv_9",
        "runtime_validation_id": "rv_9",
        "bundle_sha256": hashlib.sha256(bundle).hexdigest(),
        "safety": {"halt.on_ambiguous": True},
        "grounding_model": {"enabled": False},
    }
    payload.update(
        managed_dispatch=_wrap_dispatch(payload["run_id"], _CASE["admission_sha256"]),
        managed_delivery_authority_url="https://cloud.test/api/internal/managed-delivery-permit",
        managed_dispatch_session_id=_CASE["payload"]["managed_dispatch_session_id"],
    )
    payload.update(overrides)
    return payload


class RecordingPlane:
    """A stub control plane that records every raw request byte."""

    def __init__(self, control=None, poll_status=None):
        self.raw: list[bytes] = []
        self.requests: list[httpx.Request] = []
        self.jobs: list[dict] = []
        self.control = control or {
            "max_concurrent_leases": 1,
            "runs_stopped": False,
            "cancel_job_ids": [],
            "rotate_recommended": False,
            "token_expires_at": None,
        }
        self.poll_status = poll_status
        self.lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
            self.raw.append(
                request.method.encode()
                + b" "
                + str(request.url).encode()
                + b"\n"
                + b"\n".join(f"{k}: {v}".encode() for k, v in request.headers.items())
                + b"\n\n"
                + (request.content or b"")
            )
        path = request.url.path
        if path == "/api/connector/poll":
            if self.poll_status == 423:
                return httpx.Response(
                    423,
                    json={
                        "error": "org_runs_stopped",
                        "connector": {**self.control, "runs_stopped": True},
                    },
                )
            with self.lock:
                for job in self.jobs:
                    if job["status"] == "queued":
                        job["status"] = "leased"
                        return httpx.Response(
                            200,
                            json={
                                "job": {"id": job["id"], "payload": job["payload"]},
                                "connector": self.control,
                            },
                        )
            return httpx.Response(
                204, headers={"x-oa-connector-control": json.dumps(self.control)}
            )
        if path == "/api/connector/heartbeat":
            return httpx.Response(200, json={"connector": self.control})
        if path == "/api/connector/rotate":
            return httpx.Response(
                200,
                json={
                    "token": "oaconn_" + "e" * 64,
                    "token_expires_at": None,
                    "previous_token_valid_until": None,
                },
            )
        return httpx.Response(200, json={"ok": True})

    def all_bytes(self) -> bytes:
        return b"\n".join(self.raw)


def _client(plane: RecordingPlane, **kwargs) -> ConnectorClient:
    return ConnectorClient(
        "https://cloud.test",
        token=TOKEN,
        transport=httpx.MockTransport(plane.handler),
        **kwargs,
    )


# --------------------------------------------------------------------------
# The metadata-only boundary, end to end.
# --------------------------------------------------------------------------
def _leaky_report() -> dict:
    """A report that holds record values and a secret in every free-text slot."""
    return {
        "execution_target_kind": "web",
        "success": False,
        "terminal_outcome": "halt",
        "halt": {"reason": f"field showed {CANARY_RECORD}", "typed": CANARY_PARAM},
        "results": [
            {"step_id": "s1", "ok": True, "intent": f"type {CANARY_PARAM}"},
            {
                "step_id": "s2",
                "ok": False,
                "error": f"saw {CANARY_RECORD} and {CANARY_LOCAL_ENV_MARKER}",
            },
        ],
        "ocr": CANARY_RECORD,
        "model_calls": 0,
    }


@pytest.mark.parametrize("outcome_kind", ["halt_report", "child_crash"])
def test_no_canary_leaves_the_machine(tmp_path, outcome_kind):
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    secret_file = secrets_dir / "notes-reader.env"
    secret_file.write_text(f"NOTES_PASSWORD={CANARY_LOCAL_ENV_MARKER}\n")
    os.chmod(secret_file, 0o600)
    plane = RecordingPlane()
    plane.jobs.append(
        {
            "id": "bjob_9",
            "status": "queued",
            "payload": _payload(secrets_ref="notes-reader"),
        }
    )
    client = _client(plane, egress_audit_log=str(tmp_path / "egress.jsonl"))
    settings = _configured_settings(
        control_plane_url="https://cloud.test",
        token=TOKEN,
        poll_wait_s=0,
        secrets_dir=str(secrets_dir),
    )
    seen_env: dict[str, str] = {}

    def runner(argv, run_dir: Path, child_env) -> RunOutcome:
        seen_env.update(child_env)
        if outcome_kind == "child_crash":
            raise RuntimeError(
                f"crashed holding {CANARY_RECORD} {CANARY_LOCAL_ENV_MARKER}"
            )
        report = _leaky_report()
        (run_dir / "report.json").write_text(json.dumps(report))
        return RunOutcome(returncode=1, report=report)

    storage = InMemoryCustomerStorage(bundle_bytes=PLAIN_BUNDLE)
    rc = run_loop(
        client,
        settings,
        runner=runner,
        storage_factory=lambda job: storage,
        once=True,
        log=lambda _m: None,
    )
    assert rc == 0
    # The secret reached only the governed child.
    assert seen_env.get("NOTES_PASSWORD") == CANARY_LOCAL_ENV_MARKER
    # The run reported and acked.
    paths = [r.url.path for r in plane.requests]
    assert "/api/internal/run-callback" in paths and "/api/connector/ack" in paths
    sent = plane.all_bytes() + (tmp_path / "egress.jsonl").read_bytes()
    for canary in (CANARY_RECORD, CANARY_LOCAL_ENV_MARKER, CANARY_PARAM):
        assert canary.encode() not in sent, canary
    # The PHI-bearing report stayed in customer storage.
    if outcome_kind == "halt_report":
        assert CANARY_RECORD in json.dumps(storage.written)
        # Only the stored report's digest left, so Cloud can bind it.
        from openadapt_flow.connector.storage import report_bytes

        callback = next(
            json.loads(r.content)
            for r in plane.requests
            if r.url.path == "/api/internal/run-callback"
        )
        (stored,) = storage.written.values()
        assert (
            callback["metrics"]["report_sha256"]
            == hashlib.sha256(report_bytes(stored)).hexdigest()
        )


def test_guard_callback_refuses_everything_off_the_list():
    base = {
        "run_id": "r1",
        "org_id": "o1",
        "workflow_id": "w1",
        "mode": "replay",
        "status": "failed",
        "report_path": "o1/r1/report.json",
        "metrics": {"steps": 1},
        "error_code": "runner_failure",
    }
    assert egress.guard_callback(base)["status"] == "failed"
    bad = [
        {**base, "error": "free text"},
        {**base, "error_code": "Saw Alice Smith"},
        {**base, "metrics": {"steps": "Alice"}},
        {**base, "metrics": {"patient_name": 1}},
        {**base, "halt": {"present": True, "reason": "x"}},
        {**base, "report_path": "/etc/passwd"},
        {**base, "report_path": "a/../b"},
        {**base, "status": "VERIFIED by Alice"},
        {**base, "outcome": {"outcome": "VERIFIED", "note": "Alice"}},
        {**base, "metrics": {"wrap_readback": {"readback_sha256": "x"}}},
        {**base, "metrics": {"report_sha256": f"report of {CANARY_RECORD}"}},
        {**base, "metrics": {"report_sha256": "A" * 64}},
    ]
    for body in bad:
        with pytest.raises(egress.EgressViolation):
            egress.guard_callback(body)


def test_ack_and_poll_guards():
    assert (
        egress.guard_ack("bjob_1", "failed", "governance_refused")["error_code"]
        == "governance_refused"
    )
    for code in ("Bad Code", "x" * 80, "has space"):
        with pytest.raises(egress.EgressViolation):
            egress.guard_ack("bjob_1", "failed", code)
    with pytest.raises(egress.EgressViolation):
        egress.guard_ack("bjob 1", "done")
    with pytest.raises(egress.EgressViolation):
        egress.guard_poll(10, ["Not A Token"])
    with pytest.raises(egress.EgressViolation):
        egress.guard_poll(10, [], ["id"] * (egress.MAX_ACTIVE_JOBS + 1))


def test_serialize_refuses_any_encoding_of_a_secret():
    with pytest.raises(egress.EgressViolation):
        egress.serialize_checked(
            {"run_id": CANARY_LOCAL_ENV_MARKER}, [CANARY_LOCAL_ENV_MARKER]
        )
    import base64

    encoded = base64.b64encode(CANARY_LOCAL_ENV_MARKER.encode()).decode()
    with pytest.raises(egress.EgressViolation):
        egress.serialize_checked({"x": encoded}, [CANARY_LOCAL_ENV_MARKER])
    assert egress.serialize_checked({"run_id": "r1"}, [CANARY_LOCAL_ENV_MARKER])


# --------------------------------------------------------------------------
# Request signing and replay protection.
# --------------------------------------------------------------------------
def test_requests_are_signed_with_fresh_nonces():
    plane = RecordingPlane()
    client = _client(plane)
    client.poll(0)
    client.poll(0)
    nonces = set()
    for request in plane.requests:
        ts = request.headers["x-oa-connector-ts"]
        nonce = request.headers["x-oa-connector-nonce"]
        assert len(nonce) == 32 and abs(int(ts) - time.time()) < 5
        expected = sign_request(
            TOKEN, "POST", request.url.path, request.content, ts=int(ts), nonce=nonce
        )
        assert hmac.compare_digest(
            expected, request.headers["x-oa-connector-signature"]
        )
        nonces.add(nonce)
    assert len(nonces) == 2
    # The callback uses the run token, not the connector token.
    tampered = sign_request(
        TOKEN, "POST", "/api/connector/poll", b"{}", ts=1, nonce="0" * 32
    )
    assert tampered != sign_request(
        TOKEN, "POST", "/api/connector/ack", b"{}", ts=1, nonce="0" * 32
    )


def test_signature_key_is_the_stored_token_hash():
    key = hashlib.sha256(TOKEN.encode()).hexdigest().encode()
    msg = "\n".join(
        ["v1", "100", "a" * 32, "POST", "/p", hashlib.sha256(b"").hexdigest()]
    ).encode()
    assert (
        sign_request(TOKEN, "post", "/p", b"", ts=100, nonce="a" * 32)
        == hmac.new(key, msg, hashlib.sha256).hexdigest()
    )


# --------------------------------------------------------------------------
# Kill switch, cancellation, concurrency, rotation.
# --------------------------------------------------------------------------
def test_poll_423_raises_runs_stopped_and_cancels_everything(monkeypatch):
    plane = RecordingPlane(poll_status=423)
    client = _client(plane)
    with pytest.raises(RunsStopped):
        client.poll(0)
    assert client.control.runs_stopped
    cancelled: list[str] = []
    monkeypatch.setattr(
        "openadapt_flow.connector.daemon.cancel_job",
        lambda j: cancelled.append(j) or True,
    )
    apply_control(client.control, ["bjob_1", "bjob_2"], lambda _m: None)
    assert sorted(cancelled) == ["bjob_1", "bjob_2"]


def test_cancel_ids_stop_only_listed_jobs(monkeypatch):
    cancelled: list[str] = []
    monkeypatch.setattr(
        "openadapt_flow.connector.daemon.cancel_job",
        lambda j: cancelled.append(j) or True,
    )
    apply_control(
        ConnectorControl(cancel_job_ids=frozenset({"bjob_2"})),
        ["bjob_1", "bjob_2"],
        lambda _m: None,
    )
    assert cancelled == ["bjob_2"]


def test_cancel_job_terminates_the_governed_child(tmp_path):
    started = threading.Event()
    outcome: dict = {}

    def run():
        executor_mod._CURRENT.job_id = "bjob_kill"
        started.set()
        outcome["r"] = _subprocess_runner(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            tmp_path,
            dict(os.environ),
        )

    thread = threading.Thread(target=run)
    began = time.monotonic()
    thread.start()
    started.wait(5)
    for _ in range(50):
        with executor_mod._CHILDREN.lock:
            if "bjob_kill" in executor_mod._CHILDREN.procs:
                break
        time.sleep(0.05)
    assert cancel_job("bjob_kill") is True
    thread.join(15)
    assert not thread.is_alive() and time.monotonic() - began < 15
    assert outcome["r"].returncode != 0


def test_cancelled_job_reports_failed_with_closed_code(tmp_path):
    job = parse_job(_payload(), lease_job_id="bjob_c")
    cancel_job("bjob_c")

    def runner(argv, run_dir, env):  # pragma: no cover - must not run
        raise AssertionError("child started after cancel")

    result = execute_job(
        job,
        _configured_settings(),
        InMemoryCustomerStorage(PLAIN_BUNDLE),
        runner=runner,
    )
    assert (
        result.status == "failed" and result.error_code == "cancelled_by_control_plane"
    )


def test_cancel_that_kills_a_started_child_is_uncertain_not_failed(tmp_path):
    """A cancel after the child started may follow a landed write."""
    job = parse_job(_payload(), lease_job_id="bjob_mid")

    def runner(argv, run_dir, env):
        cancel_job("bjob_mid")  # arrives while the child runs; it is killed
        return RunOutcome(returncode=-15, report={})

    result = execute_job(
        job,
        _configured_settings(),
        InMemoryCustomerStorage(PLAIN_BUNDLE),
        runner=runner,
    )
    assert result.status == "halt"
    assert result.error_code == "cancelled_after_child_start"
    assert result.report_ref is None
    assert result.halt == {"present": True} and result.outcome is None


def test_cancel_after_the_child_reported_keeps_its_real_outcome(tmp_path):
    job = parse_job(_payload(), lease_job_id="bjob_late")

    envelope = json.loads(_VERIFIED_ENVELOPE.read_text())

    def runner(argv, run_dir, env):
        cancel_job("bjob_late")  # too late: the child already finished
        report = {
            "workflow_name": job.workflow_id,
            "started_at": "2026-10-10T00:00:00Z",
            "execution_target_kind": job.target_kind,
            "success": True,
            "execution_profile": envelope["profile"],
            "execution_outcome": envelope["outcome"],
            "execution_completed": envelope["execution_completed"],
            "production_eligible": envelope["production_eligible"],
            "outcome_envelope": envelope,
        }
        return RunOutcome(returncode=0, report=report)

    result = execute_job(
        job,
        _configured_settings(),
        InMemoryCustomerStorage(PLAIN_BUNDLE),
        runner=runner,
    )
    assert result.status == "success" and result.error_code is None
    assert result.outcome is not None
    assert (
        result.outcome["workflow_contract_sha256"]
        == envelope["workflow_contract_sha256"]
    )


@pytest.mark.parametrize(
    "report",
    [
        {"success": True, "execution_outcome": "VERIFIED", "results": [{"ok": True}]},
        {"success": True, "results": [{"ok": True}]},
    ],
    ids=["verified", "legacy"],
)
def test_cancel_that_kills_a_child_after_it_reported_success_is_uncertain(
    tmp_path, report
):
    """Killed after writing success (a seal still running): the write landed."""
    lease = f"bjob_post_{len(report)}"
    job = parse_job(_payload(), lease_job_id=lease)

    def runner(argv, run_dir, env):
        cancel_job(lease)
        return RunOutcome(
            returncode=-15, report={"execution_target_kind": "web", **report}
        )

    result = execute_job(
        job,
        _configured_settings(),
        InMemoryCustomerStorage(PLAIN_BUNDLE),
        runner=runner,
    )
    assert result.status == "halt"
    assert result.error_code == "cancelled_after_child_start"
    assert result.halt == {"present": True} and result.outcome is None


@pytest.mark.parametrize(
    "returncode,report,status,error_code",
    [
        # No cancel: a stop, an OOM kill or a crash after the child started.
        (-2, {}, "halt", "child_outcome_unknown"),
        (
            -9,
            {"execution_outcome": "VERIFIED", "success": True},
            "halt",
            "child_outcome_unknown",
        ),
        (1, {"success": True}, "halt", "child_outcome_unknown"),
        (-15, {"terminal_outcome": "success"}, "halt", "child_outcome_unknown"),
        # A report that records a refusal or a halt keeps its own outcome.
        (
            1,
            {"execution_outcome": "FAILED", "success": False},
            "failed",
            "governed_run_failed",
        ),
        (1, {"execution_outcome": "HALTED", "success": False}, "halt", None),
    ],
    ids=["no-report", "verified", "legacy", "terminal-success", "refused", "halted"],
)
def test_a_nonzero_exit_after_start_is_judged_without_a_cancel(
    tmp_path, returncode, report, status, error_code
):
    job = parse_job(_payload(), lease_job_id=f"bjob_exit_{returncode}_{len(report)}")

    def runner(argv, run_dir, env):
        body = {"execution_target_kind": "web", **report} if report else {}
        return RunOutcome(returncode=returncode, report=body)

    result = execute_job(
        job,
        _configured_settings(),
        InMemoryCustomerStorage(PLAIN_BUNDLE),
        runner=runner,
    )
    assert result.status == status
    assert result.error_code == error_code
    if error_code == "child_outcome_unknown":
        assert result.halt == {"present": True} and result.outcome is None


def test_a_clean_exit_with_no_readable_report_is_uncertain_not_failed(tmp_path):
    """Exit 0 but no report the connector can read (a sealed run's report it
    could not decrypt, or none written): a write may have landed."""
    job = parse_job(_payload(), lease_job_id="bjob_exit0_noreport")

    def runner(argv, run_dir, env):
        return RunOutcome(returncode=0, report={})

    result = execute_job(
        job,
        _configured_settings(),
        InMemoryCustomerStorage(PLAIN_BUNDLE),
        runner=runner,
    )
    assert result.status == "halt"
    assert result.error_code == "child_outcome_unknown"
    assert result.halt == {"present": True} and result.outcome is None


def test_the_report_is_read_through_the_sealed_set(tmp_path, monkeypatch):
    from openadapt_flow import sealed_set

    report = tmp_path / "report.json"
    assert executor_mod._read_report(report) == {}  # missing
    report.write_text("{not json", encoding="utf-8")
    assert executor_mod._read_report(report) == {}  # unreadable
    report.write_text('["a list"]', encoding="utf-8")
    assert executor_mod._read_report(report) == {}  # not an object
    report.write_text('{"success": true}', encoding="utf-8")
    assert executor_mod._read_report(report) == {"success": True}

    # A sealed run's report is decrypted by the sealed set, and a sealed
    # report that can't be read (no key, a bad tag) is unknown, not plaintext.
    monkeypatch.setattr(sealed_set, "is_sealed_path", lambda path: True)
    monkeypatch.setattr(
        sealed_set, "read_text", lambda path, encoding="utf-8": '{"halt": {}}'
    )
    assert executor_mod._read_report(report) == {"halt": {}}

    def refuse(path, encoding="utf-8"):
        raise ValueError("no key for this sealed set")

    monkeypatch.setattr(sealed_set, "read_text", refuse)
    assert executor_mod._read_report(report) == {}


@pytest.mark.skipif(not hasattr(os, "getsid"), reason="POSIX sessions")
@pytest.mark.parametrize("signame", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_a_daemon_stop_mid_run_stops_the_child_and_reports_it_uncertain(
    tmp_path, signame
):
    """Ctrl-C, a service stop, kill or a closed terminal while a real child
    runs: the daemon stops the child and reports halt, never failed."""
    if not hasattr(signal, signame):
        pytest.skip(f"no {signame} here")
    plane = RecordingPlane()
    job_id = f"bjob_stop_{signame.lower()}"
    plane.jobs.append({"id": job_id, "status": "queued", "payload": _payload()})
    started = threading.Event()
    child: dict = {}

    client = _client(plane)

    def interrupt() -> None:
        # The operator's Ctrl-C (or the unit's SIGINT) reaches this process
        # while the child runs. Only this process: the test runner's own
        # process group is left alone.
        # Signal even if the child never started, so a regression fails the
        # assertions below instead of leaving the loop polling forever.
        started.wait(10)
        time.sleep(0.5)
        os.kill(os.getpid(), getattr(signal, signame))

    threading.Thread(target=interrupt, daemon=True).start()

    def runner(argv, run_dir, env):
        real = [sys.executable, "-c", "import time; time.sleep(30)"]
        original = executor_mod.subprocess.Popen

        def spy(*args, **kwargs):
            proc = original(*args, **kwargs)
            child["proc"] = proc
            started.set()
            return proc

        executor_mod.subprocess.Popen = spy
        try:
            return _subprocess_runner(real, run_dir, {"PATH": os.environ["PATH"]})
        finally:
            executor_mod.subprocess.Popen = original

    settings = _configured_settings(
        control_plane_url="https://cloud.test",
        token=TOKEN,
        poll_wait_s=0,
        max_workers=2,
    )
    began = time.monotonic()
    run_loop(
        client,
        settings,
        runner=runner,
        storage_factory=lambda job: InMemoryCustomerStorage(PLAIN_BUNDLE),
        log=lambda _m: None,
    )
    assert time.monotonic() - began < 20  # stopped, not waited out
    assert "proc" in child, "the governed child never started"
    proc = child["proc"]
    assert proc.poll() is not None  # the daemon stopped its child
    acks = [
        json.loads(r.content)
        for r in plane.requests
        if r.url.path == "/api/connector/ack"
    ]
    assert len(acks) == 1 and acks[0]["job_id"] == job_id
    assert acks[0]["status"] != "failed"
    assert acks[0].get("error_code") == "cancelled_after_child_start"


@pytest.mark.skipif(not hasattr(os, "getsid"), reason="POSIX sessions")
def test_the_governed_child_runs_in_its_own_session(tmp_path):
    """A signal to the daemon's process group does not reach the child."""
    seen: dict = {}
    original = executor_mod.subprocess.Popen

    def spy(*args, **kwargs):
        proc = original(*args, **kwargs)
        seen["sid"] = os.getsid(proc.pid)
        return proc

    executor_mod.subprocess.Popen = spy
    try:
        _subprocess_runner([sys.executable, "-c", "pass"], tmp_path, dict(os.environ))
    finally:
        executor_mod.subprocess.Popen = original
    assert seen["sid"] != os.getsid(0)


def test_concurrency_runs_up_to_the_server_cap(tmp_path):
    plane = RecordingPlane(
        control={
            "max_concurrent_leases": 2,
            "runs_stopped": False,
            "cancel_job_ids": [],
            "rotate_recommended": False,
            "token_expires_at": None,
        }
    )
    for n in (1, 2):
        plane.jobs.append(
            {
                "id": f"bjob_{n}",
                "status": "queued",
                "payload": _payload(run_id=f"00000000-0000-4000-8000-00000000000{n}"),
            }
        )
    barrier = threading.Barrier(2, timeout=10)
    polls = {"n": 0}

    class StoppingClient(ConnectorClient):
        def poll(self, wait_s, *, active_job_ids=()):
            polls["n"] += 1
            if polls["n"] > 6:
                raise KeyboardInterrupt
            return super().poll(0, active_job_ids=active_job_ids)

    client = StoppingClient(
        "https://cloud.test", token=TOKEN, transport=httpx.MockTransport(plane.handler)
    )
    client.control = ConnectorControl(max_concurrent_leases=2)

    def runner(argv, run_dir, env):
        barrier.wait()  # both jobs must be running at the same time
        report = {
            "execution_target_kind": "web",
            "success": True,
            "terminal_outcome": "success",
            "results": [],
        }
        return RunOutcome(returncode=0, report=report)

    settings = _configured_settings(
        control_plane_url="https://cloud.test",
        token=TOKEN,
        poll_wait_s=0,
        max_workers=4,
    )
    run_loop(
        client,
        settings,
        runner=runner,
        storage_factory=lambda job: InMemoryCustomerStorage(PLAIN_BUNDLE),
        log=lambda _m: None,
    )
    acks = [
        json.loads(r.content)
        for r in plane.requests
        if r.url.path == "/api/connector/ack"
    ]
    assert sorted(a["job_id"] for a in acks) == ["bjob_1", "bjob_2"]
    assert not barrier.broken


def test_rotation_mints_locally_and_sends_only_a_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path))
    plane = RecordingPlane()
    client = _client(plane)
    client.control.rotate_recommended = True
    settings = _configured_settings(control_plane_url="https://cloud.test", token=TOKEN)
    save_enrollment(settings)
    events: list[str] = []

    def pending(new_token: str) -> None:
        settings.pending_token = new_token
        save_enrollment(settings)
        events.append("pending")

    def persist(new_token: str) -> None:
        assert client.token == TOKEN  # still the old one while persisting
        settings.token, settings.pending_token = new_token, None
        save_enrollment(settings)
        events.append("persisted")

    assert maybe_rotate(client, persist, lambda _m: None, persist_pending=pending)
    assert events == ["pending", "persisted"]
    new = client.token
    assert new != TOKEN and new.startswith("oaconn_")
    rotate = [r for r in plane.requests if r.url.path == "/api/connector/rotate"][0]
    assert json.loads(rotate.content) == {
        "new_token_sha256": hashlib.sha256(new.encode()).hexdigest()
    }
    assert new.encode() not in plane.all_bytes()  # the token itself never crossed
    reloaded = load_settings()
    assert reloaded.token == new and reloaded.pending_token is None
    assert (tmp_path / "connector.toml").stat().st_mode & 0o777 == 0o600
    assert not maybe_rotate(client, persist, lambda _m: None)  # nothing recommended now


def test_lost_rotate_response_is_finished_by_retrying(monkeypatch):
    from openadapt_flow.connector.daemon import finish_rotation

    monkeypatch.setattr("openadapt_flow.connector.daemon.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("lost", request=request)
        return httpx.Response(200, json={"token": None, "already_rotated": True})

    client = ConnectorClient(
        "https://cloud.test", token=TOKEN, transport=httpx.MockTransport(handler)
    )
    saved: list[str] = []
    assert finish_rotation(client, "oaconn_" + "9" * 64, saved.append, lambda _m: None)
    assert saved == ["oaconn_" + "9" * 64] and client.token == saved[0]


# --------------------------------------------------------------------------
# Local secret store.
# --------------------------------------------------------------------------
def test_secret_store_rules(tmp_path):
    store = LocalSecretStore(str(tmp_path))
    assert store.resolve(None) == {}
    f = tmp_path / "app.env"
    f.write_text("# comment\nAPP_PASSWORD=hunter22\n")
    os.chmod(f, 0o644)
    with pytest.raises(SecretStoreError, match="group or others"):
        store.resolve("app")
    os.chmod(f, 0o600)
    assert store.resolve("app") == {"APP_PASSWORD": "hunter22"}
    for bad in ("../app", "a b", ""):
        if bad:
            with pytest.raises(SecretStoreError):
                store.resolve(bad)
    g = tmp_path / "evil.env"
    g.write_text("LD_PRELOAD=/tmp/x.so\n")
    os.chmod(g, 0o600)
    with pytest.raises(SecretStoreError, match="line 1"):
        store.resolve("evil")
    with pytest.raises(SecretStoreError):
        LocalSecretStore(None).resolve("app")


def test_job_naming_unknown_secret_is_refused_before_any_run():
    job = parse_job(_payload(secrets_ref="missing"), lease_job_id="bjob_s")
    result = execute_job(
        job,
        _configured_settings(),
        InMemoryCustomerStorage(PLAIN_BUNDLE),
        runner=lambda *a: (_ for _ in ()).throw(AssertionError("ran")),
    )
    assert result.error_code == "secrets_unavailable"


# --------------------------------------------------------------------------
# Admitted wrap programs: pinned admitter keys and the independent read-back.
# --------------------------------------------------------------------------
ADMISSION = b'{"admission": "signed"}'
VERIFY_YAML = b"verifier: {kind: rest}\n"
WRAP_BUNDLE = _zip(
    {
        "workflow.json": b"{}",
        "admission.json": ADMISSION,
        "readback/verify.yaml": VERIFY_YAML,
        "openadapt-wrap-publish.json": b"{}",
    }
)
ADMITTER_KEY = "f" * 64


def _wrap_dispatch(run_id: str, admission_sha: str) -> dict:
    from openadapt_flow.runner.dispatch_envelope import ManagedDispatchEnvelope
    from openadapt_flow.runner.protocol import dispatch_binding_sha256
    from openadapt_flow.runtime.authorization import GovernedRunAuthorization

    authorization = GovernedRunAuthorization.model_validate(
        _CASE["dispatch"]["authorization"]
    ).model_copy(
        update={"approval_source": "hosted:program-admission:" + admission_sha}
    )
    envelope = ManagedDispatchEnvelope(
        run_id=run_id,
        bundle_content_digest=authorization.bundle_content_digest,
        runtime_inputs_digest=authorization.runtime_inputs_digest,
        authorization=authorization,
        dispatch_binding_sha256=dispatch_binding_sha256(run_id, authorization),
    )
    reference = copy.deepcopy(_CASE["dispatch"]["program_admission"])
    reference["admission_sha256"] = admission_sha
    return {**envelope.model_dump(mode="json"), "program_admission": reference}


def _wrap_job(admission_sha: str | None = None):
    payload = _payload(bundle=WRAP_BUNDLE)
    sha = admission_sha or hashlib.sha256(ADMISSION).hexdigest()
    payload["managed_dispatch"] = _wrap_dispatch(payload["run_id"], sha)
    payload["managed_delivery_authority_url"] = (
        "https://cloud.test/api/internal/managed-delivery-permit"
    )
    return parse_job(payload, lease_job_id="bjob_w")


def _settings(**kw):
    return ConnectorSettings(
        control_plane_url="https://cloud.test", org_id="org_pilot", **kw
    )


def test_unpinned_admitter_key_is_refused():
    result = execute_job(
        _wrap_job(),
        _settings(),
        InMemoryCustomerStorage(WRAP_BUNDLE),
        runner=lambda *a: (_ for _ in ()).throw(AssertionError("ran")),
    )
    assert result.error_code == "admission_key_not_pinned"


def test_dispatched_admission_must_match_the_archive():
    result = execute_job(
        _wrap_job("0" * 64),
        _settings(trusted_admission_keys=ADMITTER_KEY),
        InMemoryCustomerStorage(WRAP_BUNDLE),
        runner=lambda *a: (_ for _ in ()).throw(AssertionError("ran")),
    )
    assert result.error_code == "program_admission_refused"


def test_admitted_wrap_run_stages_key_and_reports_readback(monkeypatch):
    calls: list[list[str]] = []

    def fake_tool(argv, env):
        calls.append(argv)
        if "--capture-baseline" in argv:
            Path(argv[argv.index("--capture-baseline") + 1]).write_text(
                json.dumps({"records": [CANARY_RECORD]})
            )
        else:
            out = Path(argv[argv.index("--out") + 1])
            (out / "effect-receipt.json").write_text(
                json.dumps(
                    {
                        "outcome": "verified",
                        "effect_verdict": "CONFIRMED",
                        "verification_tier": "independent_readback",
                        "records": [CANARY_RECORD],
                    }
                )
            )
        return 0

    monkeypatch.setattr(executor_mod, "_run_flow_tool", fake_tool)
    seen: dict = {}

    def runner(argv, run_dir, env):
        key_dir = Path(env["OPENADAPT_JIT_KEY_DIR"])
        seen["key"] = (key_dir / "keys" / "ed25519.pub").read_text().strip()
        seen["dispatch"] = json.loads(
            Path(argv[argv.index("--managed-dispatch-file") + 1]).read_text()
        )
        report = {
            "execution_target_kind": "web",
            "success": True,
            "execution_outcome": "VERIFIED",
            "results": [{"ok": True}],
        }
        return RunOutcome(returncode=0, report=report)

    job = _wrap_job()
    result = execute_job(
        job,
        _settings(trusted_admission_keys=ADMITTER_KEY),
        InMemoryCustomerStorage(WRAP_BUNDLE),
        runner=runner,
    )
    assert seen["key"] == ADMITTER_KEY
    assert "program_admission" not in seen["dispatch"]  # only the six Flow keys
    assert len(calls) == 2 and "--authorization-ref" in calls[1]
    readback = result.metrics["wrap_readback"]
    assert readback["readback_sha256"] == hashlib.sha256(VERIFY_YAML).hexdigest()
    assert readback["effect_verdict"] == "CONFIRMED"
    body = egress.guard_callback(
        {
            "run_id": job.run_id,
            "org_id": job.org_id,
            "workflow_id": job.workflow_id,
            "status": result.status,
            "metrics": result.metrics,
            "report_path": result.report_ref,
        }
    )
    assert CANARY_RECORD not in json.dumps(body)


def test_readback_evidence_is_closed():
    good = json.dumps(
        {
            "outcome": "verified",
            "effect_verdict": "CONFIRMED",
            "verification_tier": "independent_readback",
        }
    ).encode()
    assert wrap_readback_evidence(good, "a" * 64)["outcome"] == "verified"
    bad = json.dumps(
        {
            "outcome": "verified by Alice",
            "effect_verdict": "CONFIRMED",
            "verification_tier": "x",
        }
    ).encode()
    assert wrap_readback_evidence(bad, "a" * 64) is None
    assert wrap_readback_evidence(good, "") is None


_VERIFIED_ENVELOPE = (
    Path(__file__).parent / "fixtures" / "connector_outcome_envelope_verified.json"
)


def test_a_verified_outcome_envelope_passes_the_egress_guard():
    """A real VERIFIED envelope (staging, synthetic) carries digest-named keys."""
    from openadapt_flow.connector import egress

    envelope = json.loads(_VERIFIED_ENVELOPE.read_text())
    body = {
        "run_id": "run_1",
        "org_id": "org_1",
        "workflow_id": "wf_1",
        "status": "success",
        "report_path": "org_1/run_1/report.json",
        "metrics": {"steps": 3},
        "mode": "replay",
        "outcome": envelope,
    }
    sent = egress.guard_callback(body)
    assert sent["outcome"]["outcome"] == "VERIFIED"
    assert (
        sent["outcome"]["workflow_contract_sha256"]
        == envelope["workflow_contract_sha256"]
    )


def test_the_outcome_envelope_still_refuses_free_text():
    from openadapt_flow.connector import egress

    def body(envelope):
        return {
            "run_id": "run_1",
            "org_id": "org_1",
            "workflow_id": "wf_1",
            "status": "success",
            "report_path": "org_1/run_1/report.json",
            "metrics": {"steps": 3},
            "mode": "replay",
            "outcome": envelope,
        }

    # A record value in a string leaf is refused.
    envelope = json.loads(_VERIFIED_ENVELOPE.read_text())
    envelope["postcondition_evidence"][0]["action_kind"] = f"type {CANARY_RECORD}"
    with pytest.raises(egress.EgressViolation):
        egress.guard_callback(body(envelope))

    # An unknown key is refused or dropped by the closed model; it never leaves.
    envelope = json.loads(_VERIFIED_ENVELOPE.read_text())
    envelope["required_contracts"][f"note {CANARY_RECORD}"] = 1
    try:
        sent = egress.guard_callback(body(envelope))
    except egress.EgressViolation:
        return
    assert CANARY_RECORD not in json.dumps(sent)


def test_local_report_digest_matches_the_stored_bytes(tmp_path):
    from openadapt_flow.connector.storage import LocalCustomerStorage, report_bytes

    report = {"success": True, "note": CANARY_RECORD}
    storage = LocalCustomerStorage(str(tmp_path))
    written = storage.write_report("org_1/run_1/report.json", report)
    assert Path(written).read_bytes() == report_bytes(report)
    assert egress.guard_callback(
        {
            "run_id": "run_1",
            "org_id": "org_1",
            "workflow_id": "wf_1",
            "status": "success",
            "report_path": "org_1/run_1/report.json",
            "metrics": {
                "steps": 1,
                "report_sha256": hashlib.sha256(report_bytes(report)).hexdigest(),
            },
            "mode": "replay",
        }
    )["metrics"]["report_sha256"]


def test_wrap_readback_evidence_reads_the_v1_effect_receipt():
    """verify-run nests the closed labels under "receipt" (effect-receipt/v1)."""
    from openadapt_flow.connector.executor import wrap_readback_evidence

    receipt = json.dumps(
        {
            "schema_version": "openadapt.effect-receipt/v1",
            "receipt": {
                "outcome": "verified",
                "effect_verdict": "CONFIRMED",
                "verification_tier": "system_of_record",
            },
            "evidence": {"checked": {"frame": "confirmed"}},
        }
    ).encode()
    evidence = wrap_readback_evidence(receipt, "a" * 64)
    assert evidence == {
        "readback_sha256": "a" * 64,
        "outcome": "verified",
        "effect_verdict": "CONFIRMED",
        "verification_tier": "system_of_record",
        "receipt_sha256": hashlib.sha256(receipt).hexdigest(),
    }
    # A free-text label is still refused.
    bad = json.loads(receipt)
    bad["receipt"]["outcome"] = f"saw {CANARY_RECORD}"
    assert wrap_readback_evidence(json.dumps(bad).encode(), "a" * 64) is None
