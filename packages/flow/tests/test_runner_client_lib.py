"""Unit tests for the Experimental runner-client LIBRARY
(``openadapt_flow.runner``): strict contract parsing, the local verification
refusal matrix, the pure lease state machine (expiry / sleep / honest late
completion), the PHI-free evidence builders, the durable outbox, and the
governed command mapping.

No network anywhere: everything runs against tmp fixtures and a wire-safety
mirror of the cloud's fail-closed ``runEvidence.ts`` validator rules (the
forbidden-key / PHI-shaped-text / whitelist discipline), so evidence the
library emits is provably acceptable to the mock-contract server without one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from openadapt_flow.ir import (
    HaltObservation,
    IdentityCheck,
    Resolution,
    RunReport,
    StepResult,
    Workflow,
)
from openadapt_flow.runner import commands, evidence, lease
from openadapt_flow.runner.config import (
    RunnerConfigError,
    load_runner_config,
)
from openadapt_flow.runner.outbox import EvidenceOutbox
from openadapt_flow.runner.protocol import (
    DispatchParseError,
    parse_dispatch,
)
from openadapt_flow.runner.verify import Refusal, RefusalCode, verify_dispatch
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    runtime_inputs_digest,
)
from tests.test_replayer import click_step, make_png

# ---------------------------------------------------------------------------
# Fixtures: a sealed bundle, a deployment profile, and a trust manifest
# ---------------------------------------------------------------------------

PARAMS = {"visit_date": "2026-07-01"}


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))


@pytest.fixture()
def sealed(tmp_path) -> tuple[Workflow, Path]:
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    workflow = Workflow(name="claims-entry", steps=[click_step()])
    workflow.save(bundle)
    return Workflow.load(bundle), bundle


@pytest.fixture()
def profile(tmp_path) -> Path:
    path = tmp_path / "deployment.yaml"
    path.write_text("runtime:\n  durable: false\n", encoding="utf-8")
    return path


def write_manifest(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "runner.toml"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture()
def config(tmp_path, sealed, profile):
    workflow, bundle = sealed
    assert workflow.manifest is not None
    manifest = write_manifest(
        tmp_path,
        f"""
[runner]
name = "front-desk-1"

[profiles]
default = "{profile}"

[[bundles]]
content_digest = "{workflow.manifest.content_digest}"
path = "{bundle}"
""",
    )
    return load_runner_config(manifest)


def mint_authorization(
    workflow: Workflow, params: dict[str, str] | None = None
) -> GovernedRunAuthorization:
    assert workflow.manifest is not None
    return GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(
            workflow, params if params is not None else PARAMS, None
        ),
        admitted_policy_name="clinical-write",
        approval_source="hosted:app.openadapt.ai:apr_test:user_test",
    )


def dispatch_payload(workflow: Workflow, **overrides) -> dict:
    assert workflow.manifest is not None
    authorization = overrides.pop("authorization", None) or mint_authorization(workflow)
    payload = {
        "job_kind": "governed_run",
        "run_id": "run_1",
        "workflow_id": "wf_1",
        "bundle": {
            "version_id": None,
            "content_digest": workflow.manifest.content_digest,
            "url": "mock://bundles/never-fetched",
        },
        "deployment_profile_id": "default",
        "authorization": authorization.model_dump(mode="json"),
        "params": {"values": dict(PARAMS)},
        "expires_at": "2099-01-01T00:00:00Z",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Wire-safety mirror of the cloud validator (runEvidence.ts) — the discipline
# every emitted event must satisfy
# ---------------------------------------------------------------------------

FORBIDDEN_KEYS = {
    "field_values",
    "fields",
    "matched_records",
    "records",
    "values",
    "value",
    "observed",
    "expected",
    "dom",
    "html",
    "screenshot",
    "screenshots",
    "frame",
    "frames",
    "image",
    "images",
    "pixels",
    "report",
    "report_body",
    "body",
    "text",
    "ocr_text",
    "selector_value",
    "params",
    "param_values",
    "worklist",
    "worklists",
    "patient",
    "patient_id",
    "mrn",
    "dob",
    "name",
    "address",
    "phone",
    "email",
    "ssn",
    "free_text",
    "note",
    "notes",
}
SSN_SHAPED = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
EMAIL_SHAPED = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def assert_wire_safe(value, path="$"):
    if isinstance(value, str):
        assert len(value) <= 500, f"{path}: string too long"
        assert not SSN_SHAPED.search(value), f"{path}: SSN-shaped text"
        assert not EMAIL_SHAPED.search(value), f"{path}: email-shaped text"
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            assert_wire_safe(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            assert key.lower() not in FORBIDDEN_KEYS, f"{path}.{key}: forbidden key"
            assert_wire_safe(child, f"{path}.{key}")


def assert_valid_batch(events, run_id):
    seqs = []
    for event in events:
        assert event["schema"] == evidence.SCHEMA
        assert event["run_id"] == run_id
        assert event["authorization_id"]
        assert event["kind"] in {"state", "step", "run_summary", "halt"}
        # exactly one body, matching kind
        bodies = {k for k in ("state", "step", "run_summary", "halt") if k in event}
        assert bodies == {event["kind"]}
        seqs.append(event["seq"])
        assert_wire_safe(event)
    assert seqs == sorted(seqs)


# ---------------------------------------------------------------------------
# protocol: strict contract parsing
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_parses_a_contract_exact_payload(self, sealed):
        workflow, _ = sealed
        parsed = parse_dispatch(dispatch_payload(workflow))
        assert parsed.job_kind == "governed_run"
        assert parsed.bundle.url == "mock://bundles/never-fetched"
        assert parsed.authorization.admitted_policy_name == "clinical-write"

    def test_unknown_field_is_contract_drift(self, sealed):
        workflow, _ = sealed
        with pytest.raises(DispatchParseError):
            parse_dispatch(dispatch_payload(workflow, surprise="field"))

    def test_malformed_digest_refuses(self, sealed):
        workflow, _ = sealed
        payload = dispatch_payload(workflow)
        payload["bundle"]["content_digest"] = "not-a-digest"
        with pytest.raises(DispatchParseError):
            parse_dispatch(payload)

    def test_params_ref_lane_parses(self, sealed):
        workflow, _ = sealed
        payload = dispatch_payload(
            workflow, params={"ref": "worklist-7", "expected_digest": "a" * 64}
        )
        parsed = parse_dispatch(payload)
        assert not hasattr(parsed.params, "values")


# ---------------------------------------------------------------------------
# config: the operator trust manifest
# ---------------------------------------------------------------------------


class TestTrustManifest:
    def test_loads_bundles_profiles_and_pins(self, tmp_path, sealed, profile):
        workflow, bundle = sealed
        digest = workflow.manifest.content_digest
        manifest = write_manifest(
            tmp_path,
            f"""
[runner]
name = "front-desk-1"
backends = ["web", "windows"]

[profiles]
default = "{profile}"

[[bundles]]
content_digest = "{digest}"
path = "{bundle}"
policy = "clinical-write"
params_ref_required = true
allow_unverified_writes = true
[bundles.param_patterns]
visit_date = '^\\d{{4}}-\\d{{2}}-\\d{{2}}$'
""",
        )
        cfg = load_runner_config(manifest)
        assert cfg.name == "front-desk-1"
        assert cfg.backends == ("web", "windows")
        trusted = cfg.bundles[digest]
        assert trusted.policy == "clinical-write"
        assert trusted.params_ref_required is True
        assert trusted.param_patterns == {"visit_date": r"^\d{4}-\d{2}-\d{2}$"}

    def test_missing_manifest_fails_loudly(self, tmp_path):
        with pytest.raises(RunnerConfigError, match="No runner trust manifest"):
            load_runner_config(tmp_path / "missing.toml")

    def test_bad_digest_refused(self, tmp_path, sealed, profile):
        _, bundle = sealed
        manifest = write_manifest(
            tmp_path,
            f"""
[runner]
name = "n"
[profiles]
default = "{profile}"
[[bundles]]
content_digest = "abc"
path = "{bundle}"
""",
        )
        with pytest.raises(RunnerConfigError, match="64 lowercase hex"):
            load_runner_config(manifest)

    def test_invalid_param_regex_refused(self, tmp_path, sealed, profile):
        workflow, bundle = sealed
        manifest = write_manifest(
            tmp_path,
            f"""
[runner]
name = "n"
[profiles]
default = "{profile}"
[[bundles]]
content_digest = "{workflow.manifest.content_digest}"
path = "{bundle}"
[bundles.param_patterns]
visit_date = "["
""",
        )
        with pytest.raises(RunnerConfigError, match="not a valid regex"):
            load_runner_config(manifest)

    def test_missing_profile_file_refused(self, tmp_path):
        manifest = write_manifest(
            tmp_path,
            f"""
[runner]
name = "n"
[profiles]
default = "{tmp_path / "nope.yaml"}"
""",
        )
        with pytest.raises(RunnerConfigError, match="missing deployment config"):
            load_runner_config(manifest)


# ---------------------------------------------------------------------------
# verify: the refusal matrix + the admit path
# ---------------------------------------------------------------------------


def verified_or_refusal(workflow, config, **overrides):
    payload = parse_dispatch(dispatch_payload(workflow, **overrides))
    return verify_dispatch(payload, config)


class TestVerifyAdmits:
    def test_full_admit_returns_execution_snapshot(self, sealed, config):
        workflow, bundle = sealed
        verdict = verified_or_refusal(workflow, config)
        assert not isinstance(verdict, Refusal)
        assert verdict.bundle.path == bundle
        assert verdict.params == PARAMS
        assert verdict.profile_path.name == "deployment.yaml"
        # Whole-workflow coverage counts come from the sealed bundle itself.
        assert verdict.consequential_steps == 1
        assert verdict.effect_covered_consequential_steps == 0
        assert verdict.workflow.manifest is not None


class TestVerifyRefusals:
    def test_unknown_job_kind(self, sealed, config):
        workflow, _ = sealed
        verdict = verified_or_refusal(workflow, config, job_kind="pause")
        assert isinstance(verdict, Refusal)
        assert verdict.code is RefusalCode.UNSUPPORTED_JOB_KIND

    def test_concurrent_run_for_same_workflow(self, sealed, config):
        workflow, _ = sealed
        payload = parse_dispatch(dispatch_payload(workflow))
        verdict = verify_dispatch(payload, config, active_workflow_ids={"wf_1"})
        assert isinstance(verdict, Refusal)
        assert verdict.code is RefusalCode.CONCURRENT_RUN

    def test_expired_dispatch_never_starts(self, sealed, config):
        workflow, _ = sealed
        verdict = verified_or_refusal(
            workflow, config, expires_at="2020-01-01T00:00:00Z"
        )
        assert isinstance(verdict, Refusal)
        assert verdict.code is RefusalCode.DISPATCH_EXPIRED

    def test_unparseable_expiry_is_malformed(self, sealed, config):
        workflow, _ = sealed
        verdict = verified_or_refusal(workflow, config, expires_at="soon")
        assert isinstance(verdict, Refusal)
        assert verdict.code is RefusalCode.MALFORMED_DISPATCH

    def test_digest_disagreement_inside_payload(self, sealed, config):
        workflow, _ = sealed
        payload = dispatch_payload(workflow)
        payload["bundle"]["content_digest"] = "a" * 64
        verdict = verify_dispatch(parse_dispatch(payload), config)
        assert isinstance(verdict, Refusal)
        assert verdict.code is RefusalCode.DIGEST_MISMATCH

    def test_unknown_bundle_is_never_fetched(self, sealed, config):
        workflow, _ = sealed
        fake = "b" * 64
        authorization = mint_authorization(workflow).model_copy(
            update={"bundle_content_digest": fake}
        )
        payload = dispatch_payload(workflow, authorization=authorization)
        payload["bundle"]["content_digest"] = fake
        verdict = verify_dispatch(parse_dispatch(payload), config)
        assert isinstance(verdict, Refusal)
        assert verdict.code is RefusalCode.BUNDLE_NOT_HELD
        assert "never downloads" in verdict.detail

    def test_params_ref_lane_refused_without_resolver(self, sealed, config):
        workflow, _ = sealed
        verdict = verified_or_refusal(
            workflow,
            config,
            params={"ref": "worklist-7", "expected_digest": "c" * 64},
        )
        assert isinstance(verdict, Refusal)
        assert verdict.code is RefusalCode.PARAMS_REF_UNSUPPORTED

    def test_regulated_bundle_refuses_inline_values(self, tmp_path, sealed, profile):
        workflow, bundle = sealed
        manifest = write_manifest(
            tmp_path,
            f"""
[runner]
name = "n"
[profiles]
default = "{profile}"
[[bundles]]
content_digest = "{workflow.manifest.content_digest}"
path = "{bundle}"
params_ref_required = true
""",
        )
        cfg = load_runner_config(manifest)
        verdict = verified_or_refusal(workflow, cfg)
        assert isinstance(verdict, Refusal)
        assert verdict.code is RefusalCode.PARAMS_VALUES_REFUSED

    @pytest.mark.parametrize(
        "params, why",
        [
            ({"visit_date": "07/01/2026"}, "does not match"),
            ({"visit_date": "2026-07-01", "extra": "x"}, "no operator-pinned"),
        ],
    )
    def test_param_domain_pinning(self, tmp_path, sealed, profile, params, why):
        workflow, bundle = sealed
        manifest = write_manifest(
            tmp_path,
            f"""
[runner]
name = "n"
[profiles]
default = "{profile}"
[[bundles]]
content_digest = "{workflow.manifest.content_digest}"
path = "{bundle}"
[bundles.param_patterns]
visit_date = '^\\d{{4}}-\\d{{2}}-\\d{{2}}$'
""",
        )
        cfg = load_runner_config(manifest)
        authorization = mint_authorization(workflow, params)
        verdict = verified_or_refusal(
            workflow,
            cfg,
            params={"values": params},
            authorization=authorization,
        )
        assert isinstance(verdict, Refusal)
        assert verdict.code is RefusalCode.PARAM_DOMAIN_REFUSED
        assert why in verdict.detail

    def test_unknown_profile(self, sealed, config):
        workflow, _ = sealed
        verdict = verified_or_refusal(workflow, config, deployment_profile_id="other")
        assert isinstance(verdict, Refusal)
        assert verdict.code is RefusalCode.UNKNOWN_PROFILE

    def test_egress_profile_refused(self, tmp_path, sealed):
        workflow, bundle = sealed
        egress_profile = tmp_path / "egress.yaml"
        egress_profile.write_text(
            "runtime:\n  allow_model_grounding: true\n", encoding="utf-8"
        )
        manifest = write_manifest(
            tmp_path,
            f"""
[runner]
name = "n"
[profiles]
default = "{egress_profile}"
[[bundles]]
content_digest = "{workflow.manifest.content_digest}"
path = "{bundle}"
""",
        )
        cfg = load_runner_config(manifest)
        verdict = verified_or_refusal(workflow, cfg)
        assert isinstance(verdict, Refusal)
        assert verdict.code is RefusalCode.EGRESS_PROFILE_REFUSED

    def test_bundle_load_failure_refuses(self, tmp_path, sealed, profile):
        workflow, _ = sealed
        empty = tmp_path / "empty-bundle"
        empty.mkdir()
        manifest = write_manifest(
            tmp_path,
            f"""
[runner]
name = "n"
[profiles]
default = "{profile}"
[[bundles]]
content_digest = "{workflow.manifest.content_digest}"
path = "{empty}"
""",
        )
        cfg = load_runner_config(manifest)
        verdict = verified_or_refusal(workflow, cfg)
        assert isinstance(verdict, Refusal)
        assert verdict.code is RefusalCode.BUNDLE_LOAD_FAILED

    def test_authorization_bound_to_other_bundle_refuses(
        self, tmp_path, sealed, profile
    ):
        # The manifest trusts the real bundle under a DIFFERENT digest key,
        # and the authorization is minted for that other digest: the loaded
        # bundle can never satisfy it.
        workflow, bundle = sealed
        other = "d" * 64
        manifest = write_manifest(
            tmp_path,
            f"""
[runner]
name = "n"
[profiles]
default = "{profile}"
[[bundles]]
content_digest = "{other}"
path = "{bundle}"
""",
        )
        cfg = load_runner_config(manifest)
        authorization = mint_authorization(workflow).model_copy(
            update={"bundle_content_digest": other}
        )
        payload = dispatch_payload(workflow, authorization=authorization)
        payload["bundle"]["content_digest"] = other
        verdict = verify_dispatch(parse_dispatch(payload), cfg)
        assert isinstance(verdict, Refusal)
        assert verdict.code is RefusalCode.AUTHORIZATION_MISMATCH

    def test_runtime_inputs_drift_fails_closed(self, sealed, config):
        workflow, _ = sealed
        verdict = verified_or_refusal(
            workflow, config, params={"values": {"visit_date": "2026-07-02"}}
        )
        assert isinstance(verdict, Refusal)
        assert verdict.code is RefusalCode.RUNTIME_INPUTS_MISMATCH

    def test_operator_policy_pin_is_final(self, tmp_path, sealed, profile):
        workflow, bundle = sealed
        manifest = write_manifest(
            tmp_path,
            f"""
[runner]
name = "n"
[profiles]
default = "{profile}"
[[bundles]]
content_digest = "{workflow.manifest.content_digest}"
path = "{bundle}"
policy = "stricter-policy"
""",
        )
        cfg = load_runner_config(manifest)
        verdict = verified_or_refusal(workflow, cfg)
        assert isinstance(verdict, Refusal)
        assert verdict.code is RefusalCode.POLICY_MISMATCH

    def test_every_refusal_reason_is_wire_safe(self, sealed, config):
        workflow, _ = sealed
        verdict = verified_or_refusal(workflow, config, job_kind="pause")
        assert isinstance(verdict, Refusal)
        events = evidence.refusal_events(
            verdict,
            run_id="run_1",
            workflow_id="wf_1",
            bundle_digest="e" * 64,
            authorization_id="auth_1",
        )
        assert_valid_batch(events, "run_1")


# ---------------------------------------------------------------------------
# lease: the pure state machine
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def iso_at(clock: FakeClock, offset: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(clock.now + offset, tz=timezone.utc).isoformat()


class TestLeaseTracker:
    def test_lifecycle_ack_path(self):
        clock = FakeClock()
        tracker = lease.LeaseTracker(clock)
        tracker.acquire("disp_1", "run_1", iso_at(clock, 900))
        assert tracker.phase is lease.LeasePhase.LEASED
        tracker.mark_started()
        clock.advance(60)
        assert (
            tracker.completion_disposition() is lease.CompletionDisposition.ACKS_LEASE
        )
        tracker.release()
        assert not tracker.held

    def test_single_flight_per_machine(self):
        clock = FakeClock()
        tracker = lease.LeaseTracker(clock)
        tracker.acquire("disp_1", "run_1", iso_at(clock, 900))
        with pytest.raises(lease.LeaseError, match="single-flight"):
            tracker.acquire("disp_2", "run_2", iso_at(clock, 900))

    def test_expired_before_start_refuses_and_resets(self):
        clock = FakeClock()
        tracker = lease.LeaseTracker(clock)
        tracker.acquire("disp_1", "run_1", iso_at(clock, 900))
        clock.advance(901)
        with pytest.raises(lease.StartRefused, match="re-offered"):
            tracker.mark_started()
        assert not tracker.held  # server will re-offer; we hold nothing

    def test_sleep_mid_run_is_detected_and_reported_late(self):
        clock = FakeClock()
        tracker = lease.LeaseTracker(clock)
        tracker.acquire("disp_1", "run_1", iso_at(clock, 900))
        tracker.mark_started()
        tracker.tick()
        clock.advance(3600)  # laptop lid closed for an hour
        gap = tracker.tick()
        assert gap is not None and gap.lease_lost
        assert gap.gap_seconds == pytest.approx(3600)
        assert (
            tracker.completion_disposition()
            is lease.CompletionDisposition.LATE_AFTER_LEASE_LOSS
        )

    def test_renewal_extends_and_expired_renewal_refused(self):
        clock = FakeClock()
        tracker = lease.LeaseTracker(clock)
        tracker.acquire("disp_1", "run_1", iso_at(clock, 900))
        tracker.mark_started()
        clock.advance(800)
        tracker.renew(iso_at(clock, 900))  # heartbeat-extends-lease (modeled)
        clock.advance(800)
        assert (
            tracker.completion_disposition() is lease.CompletionDisposition.ACKS_LEASE
        )
        clock.advance(200)
        with pytest.raises(lease.LeaseError, match="report late"):
            tracker.renew(iso_at(clock, 900))

    def test_server_reclaim_rule_mirror(self):
        assert (
            lease.server_reclaim_outcome(run_started=False, lease_expired=False)
            == "keep"
        )
        assert (
            lease.server_reclaim_outcome(run_started=False, lease_expired=True)
            == "reoffer"
        )
        assert (
            lease.server_reclaim_outcome(run_started=True, lease_expired=True)
            == "uncertain"
        )

    def test_workflow_serialization_registry(self):
        serial = lease.WorkflowSerialization()
        assert serial.begin("wf_1")
        assert not serial.begin("wf_1")
        assert serial.begin("wf_2")
        serial.end("wf_1")
        assert serial.begin("wf_1")
        assert serial.active == {"wf_1", "wf_2"}


# ---------------------------------------------------------------------------
# evidence: PHI-free builders
# ---------------------------------------------------------------------------


def make_report(*, success: bool, halted: bool = False) -> RunReport:
    results = [
        StepResult(
            step_id="s1",
            intent="click Save for Jane Doe",  # local-only free text
            ok=True,
            resolution=Resolution(
                rung="structural", point=(1, 1), confidence=0.99, elapsed_ms=12.0
            ),
            identity=IdentityCheck(status="verified", mode="structured"),
            effect_verified=True,
            effect_contract_hashes=[f"sha256:{'a' * 64}"],
            elapsed_ms=120.0,
        ),
        StepResult(
            step_id="s2",
            intent="submit claim for MRN 12345",
            ok=not halted,
            resolution=Resolution(
                rung="ocr", point=(2, 2), confidence=0.7, elapsed_ms=30.0
            ),
            elapsed_ms=80.0,
        ),
    ]
    halt = None
    if halted:
        halt = HaltObservation(
            state_id="s2",
            intent="submit claim for MRN 12345",
            reason="unexpected dialog blocking patient Jane Doe 123-45-6789",
            observed_texts=["Jane Doe", "jane@example.com"],
            completed_intents=["click Save for Jane Doe"],
        )
    return RunReport(
        workflow_name="claims-entry",
        started_at="2026-07-19T00:00:00Z",
        results=results,
        success=success,
        halt=halt,
        total_ms=4321.5,
        required_identity_step_ids=["s1"],
    )


class TestEvidenceBuilders:
    def test_success_stream_is_wire_safe_and_terminal(self):
        events = evidence.report_events(
            make_report(success=True),
            run_id="run_1",
            workflow_id="wf_1",
            bundle_digest="f" * 64,
            authorization_id="auth_1",
            consequential_steps=1,
            effect_covered_consequential_steps=1,
        )
        assert_valid_batch(events, "run_1")
        summary = events[-1]["run_summary"]
        assert summary["status"] == "confirmed"
        assert summary["steps_total"] == 2
        assert summary["effects_confirmed"] == 1
        assert summary["identity_steps_required"] == 1
        assert summary["identity_steps_verified"] == 1
        assert summary["screenshots_may_leave_box"] is False
        step_bodies = [e["step"] for e in events if e["kind"] == "step"]
        assert [b["rung"] for b in step_bodies] == ["structural", "ocr"]
        assert step_bodies[0]["effect_verified"] is True

    def test_halt_stream_never_forwards_free_text(self):
        events = evidence.report_events(
            make_report(success=False, halted=True),
            run_id="run_1",
            workflow_id="wf_1",
            bundle_digest="f" * 64,
            authorization_id="auth_1",
            consequential_steps=0,
            effect_covered_consequential_steps=0,
        )
        assert_valid_batch(events, "run_1")  # catches the SSN/email shapes too
        halt = next(e for e in events if e["kind"] == "halt")["halt"]
        assert halt["kind"] == "resolver_halt"
        assert halt["step_id"] == "s2"
        assert halt["rung"] == "ocr"
        assert "Jane" not in json.dumps(events)
        assert halt["evidence_digest"] == {
            "observed_texts_count": 2,
            "completed_steps_count": 1,
        }
        assert events[-1]["run_summary"]["status"] == "halted-needs-attention"

    def test_identity_mismatch_classifies_identity_halt(self):
        report = make_report(success=False, halted=True)
        report.results[1].identity = IdentityCheck(status="mismatch", mode="structured")
        events = evidence.report_events(
            report,
            run_id="run_1",
            workflow_id="wf_1",
            bundle_digest="f" * 64,
            authorization_id="auth_1",
            consequential_steps=0,
            effect_covered_consequential_steps=0,
        )
        halt = next(e for e in events if e["kind"] == "halt")["halt"]
        assert halt["kind"] == "identity_halt"

    def test_egress_wired_run_refuses_to_attest(self):
        report = make_report(success=True)
        report.screenshots_may_leave_box = True
        with pytest.raises(ValueError, match="screenshots_may_leave_box"):
            evidence.report_events(
                report,
                run_id="run_1",
                workflow_id="wf_1",
                bundle_digest="f" * 64,
                authorization_id="auth_1",
                consequential_steps=0,
                effect_covered_consequential_steps=0,
            )

    def test_refusal_and_failure_events(self):
        refusal = Refusal(RefusalCode.BUNDLE_NOT_HELD, "bundle abcd not held")
        events = evidence.refusal_events(
            refusal,
            run_id="run_1",
            workflow_id="wf_1",
            bundle_digest="e" * 64,
            authorization_id="auth_1",
        )
        assert_valid_batch(events, "run_1")
        assert events[0]["halt"]["kind"] == "authorization_refused"
        assert events[0]["halt"]["reason"].startswith("bundle_not_held:")
        assert events[-1]["run_summary"]["status"] == "failed"

        failure = evidence.failure_events(
            run_id="run_1",
            bundle_digest="e" * 64,
            authorization_id="auth_1",
            duration_ms=500,
        )
        assert_valid_batch(failure, "run_1")
        assert failure[0]["run_summary"]["status"] == "failed"

    def test_state_event_validates_state(self):
        event = evidence.state_event("run_1", "auth_1", 0, "started")
        assert event["state"] == {"state": "started"}
        with pytest.raises(ValueError):
            evidence.state_event("run_1", "auth_1", 0, "paused")


# ---------------------------------------------------------------------------
# outbox: durable offline queue
# ---------------------------------------------------------------------------


class TestOutbox:
    def test_batches_flush_in_order_and_prune(self, tmp_path):
        outbox = EvidenceOutbox(tmp_path / "outbox")
        e1 = [evidence.state_event("run_1", "a", 0, "started")]
        e2 = evidence.failure_events(
            run_id="run_1", bundle_digest="e" * 64, authorization_id="a"
        )
        p1 = outbox.enqueue("run_1", e1)
        p2 = outbox.enqueue("run_1", e2)
        assert outbox.depth() == 2
        pending = list(outbox.pending())
        assert [p for _, p in pending] == [p1, p2]
        assert outbox.load(p1) == e1
        outbox.mark_sent(p1)
        outbox.mark_sent(p2)
        assert outbox.depth() == 0
        assert not (tmp_path / "outbox" / "run_1").exists()

    def test_rejected_batches_are_quarantined_not_retried(self, tmp_path):
        outbox = EvidenceOutbox(tmp_path / "outbox")
        path = outbox.enqueue(
            "run_1", [evidence.state_event("run_1", "a", 0, "started")]
        )
        moved = outbox.mark_rejected("run_1", path, "422: schema violation")
        assert moved.is_file()
        assert "422" in moved.with_suffix(".reason.txt").read_text(encoding="utf-8")
        assert outbox.depth() == 0

    def test_empty_batch_refused_and_malformed_load_raises(self, tmp_path):
        outbox = EvidenceOutbox(tmp_path / "outbox")
        with pytest.raises(ValueError):
            outbox.enqueue("run_1", [])
        bad = tmp_path / "outbox" / "run_1"
        bad.mkdir(parents=True)
        broken = bad / "000000.json"
        broken.write_text('{"nope": 1}', encoding="utf-8")
        with pytest.raises(ValueError, match="malformed"):
            outbox.load(broken)


# ---------------------------------------------------------------------------
# commands: mapping onto the existing governed entry points
# ---------------------------------------------------------------------------


class TestCommandMapping:
    def test_run_argv_pins_everything_local(self, sealed, config, tmp_path):
        workflow, bundle = sealed
        verdict = verified_or_refusal(workflow, config)
        assert not isinstance(verdict, Refusal)
        run_dir = tmp_path / "run"
        params_file = tmp_path / "params.json"
        argv = commands.build_run_argv(verdict, run_dir, params_file)
        assert argv[2:4] == ["openadapt_flow", "run"]
        assert str(bundle) in argv
        assert "--pin-digest" in argv
        assert argv[argv.index("--pin-digest") + 1] == (
            workflow.manifest.content_digest
        )
        assert "--params-file" in argv
        # not locally authorized => the escape hatches never appear
        assert "--approve-unverified-writes" not in argv
        assert "--allow-unencrypted" not in argv

    def test_resume_requires_local_approval_by_default(self, tmp_path):
        argv = commands.build_resume_argv(tmp_path / "run")
        assert argv[2:4] == ["openadapt_flow", "resume"]
        assert "--require-approval" in argv

    @pytest.mark.parametrize("verb", ["pause", "approve", "rollback-to-version"])
    def test_control_verbs_without_honest_mapping_refuse(self, verb, tmp_path):
        with pytest.raises(commands.UnmappedVerbError):
            commands.map_control_verb(verb, tmp_path / "run")

    def test_unknown_verb_refuses(self, tmp_path):
        with pytest.raises(commands.UnmappedVerbError, match="unknown"):
            commands.map_control_verb("self-update", tmp_path / "run")

    def test_run_is_not_a_control_verb(self, tmp_path):
        with pytest.raises(commands.UnmappedVerbError, match="leased dispatch"):
            commands.map_control_verb("run", tmp_path / "run")
