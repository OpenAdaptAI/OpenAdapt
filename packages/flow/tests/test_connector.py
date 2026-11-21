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

import copy
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import httpx
import pytest

from openadapt_flow.connector import (
    MANAGED_QUALIFICATION_AUTHORITY_CAPABILITY,
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
    status_from_report,
)
from openadapt_flow.connector.executor import RunOutcome, precise_outcome_from_report
from openadapt_flow.connector.protocol import (
    FLOW_DISPATCH_WIRE_KEYS,
    ByocGovernanceError,
)
from openadapt_flow.connector.storage import LocalCustomerStorage
from openadapt_flow.failure_signals import (
    AUTOMATION_FAILURE_SIGNAL_SCHEMA,
    automation_failure_signal,
)
from openadapt_flow.runner.dispatch_envelope import ManagedDispatchEnvelope
from openadapt_flow.runner.protocol import dispatch_binding_sha256
from openadapt_flow.runtime.authorization import GovernedRunAuthorization
from openadapt_flow.runtime.durable.authority import (
    REMOTE_AUTHORITY_TOKEN_ENV,
    REMOTE_AUTHORITY_URL_ENV,
    REMOTE_DISPATCH_SESSION_ID_ENV,
)
from tests.connector_v3_fixtures import build_program_case

_CASE = None
_BUNDLE_BYTES = b""
_BUNDLE_SHA256 = ""


@pytest.fixture(scope="module", autouse=True)
def _signed_transport_program(module_durable_authority, tmp_path_factory):
    # Transport tests still fake the governed child. Their bundle now carries
    # real signatures instead of inheriting live authority from a legacy mock.
    global _CASE, _BUNDLE_BYTES, _BUNDLE_SHA256
    _CASE = build_program_case(tmp_path_factory.mktemp("connector-program"))
    _BUNDLE_BYTES = _CASE["bundle_bytes"]
    _BUNDLE_SHA256 = hashlib.sha256(_BUNDLE_BYTES).hexdigest()


@pytest.fixture(autouse=True)
def _transport_has_no_external_readback(monkeypatch):
    # The old transport fixture had no readback file. Keep these wire/report
    # tests offline while leaving signature, registry and envelope gates real.
    monkeypatch.setattr(
        "openadapt_flow.connector.executor._run_flow_tool", lambda *_: 1
    )


def _settings(**overrides):
    return ConnectorSettings(
        **{
            "control_plane_url": "https://app.test",
            "org_id": "org_demo",
            "trusted_admission_keys": _CASE["public_key_hex"],
            "admission_key_dir": str(_CASE["key_dir"]),
            **overrides,
        }
    )


# --------------------------------------------------------------------------
# Fixtures: a governed dispatch payload + a fake governed-run child.
# --------------------------------------------------------------------------
def _payload(**overrides):
    payload = {
        "mode": "replay",
        "run_id": "00000000-0000-4000-8000-000000000005",
        "org_id": "org_demo",
        "workflow_id": "wf_demo_1",
        "target_kind": "web",
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
        "secrets_ref": None,
        "run_token": "a" * 64,
        "bundle_version_id": "bv_1",
        "runtime_validation_id": "rv_1",
        "bundle_sha256": _BUNDLE_SHA256,
        "safety": {"halt.on_ambiguous": True, "effects.verify": True},
        "grounding_model": {"enabled": False, "api_key_env": ""},
    }
    payload.update(
        managed_dispatch=_managed_dispatch(),
        managed_delivery_authority_url="https://app.test/api/internal/managed-delivery-permit",
        managed_dispatch_session_id=_CASE["payload"]["managed_dispatch_session_id"],
    )
    payload.update(overrides)
    return payload


def _managed_dispatch(*, run_id: str | None = None) -> dict:
    exact_run_id = run_id or "00000000-0000-4000-8000-000000000005"
    authorization = GovernedRunAuthorization.model_validate(
        _CASE["dispatch"]["authorization"]
    )
    envelope = ManagedDispatchEnvelope(
        run_id=exact_run_id,
        bundle_content_digest=authorization.bundle_content_digest,
        runtime_inputs_digest=authorization.runtime_inputs_digest,
        authorization=authorization,
        dispatch_binding_sha256=dispatch_binding_sha256(exact_run_id, authorization),
    )
    return {
        **envelope.model_dump(mode="json"),
        "program_admission": copy.deepcopy(_CASE["dispatch"]["program_admission"]),
    }


def _wire(dispatch: dict) -> dict:
    return {key: dispatch[key] for key in FLOW_DISPATCH_WIRE_KEYS}


def _fake_success_runner(report):
    def runner(argv, run_dir: Path, _env) -> RunOutcome:
        (run_dir / "report.json").write_text(json.dumps(report))
        return RunOutcome(returncode=0, report=report)

    return runner


SUCCESS_REPORT = {
    "workflow_name": "wf_demo_1",
    "execution_target_kind": "web",
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
    settings = _settings(profile=None)
    storage = InMemoryCustomerStorage(bundle_bytes=_BUNDLE_BYTES)

    result = execute_job(
        job, settings, storage, runner=_fake_success_runner(SUCCESS_REPORT)
    )

    assert result.status == "success"
    assert result.metrics["steps"] == 2
    assert result.metrics["steps_ok"] == 2
    # The PHI-bearing report body went to the CUSTOMER store, never returned up.
    assert "org_demo/run_5/report.json" in storage.written
    assert storage.written["org_demo/run_5/report.json"]["success"] is True


def test_execute_job_consumes_exact_managed_dispatch_through_private_child_boundary(
    monkeypatch,
):
    authority_url = "https://app.test/api/internal/managed-delivery-permit"
    token = "a" * 64
    managed_dispatch = _managed_dispatch()
    monkeypatch.setenv(REMOTE_DISPATCH_SESSION_ID_ENV, "stale-parent-session")
    monkeypatch.setenv(REMOTE_AUTHORITY_URL_ENV, "https://stale.invalid/permit")
    monkeypatch.setenv(REMOTE_AUTHORITY_TOKEN_ENV, "stale-parent-token")
    job = parse_job(
        _payload(
            managed_dispatch=managed_dispatch,
            managed_delivery_authority_url=authority_url,
        ),
        lease_job_id="bjob_1",
    )
    storage = InMemoryCustomerStorage(bundle_bytes=_BUNDLE_BYTES)
    observed: dict[str, object] = {}

    def runner(argv, run_dir: Path, child_env) -> RunOutcome:
        envelope_path = Path(argv[argv.index("--managed-dispatch-file") + 1])
        observed["argv"] = argv
        observed["mode"] = stat.S_IMODE(envelope_path.stat().st_mode)
        observed["envelope"] = json.loads(envelope_path.read_text())
        observed["authority_url"] = child_env[REMOTE_AUTHORITY_URL_ENV]
        observed["authority_token"] = child_env[REMOTE_AUTHORITY_TOKEN_ENV]
        observed["session_id"] = child_env[REMOTE_DISPATCH_SESSION_ID_ENV]
        observed["parent_token"] = os.environ.get(REMOTE_AUTHORITY_TOKEN_ENV)
        (run_dir / "report.json").write_text(json.dumps(SUCCESS_REPORT))
        return RunOutcome(returncode=0, report=SUCCESS_REPORT)

    result = execute_job(job, _settings(), storage, runner=runner)

    assert result.status == "success"
    assert observed["mode"] == 0o600
    assert observed["envelope"] == _wire(managed_dispatch)
    assert observed["authority_url"] == authority_url
    assert observed["authority_token"] == token
    assert observed["session_id"] == job.managed_dispatch_session_id
    assert observed["parent_token"] == "stale-parent-token"
    assert token not in json.dumps(observed["argv"])
    assert token not in json.dumps(observed["envelope"])


@pytest.mark.parametrize("require_run_token", [True, False])
def test_legacy_payload_cannot_inherit_live_authority(monkeypatch, require_run_token):
    monkeypatch.setenv(REMOTE_AUTHORITY_URL_ENV, "https://stale.invalid/permit")
    monkeypatch.setenv(REMOTE_AUTHORITY_TOKEN_ENV, "stale-parent-token")
    monkeypatch.setenv(REMOTE_DISPATCH_SESSION_ID_ENV, "stale-parent-session")
    job = parse_job(
        _payload(
            managed_dispatch=None,
            managed_delivery_authority_url=None,
            managed_dispatch_session_id=None,
        ),
        lease_job_id="bjob_1",
    )
    storage = InMemoryCustomerStorage(bundle_bytes=_BUNDLE_BYTES)
    calls = []

    def fetch(*_):
        calls.append("fetch")
        raise AssertionError("legacy payload must not fetch live bytes")

    monkeypatch.setattr(storage, "fetch_bundle_archive", fetch)
    result = execute_job(
        job,
        _settings(),
        storage,
        runner=lambda *_: calls.append("run"),
        require_run_token=require_run_token,
    )
    assert result.status == "failed"
    assert result.error_code == "governance_refused"
    assert calls == []


def test_execute_job_refuses_child_report_for_different_substrate():
    job = parse_job(_payload(), lease_job_id="bjob_1")
    settings = _settings(profile=None)
    storage = InMemoryCustomerStorage(bundle_bytes=_BUNDLE_BYTES)
    mismatched = {**SUCCESS_REPORT, "execution_target_kind": "citrix"}

    result = execute_job(
        job, settings, storage, runner=_fake_success_runner(mismatched)
    )

    assert result.status == "failed"
    assert result.error is not None
    assert "does not match" in result.error


def test_malformed_child_report_never_becomes_success():
    assert status_from_report(0, {"success": "true"}) == "failed"
    assert status_from_report(0, {"success": True, "halt": {}}) == "halt"


def test_callback_body_is_phi_free():
    job = parse_job(_payload(), lease_job_id="bjob_1")
    result = ExecutionResult(
        status="success",
        metrics={"steps": 2, "steps_ok": 2, "halts": 0},
        halt=None,
        report_ref="org_demo/run_5/report.json",
        verified_bundle_sha256=_BUNDLE_SHA256,
    )
    body = phi_free_callback_body(job, result)

    # The immutable bundle binding the control plane verifies is echoed.
    assert body["bundle_version_id"] == "bv_1"
    assert body["runtime_validation_id"] == "rv_1"
    assert body["bundle_sha256"] == _BUNDLE_SHA256
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


def test_customer_controlled_failure_signal_is_closed_and_optional(monkeypatch):
    job = parse_job(_payload(target_kind="citrix"), lease_job_id="bjob_1")
    report = {
        "execution_target_kind": "citrix",
        "execution_profile": "regulated",
        "execution_outcome": "HALTED",
        "external_network_calls": "none",
        "model_calls": 0,
        "results": [
            {
                "step_id": "patient-Jane-Doe",
                "ok": False,
                "error": "ambiguous patient Jane Doe MRN 12345",
                "resolution": {"rung": "ocr", "point": {"x": 1, "y": 2}},
            }
        ],
    }
    signal = automation_failure_signal(report, "halt", "citrix")
    body = phi_free_callback_body(
        job,
        ExecutionResult(
            "halt", {}, {"reason": "Jane Doe"}, job.report_ref(), failure_signal=signal
        ),
    )
    serialized = json.dumps(body)
    assert body["failure_signal"]["failure_kind"] == "resolution_ambiguous"
    assert body["failure_signal"]["substrate"] == "citrix"
    assert "Jane Doe" not in serialized
    assert "12345" not in serialized
    assert "results" not in serialized

    monkeypatch.setenv("DO_NOT_TRACK", "1")
    body = phi_free_callback_body(
        job,
        ExecutionResult("halt", {}, None, job.report_ref(), failure_signal=signal),
    )
    assert "failure_signal" not in body


def test_customer_controlled_callback_carries_exact_outcome_envelope():
    report = {
        "workflow_name": "wf",
        "started_at": "2026-07-26T19:00:00Z",
        "execution_profile": "standard",
        "execution_outcome": "HALTED",
        "execution_completed": False,
        "production_eligible": False,
        "model_calls": 0,
        "external_network_calls": "none",
        "outcome_envelope": {
            "version": "openadapt.execution-outcome/v1",
            "outcome": "HALTED",
            "profile": "standard",
            "production_eligible": False,
            "execution_completed": False,
            "required_contracts": {
                "authorization": 1,
                "identity": 1,
                "postcondition": 0,
                "effect": 1,
            },
            "passed_contracts": {
                "authorization": 1,
                "identity": 0,
                "postcondition": 0,
                "effect": 0,
            },
            "evidence_classes": ["authorization"],
            "model_calls": 0,
            "external_network_calls": "none",
            "compensation_actions": 0,
        },
    }
    assert status_from_report(2, report) == "halt"
    job = parse_job(_payload(), lease_job_id="bjob_1")
    result = ExecutionResult(
        "halt",
        {},
        None,
        job.report_ref(),
        outcome=precise_outcome_from_report(report),
    )
    assert phi_free_callback_body(job, result)["outcome"]["outcome"] == "HALTED"


def test_halt_maps_to_halt_status_and_present_flag():
    halt_report = {
        "execution_target_kind": "web",
        "results": [{"step_id": "s1", "ok": True}],
        "success": False,
        "terminal_outcome": "halt",
        "halt": {"outcome": "halt", "reason": "identity ambiguous for patient row"},
    }
    job = parse_job(_payload(), lease_job_id="bjob_1")
    settings = _settings()
    storage = InMemoryCustomerStorage(bundle_bytes=_BUNDLE_BYTES)

    def runner(argv, run_dir: Path, _env) -> RunOutcome:
        (run_dir / "report.json").write_text(json.dumps(halt_report))
        return RunOutcome(returncode=2, report=halt_report)

    result = execute_job(job, settings, storage, runner=runner)
    assert result.status == "halt"
    body = phi_free_callback_body(job, result)
    assert body["halt"] == {"present": True}
    # The halt REASON free text never crosses to the control plane.
    assert "ambiguous" not in json.dumps(body)


class _FailingReportStorage(InMemoryCustomerStorage):
    """Customer storage that serves the bundle but cannot persist a report."""

    def write_report(self, ref, report):  # noqa: ANN001, ANN201
        raise OSError("customer bucket unavailable")


VERIFIED_REPORT = {
    **SUCCESS_REPORT,
    "started_at": "2026-10-10T00:00:00Z",
    "execution_profile": "standard",
    "execution_outcome": "VERIFIED",
    "execution_completed": True,
    "production_eligible": True,
    "outcome_envelope": {
        "version": "openadapt.execution-outcome/v1",
        "outcome": "VERIFIED",
        "profile": "standard",
        "production_eligible": True,
        "execution_completed": True,
        "required_contracts": {
            "authorization": 1,
            "identity": 1,
            "postcondition": 0,
            "effect": 1,
        },
        "passed_contracts": {
            "authorization": 1,
            "identity": 1,
            "postcondition": 0,
            "effect": 1,
        },
        "evidence_classes": ["authorization", "identity", "effect_tier_1"],
        "model_calls": 0,
        "external_network_calls": "none",
        "compensation_actions": 0,
    },
}


@pytest.mark.parametrize("report", [SUCCESS_REPORT, VERIFIED_REPORT])
def test_report_write_failure_does_not_report_completed_run_as_runner_failure(
    report,
):
    """A run that completed (even VERIFIED) is never reported as a runner
    failure because the customer store could not keep its report: Cloud
    would show Failed and an operator re-run would repeat the write."""
    assert precise_outcome_from_report(VERIFIED_REPORT) is not None
    job = parse_job(_payload(), lease_job_id="bjob_1")
    storage = _FailingReportStorage(bundle_bytes=_BUNDLE_BYTES)

    result = execute_job(job, _settings(), storage, runner=_fake_success_runner(report))
    body = phi_free_callback_body(job, result)

    assert body["status"] == "halt"
    assert body["error_code"] is None
    # Never a path to an object that was not written.
    assert body["report_path"] is None
    # No VERIFIED claim without a durable report; the halt is present.
    assert "outcome" not in body
    assert body["halt"] == {"present": True}
    assert result.error == "customer-storage report write failed: OSError"


def test_report_write_failure_keeps_child_failure_as_failed():
    failed_report = {**SUCCESS_REPORT, "success": False, "terminal_outcome": "failed"}
    job = parse_job(_payload(), lease_job_id="bjob_1")
    storage = _FailingReportStorage(bundle_bytes=_BUNDLE_BYTES)

    def runner(argv, run_dir: Path, _env) -> RunOutcome:
        return RunOutcome(returncode=1, report=failed_report)

    result = execute_job(job, _settings(), storage, runner=runner)
    body = phi_free_callback_body(job, result)
    assert body["status"] == "failed"
    assert body["report_path"] is None


@pytest.mark.parametrize(
    "phase", ["before_child", "before_child_egress", "verified", "failed_after_write"]
)
def test_egress_refusal_reports_whether_the_child_could_have_written(
    tmp_path, monkeypatch, phase
):
    cp = StubControlPlane()
    payload = _payload()
    # An unsafe planned report key reaches the guard only if the child wrote a report.
    before_child = phase in {"before_child", "before_child_egress"}
    if phase != "verified":
        payload["storage"]["report_ref"] = "synthetic private report"
    if before_child:
        payload["safety"] = {}
    cp.enqueue("org_demo", payload)
    client = ConnectorClient(
        "https://app.test", transport=httpx.MockTransport(cp.handler)
    )
    client.enroll(enrollment_secret="s", org_id="org_demo", name="n")
    client.add_secret_values(["effect_tier_1"])
    if phase == "before_child_egress":
        # Exercise a real pre-child egress refusal in the optional failure signal.
        monkeypatch.delenv("DO_NOT_TRACK", raising=False)
        monkeypatch.setenv("OPENADAPT_TELEMETRY", "1")
        client.add_secret_values([AUTOMATION_FAILURE_SIGNAL_SCHEMA])
    record = tmp_path / "synthetic-record.txt"

    def runner(argv, run_dir, env):
        with record.open("a") as stream:
            stream.write("saved\n")
        report = (
            VERIFIED_REPORT
            if phase == "verified"
            else {**SUCCESS_REPORT, "success": False}
        )
        return RunOutcome(0 if phase == "verified" else 1, report)

    result = run_once(
        client,
        _settings(),
        runner=runner,
        storage_factory=lambda job: InMemoryCustomerStorage(bundle_bytes=_BUNDLE_BYTES),
    )

    expected_code = "governance_refused" if phase == "before_child" else "egress_refused"
    assert result["callback_accepted"]
    assert len(cp.callbacks) == len(cp.acks) == 1
    assert result["error_code"] == cp.acks[0]["error_code"] == expected_code
    callback = cp.callbacks[0]["body"]
    assert callback["report_path"] is None and callback["metrics"] == {}
    assert "outcome" not in callback
    if expected_code == "egress_refused":
        assert "failure_signal" not in callback
    if before_child:
        assert not record.exists()
        assert (
            result["status"] == callback["status"] == cp.acks[0]["status"] == "failed"
        )
        assert callback["error_code"] == "runner_failure"
        assert "bundle_sha256" not in callback and "halt" not in callback
    else:
        assert record.read_text().splitlines() == ["saved"]
        assert result["status"] == callback["status"] == "halt"
        assert callback["halt"] == {"present": True}
        assert callback["bundle_sha256"] == _BUNDLE_SHA256
        assert callback["error_code"] == "egress_refused"
        assert cp.acks[0]["status"] == "done"


def test_report_write_failure_lease_is_not_acked_failed():
    cp = StubControlPlane()
    cp.enqueue("org_demo", _payload())
    transport = httpx.MockTransport(cp.handler)
    client = ConnectorClient("https://app.test", token=None, transport=transport)
    client.enroll(enrollment_secret="s", org_id="org_demo", name="n")
    settings = _settings(
        control_plane_url="https://app.test",
        org_id="org_demo",
        token=client.token,
        poll_wait_s=0,
    )

    result = run_once(
        client,
        settings,
        runner=_fake_success_runner(VERIFIED_REPORT),
        storage_factory=lambda job: _FailingReportStorage(bundle_bytes=_BUNDLE_BYTES),
    )

    assert result["status"] == "halt"
    assert cp.callbacks[0]["body"]["error_code"] is None
    assert cp.acks[0]["status"] == "done"


# --------------------------------------------------------------------------
# Fail-closed governance.
# --------------------------------------------------------------------------
def test_dispatch_without_policy_is_refused():
    job = parse_job(_payload(safety={}), lease_job_id="bjob_1")
    with pytest.raises(ByocGovernanceError, match="safety policy"):
        job.ensure_governed()


def test_dispatch_without_first_class_target_kind_is_refused():
    payload = _payload()
    payload.pop("target_kind")
    with pytest.raises(ValueError, match="connector contract"):
        parse_job(payload, lease_job_id="bjob_1")


def test_dispatch_without_exact_bundle_archive_digest_is_refused():
    payload = _payload()
    payload.pop("bundle_sha256")
    with pytest.raises(ValueError, match="connector contract"):
        parse_job(payload, lease_job_id="bjob_1")


@pytest.mark.parametrize("digest", ["B" * 64, "b" * 63, "not-a-digest"])
def test_dispatch_with_noncanonical_bundle_archive_digest_is_refused(digest):
    with pytest.raises(ValueError, match="connector contract"):
        parse_job(_payload(bundle_sha256=digest), lease_job_id="bjob_1")


def test_dispatch_with_unknown_target_kind_is_refused():
    with pytest.raises(ValueError, match="connector contract"):
        parse_job(_payload(target_kind="mainframe"), lease_job_id="bjob_1")


def test_compatibility_target_kind_must_equal_first_class_value():
    job = parse_job(
        _payload(
            target_kind="citrix",
            target_url=None,
            allowed_hosts=[],
            params={"vendor": "ACME", "target_kind": "rdp"},
        ),
        lease_job_id="bjob_1",
    )
    with pytest.raises(ByocGovernanceError, match="disagrees"):
        job.ensure_governed()


def test_compatibility_target_kind_is_optional():
    job = parse_job(
        _payload(params={"vendor": "ACME"}),
        lease_job_id="bjob_1",
    )
    job.ensure_governed()


@pytest.mark.parametrize("target_kind", ["windows", "macos", "linux", "rdp", "citrix"])
def test_native_remote_dispatch_refuses_browser_boundary(target_kind):
    job = parse_job(
        _payload(target_kind=target_kind, params={"target_kind": target_kind}),
        lease_job_id="bjob_1",
    )
    with pytest.raises(ByocGovernanceError, match="browser target boundary"):
        job.ensure_governed()


def test_dispatch_without_run_token_is_refused():
    job = parse_job(_payload(run_token=None), lease_job_id="bjob_1")
    with pytest.raises(ByocGovernanceError, match="callback token"):
        job.ensure_governed()


def test_managed_dispatch_for_different_run_is_refused():
    job = parse_job(
        _payload(
            managed_dispatch=_managed_dispatch(
                run_id="00000000-0000-4000-8000-000000000006"
            ),
            managed_delivery_authority_url=(
                "https://app.openadapt.ai/api/internal/managed-delivery-permit"
            ),
        ),
        lease_job_id="bjob_1",
    )
    with pytest.raises(ByocGovernanceError, match="different run"):
        job.ensure_governed()


def test_execute_refuses_managed_authority_on_a_different_https_origin():
    job = parse_job(
        _payload(
            managed_dispatch=_managed_dispatch(),
            managed_delivery_authority_url=(
                "https://attacker.invalid/api/internal/managed-delivery-permit"
            ),
        ),
        lease_job_id="bjob_1",
    )
    result = execute_job(
        job,
        _settings(control_plane_url="https://app.openadapt.ai"),
        InMemoryCustomerStorage(bundle_bytes=_BUNDLE_BYTES),
        runner=_fake_success_runner(SUCCESS_REPORT),
    )
    assert result.status == "failed"
    assert "pinned to the enrolled control plane" in (result.error or "")


@pytest.mark.parametrize(
    "overrides, match",
    [
        (
            {"managed_delivery_authority_url": None},
            "must be supplied together",
        ),
        (
            {
                "managed_delivery_authority_url": "http://localhost/permit",
            },
            "credential-free HTTPS endpoint",
        ),
    ],
)
def test_incomplete_or_unsafe_managed_authority_is_refused(overrides, match):
    job = parse_job(_payload(**overrides), lease_job_id="bjob_1")
    with pytest.raises(ByocGovernanceError, match=match):
        job.ensure_governed()


@pytest.mark.parametrize("field", ["bundle_version_id", "runtime_validation_id"])
def test_dispatch_without_immutable_validation_binding_is_refused(field):
    job = parse_job(_payload(**{field: None}), lease_job_id="bjob_1")
    with pytest.raises(ByocGovernanceError, match="immutable bundle-version"):
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
    settings = _settings()
    storage = InMemoryCustomerStorage(bundle_bytes=_BUNDLE_BYTES)

    # A runner that would "succeed" — the governance gate must refuse BEFORE it.
    called = {"ran": False}

    def runner(argv, run_dir: Path, _env) -> RunOutcome:
        called["ran"] = True
        return RunOutcome(returncode=0, report=SUCCESS_REPORT)

    result = execute_job(job, settings, storage, runner=runner)
    assert result.status == "failed"
    assert "api key env" in result.error
    assert called["ran"] is False  # fail closed: never ran without the rung


def test_execute_refuses_archive_digest_mismatch_before_runner():
    job = parse_job(_payload(bundle_sha256="0" * 64), lease_job_id="bjob_1")
    storage = InMemoryCustomerStorage(bundle_bytes=_BUNDLE_BYTES)
    called = {"ran": False}

    def runner(argv, run_dir: Path, _env) -> RunOutcome:
        called["ran"] = True
        return RunOutcome(returncode=0, report=SUCCESS_REPORT)

    result = execute_job(job, _settings(), storage, runner=runner)
    assert result.status == "failed"
    assert "storage bundle fetch failed" in (result.error or "")
    assert called["ran"] is False
    assert result.verified_bundle_sha256 is None
    assert "bundle_sha256" not in phi_free_callback_body(job, result)


@pytest.mark.parametrize("spawned", [False, True])
def test_child_exception_keeps_verified_binding_and_stage(tmp_path, spawned):
    from openadapt_flow.connector.executor import _subprocess_runner

    job = parse_job(_payload(), lease_job_id="bjob_1")
    storage = InMemoryCustomerStorage(bundle_bytes=_BUNDLE_BYTES)
    record = tmp_path / "synthetic-record"

    def runner(argv, run_dir: Path, env) -> RunOutcome:
        if not spawned:
            return _subprocess_runner([str(tmp_path / "missing-program")], run_dir, env)
        record.write_text("saved")
        raise RuntimeError("synthetic result unavailable")

    result = execute_job(job, _settings(), storage, runner=runner)
    assert result.status == ("halt" if spawned else "failed")
    assert result.error_code == (
        "child_outcome_unknown" if spawned else "child_invocation_failed"
    )
    assert record.exists() == spawned
    assert result.verified_bundle_sha256 == _BUNDLE_SHA256
    callback = phi_free_callback_body(job, result)
    assert callback["bundle_sha256"] == _BUNDLE_SHA256


def test_local_storage_refuses_unpacked_directory_bundle(tmp_path):
    unpacked = tmp_path / "bundles" / "wf"
    unpacked.mkdir(parents=True)
    storage = LocalCustomerStorage(str(tmp_path))
    with pytest.raises(RuntimeError, match="exact approved ZIP"):
        storage.fetch_bundle_archive("bundles/wf", tmp_path / "scratch.zip")


@pytest.mark.parametrize(
    "phase", ["before_child", "no_report", "missing_target", "wrong_target"]
)
def test_local_diagnostic_keeps_child_output_and_refusal_off_the_wire(
    tmp_path, monkeypatch, capsys, phase
):
    from openadapt_flow import at_rest, crypto
    from openadapt_flow.connector.diagnostics import MAX_OUTPUT, read_diagnostics
    from openadapt_flow.connector.executor import _subprocess_runner

    marker = "synthetic-private-diagnostic-value"
    monkeypatch.setenv(crypto.ENV_KEY, "synthetic-local-encryption")
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    payload = _payload()
    if phase == "before_child":
        payload["safety"] = {}
    cp = StubControlPlane()
    cp.enqueue("org_demo", payload)
    audit = tmp_path / "egress.jsonl"
    client = ConnectorClient(
        "https://app.test",
        transport=httpx.MockTransport(cp.handler),
        egress_audit_log=str(audit),
    )
    client.enroll(enrollment_secret="s", org_id="org_demo", name="n")
    root = tmp_path / "customer"
    bundle = root / payload["storage"]["bundle_ref"]
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(_BUNDLE_BYTES)
    report = root / payload["storage"]["report_ref"]
    fake_report = {
        "missing_target": {"success": True},
        "wrong_target": {"execution_target_kind": "citrix"},
    }.get(phase)

    def runner(argv, run_dir, env):
        assert phase != "before_child"
        program = (
            "import pathlib, sys\n"
            f"print('old-prefix' + 'x' * {MAX_OUTPUT + 5000}, flush=True)\n"
            f"print({payload['run_token']!r}, flush=True)\n"
            f"print('gate detail: {marker}', flush=True)\n"
            f"print('run REFUSED: {marker}', file=sys.stderr, flush=True)\n"
        )
        if fake_report is not None:
            program += (
                f"pathlib.Path(sys.argv[1]).write_text({json.dumps(fake_report)!r})\n"
            )
        return _subprocess_runner(
            [sys.executable, "-c", program, str(run_dir / "report.json")], run_dir, env
        )

    result = run_once(client, _settings(storage_root=str(root)), runner=runner)
    expected = {
        "before_child": "governance_refused",
        "no_report": "child_no_report",
        "missing_target": "child_report_missing_target",
        "wrong_target": "target_kind_mismatch",
    }
    assert result["error_code"] == expected[phase]
    assert result["callback_accepted"]
    assert result["status"] == (
        "halt" if phase in {"no_report", "missing_target"} else "failed"
    )
    sidecar = report.with_name(report.name + ".connector-diagnostics")
    callback = cp.callbacks[0]["body"]
    if phase in {"before_child", "no_report"}:
        assert not report.exists()
        assert callback["report_path"] is None
        assert "report_sha256" not in callback["metrics"]
    else:
        assert callback["report_path"] == payload["storage"]["report_ref"]
        stored_digest = hashlib.sha256(report.read_bytes()).hexdigest()
        assert callback["metrics"]["report_sha256"] == stored_digest
    assert marker.encode() not in sidecar.read_bytes()
    assert crypto.is_encrypted(sidecar.read_bytes())
    if os.name == "posix":
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    diagnostic = read_diagnostics(sidecar)
    assert diagnostic["run_id"] == payload["run_id"]
    assert diagnostic["error_code"] == expected[phase]
    assert diagnostic["detail"]
    if phase == "before_child":
        assert "policy" in diagnostic["detail"]
        assert diagnostic["child_output"] == ""
    else:
        assert diagnostic["child_output"].splitlines()[-2:] == [
            f"gate detail: {marker}",
            f"run REFUSED: {marker}",
        ]
        assert "old-prefix" not in diagnostic["child_output"]
        assert payload["run_token"] not in diagnostic["child_output"]
        assert len(diagnostic["child_output"]) <= MAX_OUTPUT
    captured = capsys.readouterr()
    shared = (
        captured.out
        + captured.err
        + str(result)
        + str(cp.callbacks)
        + str(cp.acks)
        + audit.read_text()
    )
    assert marker not in shared and diagnostic["detail"] not in shared


def test_private_diagnostic_plaintext_requires_recorded_optout(tmp_path, monkeypatch):
    from openadapt_flow import at_rest, crypto
    from openadapt_flow.connector.diagnostics import read_diagnostics, write_diagnostics

    monkeypatch.delenv(crypto.ENV_KEY, raising=False)
    monkeypatch.delenv(at_rest.MODE_ENV, raising=False)
    monkeypatch.setenv(at_rest.HOME_ENV, str(tmp_path / "state"))
    monkeypatch.setenv(at_rest.LEDGER_DIR_ENV, str(tmp_path / "ledger"))
    monkeypatch.setenv(at_rest.OPT_OUT_ENV, "synthetic diagnostics fixture")
    at_rest._reset_for_tests()
    sidecar = tmp_path / "report.json.connector-diagnostics"
    content = {"detail": "synthetic refusal detail"}
    write_diagnostics(sidecar, content)
    assert not crypto.is_encrypted(sidecar.read_bytes())
    assert read_diagnostics(sidecar) == content
    assert "connector diagnostics" in at_rest.ledger_path().read_text()
    if os.name == "posix":
        sidecar.chmod(0o644)
        with pytest.raises(ValueError, match="owner-only"):
            read_diagnostics(sidecar)


def test_unavailable_private_diagnostic_preserves_authoritative_result(
    tmp_path, monkeypatch
):
    from openadapt_flow.connector.diagnostics import write_diagnostics

    path = tmp_path / "report.json.connector-diagnostics"
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", "synthetic-key")

    def refuse_descriptor(fd):
        raise PermissionError("synthetic private path detail")

    monkeypatch.setattr(
        "openadapt_flow.connector.diagnostics._check_private", refuse_descriptor
    )
    with pytest.raises(PermissionError):
        write_diagnostics(path, {"detail": "private"})
    assert not list(tmp_path.iterdir())

    cp = StubControlPlane()
    cp.enqueue("org_demo", _payload(safety={}))
    client = ConnectorClient(
        "https://app.test", transport=httpx.MockTransport(cp.handler)
    )
    client.enroll(enrollment_secret="s", org_id="org_demo", name="n")
    result = run_once(client, _settings(storage_root=str(tmp_path)), runner=pytest.fail)
    assert result["callback_accepted"] and result["status"] == "failed"
    assert result["error_code"] == "governance_refused"
    assert result["local_diagnostics_unavailable"] is True
    assert "local_diagnostics_unavailable" not in str(cp.callbacks)


def test_local_storage_copies_exact_archive_bytes(tmp_path):
    archive = tmp_path / "bundles" / "wf.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(_BUNDLE_BYTES)
    storage = LocalCustomerStorage(str(tmp_path))
    copied = storage.fetch_bundle_archive(
        "bundles/wf.zip", tmp_path / "scratch" / "approved.zip"
    )
    assert copied.read_bytes() == _BUNDLE_BYTES


def test_local_storage_refuses_report_ref_outside_root(tmp_path):
    storage = LocalCustomerStorage(str(tmp_path / "customer"))
    with pytest.raises(RuntimeError, match="report ref escapes"):
        storage.write_report("../escaped.json", {"success": True})
    assert not (tmp_path / "escaped.json").exists()


def test_governed_run_argv_uses_the_fail_closed_run_verb():
    job = parse_job(_payload(), lease_job_id="bjob_1")
    settings = _settings(
        profile="/opt/openadapt/deployment.yaml", policy="clinical-write"
    )
    argv = build_run_argv(job, settings, Path("/b"), Path("/r"), Path("/r/params.json"))
    # It shells the GOVERNED `run` (fail-closed admission), never permissive replay.
    assert argv[1:4] == ["-m", "openadapt_flow", "run"]
    assert "--config" in argv and "/opt/openadapt/deployment.yaml" in argv
    assert "--policy" in argv and "clinical-write" in argv
    assert "--params-file" in argv
    assert argv[argv.index("--backend") + 1] == "web"
    assert "replay" not in argv
    # A dispatch transport cannot turn off the governed run's approval gate.
    assert "--approval-mode" not in argv
    assert "not_gated" not in argv


def test_native_dispatch_routes_exact_first_class_target_kind():
    job = parse_job(
        _payload(
            target_kind="citrix",
            target_url=None,
            allowed_hosts=[],
            params={"vendor": "ACME", "target_kind": "citrix"},
        ),
        lease_job_id="bjob_1",
    )
    argv = build_run_argv(job, _settings(), Path("/b"), Path("/r"), None)
    assert argv[argv.index("--backend") + 1] == "citrix"
    assert "--url" not in argv


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
        self.poll_bodies = []
        self.callback_attempts = 0
        self.callback_status = 200
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
            self.poll_bodies.append(body)
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
            self.callback_attempts += 1
            if self.callback_status != 200:
                return httpx.Response(self.callback_status, json={"ok": False})
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

    monkeypatch.delenv("BYOC_QUALIFICATION_AUTHORITY_FILE", raising=False)
    authority_file = str(tmp_path / "operator-authority.json")
    settings = _settings(
        control_plane_url="https://app.test",
        token=data["token"],
        org_id="org_demo",
        qualification_authority_file=authority_file,
    )
    path = save_enrollment(settings)
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    # A bare `run` reads it back.
    reloaded = load_settings()
    assert reloaded.token == data["token"]
    assert reloaded.org_id == "org_demo"
    assert reloaded.qualification_authority_file == authority_file
    monkeypatch.setenv("BYOC_QUALIFICATION_AUTHORITY_FILE", "operator-env.json")
    assert load_settings().qualification_authority_file == "operator-env.json"
    assert (
        load_settings(
            {"qualification_authority_file": "operator-flag.json"}
        ).qualification_authority_file
        == "operator-flag.json"
    )


@pytest.mark.parametrize("verb", ["enroll", "run", "rotate-token"])
def test_connector_cli_retains_callbacks_and_refuses_unenrolled_run(
    verb, tmp_path, monkeypatch
):
    from openadapt_flow.__main__ import _cmd_connector, _connector_flags, build_parser

    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path))
    monkeypatch.delenv("BYOC_CONNECTOR_TOKEN", raising=False)
    options = ["connector", verb, "--control-plane-url", "https://app.test"]
    if verb == "run":
        options += ["--secrets-dir", str(tmp_path / "secrets"), "--max-workers", "2"]
    args = build_parser().parse_args(options)
    assert args.func is _cmd_connector
    flags = _connector_flags(args)
    assert flags["control_plane_url"] == "https://app.test"
    if verb == "run":
        assert flags["max_workers"] == 2
        assert flags["secrets_dir"] == str(tmp_path / "secrets")
    if verb != "enroll":
        # The actual moved handler must refuse before opening a connection.
        assert args.func(args) == 2


def test_full_loop_dispatch_execute_callback_ack():
    cp = StubControlPlane()
    cp.enqueue("org_demo", _payload())
    transport = httpx.MockTransport(cp.handler)
    client = ConnectorClient("https://app.test", token=None, transport=transport)
    client.enroll(enrollment_secret="s", org_id="org_demo", name="n")

    settings = _settings(
        control_plane_url="https://app.test",
        org_id="org_demo",
        token=client.token,
        poll_wait_s=0,
    )
    storage = InMemoryCustomerStorage(bundle_bytes=_BUNDLE_BYTES)
    result = run_once(
        client,
        settings,
        runner=_fake_success_runner(SUCCESS_REPORT),
        storage_factory=lambda job: storage,
    )
    assert result["status"] == "success"
    assert cp.poll_bodies == [
        {
            "wait": 0,
            "capabilities": [MANAGED_QUALIFICATION_AUTHORITY_CAPABILITY],
            "active_job_ids": [],
        }
    ]

    # A PHI-free callback carrying the run-scoped token was posted.
    assert len(cp.callbacks) == 1
    cb = cp.callbacks[0]
    assert cb["headers"].get("x-run-token") == "a" * 64
    assert cb["body"]["status"] == "success"
    assert "12345" not in json.dumps(cb["body"])  # PHI never crossed

    # The lease was released done.
    assert len(cp.acks) == 1
    assert cp.acks[0]["status"] == "done"


@pytest.mark.parametrize("callback_status", [400, 503])
def test_callback_rejection_is_retried_but_never_acked(callback_status, monkeypatch):
    monkeypatch.setattr("openadapt_flow.connector.daemon.time.sleep", lambda _: None)
    cp = StubControlPlane()
    cp.callback_status = callback_status
    cp.enqueue("org_demo", _payload())
    transport = httpx.MockTransport(cp.handler)
    client = ConnectorClient("https://app.test", transport=transport)
    client.enroll(enrollment_secret="s", org_id="org_demo", name="n")
    settings = _settings(
        control_plane_url="https://app.test",
        org_id="org_demo",
        token=client.token,
        poll_wait_s=0,
    )

    result = run_once(
        client,
        settings,
        runner=_fake_success_runner(SUCCESS_REPORT),
        storage_factory=lambda job: InMemoryCustomerStorage(bundle_bytes=_BUNDLE_BYTES),
    )

    assert result == {
        "job_id": "bjob_1",
        "run_id": _payload()["run_id"],
        "status": "success",
        "error_code": None,
        "callback_accepted": False,
    }
    assert cp.callback_attempts == 3
    assert cp.callbacks == []
    assert cp.acks == []
    assert cp.jobs[0]["status"] == "leased"


def test_cross_org_isolation_connector_never_sees_another_orgs_job():
    cp = StubControlPlane()
    cp.enqueue("org_B", _payload(org_id="org_B"))  # a DIFFERENT org's job
    transport = httpx.MockTransport(cp.handler)

    # A connector enrolled for org_A.
    client = ConnectorClient("https://app.test", transport=transport)
    client.enroll(enrollment_secret="s", org_id="org_A", name="a")
    settings = _settings(
        control_plane_url="https://app.test",
        org_id="org_A",
        token=client.token,
        poll_wait_s=0,
    )

    result = run_once(
        client,
        settings,
        runner=_fake_success_runner(SUCCESS_REPORT),
        storage_factory=lambda job: InMemoryCustomerStorage(bundle_bytes=_BUNDLE_BYTES),
    )
    assert result is None  # org_A connector sees no work; org_B's job stays queued
    assert cp.jobs[0]["status"] == "queued"
    assert cp.callbacks == []


@pytest.mark.parametrize(
    "target", ["web", "windows", "macos", "linux", "rdp", "citrix"]
)
def test_every_connector_substrate_preserves_the_governed_approval_default(target):
    from openadapt_flow.approval_mode.model import ApprovalMode, resolve_approval_mode

    job = parse_job(
        _payload(target_kind=target, target_url=None, allowed_hosts=[], params={})
    )
    argv = build_run_argv(job, _settings(), Path("/bundle"), Path("/run"), None)
    assert "--approval-mode" not in argv
    assert resolve_approval_mode(None) == ApprovalMode.EACH_ITEM
