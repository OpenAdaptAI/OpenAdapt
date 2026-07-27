"""Tests for the EXPERIMENTAL runner loop (spec: hosted runner platform, P0 desktop lane).

Covers, against a FAKE cloud (httpx.MockTransport -- no network):
  * register -> poll -> lease -> execute -> evidence -> ack semantics;
  * refusal on ANY digest/authorization mismatch (before the flow engine runs);
  * uncertain-on-restart (never silently re-execute a started run);
  * PHI-free evidence conformance (forbidden fields never serialize, fail-closed);
  * the org's Tier-3 safety policy BINDING the run -- an admin's setting must
    change the config Flow is actually given, and an unenforceable policy must
    refuse before the flow engine is invoked.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

import httpx
import pytest
import yaml

from engine import policy as policy_mod
from engine.auth import store as auth_store
from engine.config import EngineConfig
from engine.db import IndexDB
from engine.dispatch import EngineDispatcher, EngineServices
from engine.runner_loop import (
    ACK_PATH,
    BACKOFF_CAP_S,
    EVIDENCE_SCHEMA,
    EXTEND_PATH,
    FORBIDDEN_EVIDENCE_KEYS,
    POLL_PATH,
    REGISTER_PATH,
    PhiBoundaryError,
    Refusal,
    RunnerClient,
    RunnerJournal,
    RunnerService,
    _counts_only,
    assert_phi_free,
    backoff_delay,
    bundle_content_digest,
    validate_dispatch,
)

HOST = "https://cloud.test"

# A report whose steps carry PHI booby traps that must NEVER cross the wire.
TRAPPED_REPORT = {
    "run_id": "run_1",
    "total_steps": 2,
    "steps": [
        {
            "step_id": "s1",
            "rung": "structural",
            "effect_contract_hashes": ["sha256:aa"],
            "effect_verified": True,
            "identity_verified": True,
            "elapsed_ms": 10,
            # traps:
            "field_values": {"patient": "SENSITIVE-NAME"},
            "target": "#mrn-field",
            "dom": "<input value='123-45-6789'>",
        },
        {
            "step_id": "s2",
            "rung": "template",
            "effect_contract_hashes": [],
            "effect_verified": False,
            "latency_ms": 20,
            "screenshot": "frame-004.png",
        },
    ],
    "metrics": {"duration_s": 1.5},
}

TRAPPED_HALT = {
    "kind": "effect_refuted",
    "substrate": "fhir",
    "effect_kind": "record_written",
    "contract_hash": "sha256:aa",
    "verdict": "refuted",
    "reason": "observed 2 records, expected 1",
    "suggested_action": "inspect the matched records and remove the duplicate(s)",
    "step_id": "s1",
    "rung": "template",
    "drift_signature": "sig-1",
    "evidence_digest": {
        "observed_count": 2,
        "expected_count": 1,
        # traps (values must never leave the box):
        "matched_records": ["SENSITIVE-RECORD"],
        "observed": ["SENSITIVE-VALUE"],
    },
    # traps:
    "matched_records": ["SENSITIVE-RECORD"],
    "field_values": {"mrn": "12345"},
}


class FlowResultStub:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.returncode = 0 if ok else 1
        self.stdout = ""
        self.stderr = ""


class FakeFlowBridge:
    """Fake openadapt-flow bridge writing a canned report.json.

    Captures the deployment config CONTENT Flow was handed, because "what the
    engine executed under" is the only thing that proves a policy actually
    bound.
    """

    def __init__(self, report: dict | None = None, ok: bool = True) -> None:
        self.report = report if report is not None else TRAPPED_REPORT
        self.ok = ok
        self.calls: list[dict] = []

    def run(self, bundle_dir: Path, config: Path, out_dir: Path | None = None,
            **kwargs: object) -> FlowResultStub:
        self.calls.append({
            "bundle_dir": bundle_dir,
            "out_dir": out_dir,
            "config": Path(config),
            "deployment": yaml.safe_load(Path(config).read_text()),
            **kwargs,
        })
        if out_dir is not None:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            (Path(out_dir) / "report.json").write_text(json.dumps(self.report))
        return FlowResultStub(self.ok)

    @property
    def runtimes(self) -> list[dict]:
        """The ``runtime`` section of every deployment config Flow received."""
        return [call["deployment"].get("runtime") or {} for call in self.calls]


# The operator's deployment config on the runner box. Deliberately PERMISSIVE
# where a Tier-3 setting can strengthen it, so a bound policy is visible.
BASE_DEPLOYMENT = {
    "name": "runner",
    "backend": {"kind": "web", "url": "https://records.test"},
}


def make_policy(source: str = "network", **safety_overrides: object) -> dict:
    """A resolved effective policy at the safe baseline, plus any overrides."""
    safety = dict(policy_mod.SAFE_SAFETY_DEFAULTS)
    safety.update(safety_overrides)
    return {
        "policy_version": 7,
        "baseline_version": "2026.07",
        "org_id": "org_1",
        "role": "admin",
        "is_admin": True,
        "user": {},
        "org": {},
        "safety": safety,
        "source": source,
    }


class FakePolicyResolver:
    """Stands in for the control plane's ``/api/policy/effective``.

    Mutable so a test can change the org's policy between two dispatches, which
    is exactly what an admin toggling a setting does.
    """

    def __init__(self) -> None:
        self.policy: dict = make_policy()
        self.error: Exception | None = None
        self.calls = 0

    def __call__(self) -> dict:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.policy


class FakeCloud:
    """Scripted /api/runners/* control plane behind httpx.MockTransport."""

    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.registrations: list[dict] = []
        self.acks: list[dict] = []
        self.evidence: list[dict] = []
        self.extends: list[dict] = []
        self.poll_count = 0
        self.poll_status: int | None = None  # force a status (401/500) when set
        self.ack_status: int | None = None
        self.bundles: dict[str, bytes] = {}  # url path -> zip bytes

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == REGISTER_PATH:
            self.registrations.append(
                {"payload": body, "auth": request.headers.get("Authorization")}
            )
            return httpx.Response(
                201, json={"runner_id": "rnr_1", "runner_token": "oar_test"}
            )
        if path == POLL_PATH:
            self.poll_count += 1
            if self.poll_status is not None:
                return httpx.Response(self.poll_status)
            if self.jobs:
                return httpx.Response(200, json={"job": self.jobs.pop(0)})
            return httpx.Response(204)
        if path == EXTEND_PATH:
            self.extends.append(body)
            return httpx.Response(200, json={"ok": True})
        if path == ACK_PATH:
            if self.ack_status is not None:
                return httpx.Response(self.ack_status)
            self.acks.append(
                {**body, "auth": request.headers.get("Authorization")}
            )
            return httpx.Response(200, json={"ok": True})
        if path.startswith("/api/runs/") and path.endswith("/evidence"):
            self.evidence.append(body)
            return httpx.Response(202, json={"ok": True})
        if path in self.bundles:
            return httpx.Response(200, content=self.bundles[path])
        return httpx.Response(404)


def make_bundle(config: EngineConfig) -> tuple[Path, str]:
    """Create a sealed-manifest bundle in the runner's digest-keyed store."""
    manifest = json.dumps({"workflow": "wf_1", "schema_version": 2}).encode()
    digest = hashlib.sha256(manifest).hexdigest()
    bundle_dir = config.data_dir / "runner" / "bundles" / digest
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "manifest.json").write_bytes(manifest)
    return bundle_dir, digest


def make_job(digest: str, run_id: str = "run_1", **overrides: object) -> dict:
    job = {
        "job_kind": "governed_run",
        "run_id": run_id,
        "workflow_id": "wf_1",
        "bundle": {"version_id": "bv_1", "content_digest": digest},
        "deployment_profile_id": "dp_1",
        "authorization": {
            "authorization_id": "auth_1",
            "created_at": "2099-01-01T00:00:00+00:00",
            "bundle_content_digest": digest,
            "runtime_inputs_digest": "0" * 64,
            "admitted_policy_name": "clinical-write",
            "required_identity_step_ids": [],
            "unverified_write_approvals": [],
            "approval_source": "hosted:app.openadapt.ai:approval_evt_1:user_1",
        },
        "expires_at": "2099-01-01T00:00:00+00:00",
        "lease": {"job_id": "job_1", "visibility_timeout_s": 900},
    }
    job.update(overrides)
    return job


def login(host: str = HOST) -> None:
    auth_store.store_credential({
        "kind": "ingest_token", "token": "sess-token", "refresh_token": None,
        "org_id": "org_1", "host": host, "expires_at": None,
    })


@pytest.fixture
def rig(tmp_path: Path):
    """Config + real IndexDB + fake flow bridge + fake cloud + RunnerService.

    A runner that can execute at all has a deployment config on disk and can
    reach (or has cached) its org policy; the fixture provides both, and tests
    remove or corrupt them to exercise the refusal paths. The resolver is
    reachable as ``svc._policy_resolver``.
    """
    config = EngineConfig(
        data_dir=tmp_path / ".openadapt", hosted_host=HOST, runner_enabled=True,
        log_level="WARNING",
    )
    config.data_dir.mkdir(parents=True, exist_ok=True)
    (config.data_dir / "deployment.json").write_text(json.dumps(BASE_DEPLOYMENT))
    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    cloud = FakeCloud()
    flow = FakeFlowBridge()
    services = EngineServices(config, db=db, flow_bridge=flow)
    events: list[tuple[str, dict]] = []
    transport = httpx.MockTransport(cloud.handler)
    svc = RunnerService(
        config, services,
        emit=lambda e, d: events.append((e, d)),
        http_factory=lambda: httpx.AsyncClient(base_url=HOST, transport=transport),
        rng=random.Random(0),
        policy_resolver=FakePolicyResolver(),
    )
    yield svc, cloud, flow, config, db, events
    db.close()


async def run_loop(svc: RunnerService, ticks: int = 1) -> RunnerClient:
    """Drive the loop body directly (register + reconcile + N ticks), no thread."""
    async with svc._http_factory() as http:
        client = RunnerClient(http)
        assert await svc.ensure_registered(client)
        await svc.reconcile_restart(client)
        for _ in range(ticks):
            await svc._tick(client)
        return client


def all_wire_payloads(cloud: FakeCloud) -> str:
    return json.dumps(cloud.evidence + cloud.acks + cloud.registrations)


# ------------------------------------------------------------------ happy path


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_register_poll_lease_execute_callback_ack(self, rig) -> None:
        svc, cloud, flow, config, db, _events = rig
        login()
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        # register: session bearer used; runner token stored in the keychain
        assert cloud.registrations[0]["auth"] == "Bearer sess-token"
        cred = auth_store.load_runner_credential(HOST)
        assert cred == {"runner_id": "rnr_1", "runner_token": "oar_test"}

        # execution went through the existing flow bridge exactly once
        assert len(flow.calls) == 1
        # the authorization JSON is persisted in the run dir (operator audit copy)
        run_dir = config.data_dir / "runner" / "runs" / "run_1"
        auth_json = json.loads((run_dir / "authorization.json").read_text())
        assert auth_json["authorization_id"] == "auth_1"

        # evidence: started state, one step event per step, terminal summary
        kinds = [e["kind"] for e in cloud.evidence]
        assert kinds == ["state", "step", "step", "run_summary"]
        assert all(e["schema"] == EVIDENCE_SCHEMA for e in cloud.evidence)
        assert all(e["run_id"] == "run_1" for e in cloud.evidence)
        assert all(e["authorization_id"] == "auth_1" for e in cloud.evidence)
        seqs = [e["seq"] for e in cloud.evidence]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

        summary = cloud.evidence[-1]["run_summary"]
        assert summary["status"] == "confirmed"
        assert summary["bundle_digest"] == digest
        assert summary["screenshots_may_leave_box"] is False
        assert summary["effects_confirmed"] == 1

        # terminal ack with the runner token
        assert cloud.acks[-1]["job_id"] == "job_1"
        assert cloud.acks[-1]["outcome"] == "confirmed"
        assert cloud.acks[-1]["auth"] == "Bearer oar_test"

        # journal reached terminal phase
        entry = svc.journal.get("run_1")
        assert entry["phase"] == "finished"
        assert entry["outcome"] == "confirmed"

    @pytest.mark.asyncio
    async def test_halt_reports_reconciliation_task_fields(self, rig) -> None:
        svc, cloud, flow, config, db, _events = rig
        login()
        _bundle, digest = make_bundle(config)
        flow.report = {**TRAPPED_REPORT, "halt": TRAPPED_HALT}
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        halt_events = [e for e in cloud.evidence if e["kind"] == "halt"]
        assert len(halt_events) == 1
        halt = halt_events[0]["halt"]
        assert halt["kind"] == "effect_refuted"
        assert halt["substrate"] == "fhir"
        assert halt["verdict"] == "refuted"
        assert halt["contract_hash"] == "sha256:aa"
        # counts ONLY -- observed/expected VALUES and matched_records stripped
        assert halt["evidence_digest"] == {"observed_count": 2, "expected_count": 1}
        assert cloud.evidence[-1]["run_summary"]["status"] == "halted-needs-attention"
        assert cloud.acks[-1]["outcome"] == "halted-needs-attention"
        # halt mirrored into the local needs-attention badge
        assert db.count_open_halts() == 1

    @pytest.mark.asyncio
    async def test_bundle_staged_from_signed_url(self, rig, tmp_path: Path) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        import io
        import zipfile

        manifest = json.dumps({"workflow": "wf_remote"}).encode()
        digest = hashlib.sha256(manifest).hexdigest()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", manifest)
        cloud.bundles["/bundles/bv_9.zip"] = buf.getvalue()
        job = make_job(digest, run_id="run_9")
        job["bundle"]["url"] = f"{HOST}/bundles/bv_9.zip"
        job["lease"] = {"job_id": "job_9", "visibility_timeout_s": 900}
        cloud.jobs.append(job)

        await run_loop(svc, ticks=1)

        assert len(flow.calls) == 1
        assert cloud.acks[-1]["outcome"] == "confirmed"
        staged = config.data_dir / "runner" / "bundles" / digest / "manifest.json"
        assert staged.is_file()


# ------------------------------------------------------------------ refusal


class TestRefusal:
    @pytest.mark.asyncio
    async def test_refuses_on_local_digest_mismatch(self, rig) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        _bundle, digest = make_bundle(config)
        wrong = "f" * 64
        job = make_job(digest)
        # cloud claims a different digest than the locally staged bundle
        job["bundle"]["content_digest"] = wrong
        job["authorization"]["bundle_content_digest"] = wrong
        # park the tampered bundle where the staging step will find it
        (config.data_dir / "runner" / "bundles" / digest).rename(
            config.data_dir / "runner" / "bundles" / wrong
        )
        cloud.jobs.append(job)

        await run_loop(svc, ticks=1)

        assert flow.calls == []  # flow engine NEVER invoked
        assert cloud.evidence == []  # nothing streamed for a refused run
        ack = cloud.acks[-1]
        assert ack["outcome"] == "refused"
        assert "digest mismatch" in ack["reason"]
        # refusal reasons are digest-prefix-only, never full paths/values
        assert str(config.data_dir) not in ack["reason"]
        entry = svc.journal.get("run_1")
        assert entry["phase"] == "finished" and entry["outcome"] == "refused"

    @pytest.mark.asyncio
    async def test_refuses_when_dispatch_and_authorization_digests_disagree(
        self, rig
    ) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        _bundle, digest = make_bundle(config)
        job = make_job(digest)
        job["authorization"]["bundle_content_digest"] = "e" * 64
        cloud.jobs.append(job)

        await run_loop(svc, ticks=1)

        assert flow.calls == []
        assert cloud.acks[-1]["outcome"] == "refused"

    @pytest.mark.asyncio
    async def test_refuses_expired_dispatch(self, rig) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        _bundle, digest = make_bundle(config)
        job = make_job(digest)
        job["expires_at"] = "2000-01-01T00:00:00+00:00"
        cloud.jobs.append(job)

        await run_loop(svc, ticks=1)

        assert flow.calls == []
        assert cloud.acks[-1]["outcome"] == "refused"
        assert "expired" in cloud.acks[-1]["reason"]

    def test_validate_dispatch_refuses_tampered_manifest(self, tmp_path: Path) -> None:
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "manifest.json").write_text(
            json.dumps({"content_digest": "a" * 64})
        )
        with pytest.raises(Refusal, match="self-digest mismatch"):
            bundle_content_digest(bundle)

    def test_validate_dispatch_refuses_missing_authorization(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(Refusal, match="authorization"):
            validate_dispatch(
                {"job_kind": "governed_run", "run_id": "r"}, tmp_path
            )


# ------------------------------------------------------------------ safety policy


class TestSafetyPolicyBinding:
    """The governed safety policy must BIND a run, not merely resolve.

    These are effect tests, not resolver tests: each one asserts on the
    deployment config the flow engine was actually handed (or on the refusal
    that stopped it), because a control that renders and reports success while
    changing nothing about execution is the exact failure mode being guarded.
    """

    @pytest.mark.asyncio
    async def test_admin_toggle_changes_what_the_run_executes(self, rig) -> None:
        # THE regression test. Same bundle, same dispatch, same runner: the only
        # difference between the two runs is the org's Tier-3 setting.
        svc, cloud, flow, config, _db, _events = rig
        login()
        _bundle, digest = make_bundle(config)

        # 1) baseline policy: pixel-identity verification is not required.
        cloud.jobs.append(make_job(digest, run_id="run_before"))
        await run_loop(svc, ticks=1)
        assert flow.runtimes[0].get("pixel_verify_enabled") is not True

        # 2) an admin turns the safety control on in the dashboard.
        svc._policy_resolver.policy = make_policy(
            **{"pixel_verify.consequential_policy": "required"}
        )

        cloud.jobs.append(make_job(digest, run_id="run_after"))
        await run_loop(svc, ticks=1)

        assert len(flow.calls) == 2
        assert flow.runtimes[1]["pixel_verify_enabled"] is True
        assert cloud.acks[-1]["outcome"] == "confirmed"

    @pytest.mark.asyncio
    async def test_model_call_prohibition_overrides_a_permissive_config(
        self, rig
    ) -> None:
        # The local config opts into model grounding; the platform policy says
        # a healthy run makes no model calls. The policy wins.
        svc, cloud, flow, config, _db, _events = rig
        login()
        (config.data_dir / "deployment.json").write_text(
            json.dumps({**BASE_DEPLOYMENT, "runtime": {"allow_model_grounding": True}})
        )
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        assert flow.runtimes[0]["allow_model_grounding"] is False

    @pytest.mark.asyncio
    async def test_demo_profile_is_escalated_never_lowered(self, rig) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        (config.data_dir / "deployment.json").write_text(
            json.dumps({**BASE_DEPLOYMENT, "runtime": {"profile": "demo"}})
        )
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        # demo enforces no effect contracts and no identity coverage; the
        # baseline policy requires both, so the run is escalated.
        assert flow.runtimes[0]["profile"] == "standard"

    @pytest.mark.asyncio
    async def test_regulated_profile_is_left_alone(self, rig) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        (config.data_dir / "deployment.json").write_text(
            json.dumps({**BASE_DEPLOYMENT, "runtime": {"profile": "regulated"}})
        )
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        assert flow.runtimes[0]["profile"] == "regulated"

    @pytest.mark.asyncio
    async def test_absent_profile_stays_absent(self, rig) -> None:
        # Flow resolves an omitted profile to `regulated`. Writing `standard`
        # in would WEAKEN the run, so the binding must not invent one.
        svc, cloud, flow, config, _db, _events = rig
        login()
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        assert "profile" not in flow.runtimes[0]

    @pytest.mark.asyncio
    async def test_policy_is_resolved_before_every_run(self, rig) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest, run_id="run_a"))
        cloud.jobs.append(make_job(digest, run_id="run_b"))

        await run_loop(svc, ticks=2)

        assert len(flow.calls) == 2
        assert svc._policy_resolver.calls == 2

    @pytest.mark.asyncio
    async def test_bound_config_does_not_outlive_the_run(self, rig) -> None:
        # The policy-bound config is staged privately for the invocation only;
        # a PHI-capable deployment snapshot must not be left on disk.
        svc, cloud, flow, config, _db, _events = rig
        login()
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        assert not flow.calls[0]["config"].exists()

    @pytest.mark.asyncio
    async def test_journal_records_the_policy_the_run_bound(self, rig) -> None:
        svc, cloud, _flow, config, _db, _events = rig
        login()
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        entry = svc.journal.get("run_1")
        assert entry["policy_source"] == "network"
        assert entry["policy_version"] == 7


class TestSafetyPolicyFailsClosed:
    """Every way the policy can be unavailable or unusable must REFUSE."""

    @pytest.mark.asyncio
    async def test_refuses_when_no_authoritative_policy_exists(self, rig) -> None:
        # No control plane and no cache. The safest values are populated, but
        # they are the engine's guess -- an org that STRENGTHENED a setting
        # would be silently run without it. Refuse instead.
        svc, cloud, flow, config, _db, _events = rig
        login()
        svc._policy_resolver.policy = make_policy(source="fail-closed-default")
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        assert flow.calls == []  # flow engine NEVER invoked
        assert cloud.evidence == []
        assert cloud.acks[-1]["outcome"] == "refused"
        assert "no authoritative safety policy" in cloud.acks[-1]["reason"]
        entry = svc.journal.get("run_1")
        assert entry["phase"] == "finished" and entry["outcome"] == "refused"

    @pytest.mark.asyncio
    async def test_refuses_on_a_safety_value_outside_its_domain(self, rig) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        svc._policy_resolver.policy = make_policy(
            **{"identity_gate.strictness": "medium"}
        )
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        assert flow.calls == []
        assert cloud.acks[-1]["outcome"] == "refused"
        assert "identity_gate.strictness" in cloud.acks[-1]["reason"]

    @pytest.mark.asyncio
    async def test_refuses_on_a_missing_safety_key(self, rig) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        policy = make_policy()
        del policy["safety"]["halt_on_ambiguous"]
        svc._policy_resolver.policy = policy
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        assert flow.calls == []
        assert cloud.acks[-1]["outcome"] == "refused"

    @pytest.mark.asyncio
    async def test_refuses_when_resolution_raises(self, rig) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        svc._policy_resolver.error = RuntimeError("keychain exploded")
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        assert flow.calls == []
        assert cloud.acks[-1]["outcome"] == "refused"
        # the reason names the failure class, never its (possibly sensitive) text
        assert "keychain exploded" not in cloud.acks[-1]["reason"]

    @pytest.mark.asyncio
    async def test_refuses_when_the_deployment_config_is_missing(self, rig) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        (config.data_dir / "deployment.json").unlink()
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        assert flow.calls == []
        assert cloud.acks[-1]["outcome"] == "refused"

    @pytest.mark.asyncio
    async def test_refuses_on_an_unreadable_deployment_config(self, rig) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        (config.data_dir / "deployment.json").write_text("{ not: [valid")
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        assert flow.calls == []
        assert cloud.acks[-1]["outcome"] == "refused"
        # the refusal reason must not echo config contents
        assert "not: [valid" not in cloud.acks[-1]["reason"]

    @pytest.mark.asyncio
    async def test_refuses_on_an_unrankable_execution_profile(self, rig) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        (config.data_dir / "deployment.json").write_text(
            json.dumps({**BASE_DEPLOYMENT, "runtime": {"profile": "yolo"}})
        )
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        assert flow.calls == []
        assert cloud.acks[-1]["outcome"] == "refused"

    @pytest.mark.asyncio
    async def test_refusal_reasons_stay_phi_free(self, rig) -> None:
        svc, cloud, _flow, config, _db, _events = rig
        login()
        svc._policy_resolver.policy = make_policy(source="fail-closed-default")
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        wire = all_wire_payloads(cloud)
        assert "records.test" not in wire
        assert str(config.data_dir) not in wire


# ------------------------------------------------------------------ idempotency


class TestUncertainOnRestart:
    @pytest.mark.asyncio
    async def test_started_run_is_reported_uncertain_never_rerun(self, rig) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        # A previous process leased + started this run, then died.
        svc.journal.record("run_1", "leased", job_id="job_1")
        svc.journal.record("run_1", "started")

        await run_loop(svc, ticks=0)  # reconcile only

        assert flow.calls == []
        ack = cloud.acks[-1]
        assert ack["outcome"] == "uncertain"
        assert ack["job_id"] == "job_1"
        assert ack["run_id"] == "run_1"
        assert "not re-executed" in ack["reason"]
        entry = svc.journal.get("run_1")
        assert entry["phase"] == "finished" and entry["outcome"] == "uncertain"

    @pytest.mark.asyncio
    async def test_releases_of_started_run_report_uncertain(self, rig) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        svc.journal.record("run_1", "leased", job_id="job_0")
        svc.journal.record("run_1", "started")
        _bundle, digest = make_bundle(config)
        cloud.jobs.append(make_job(digest))  # the cloud re-leases the same run

        async with svc._http_factory() as http:
            client = RunnerClient(http, token="oar_test")
            await svc.handle_job(client, make_job(digest))

        assert flow.calls == []
        assert cloud.acks[-1]["outcome"] == "uncertain"

    @pytest.mark.asyncio
    async def test_failed_uncertain_ack_stays_started_but_never_reruns(
        self, rig
    ) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        svc.journal.record("run_1", "leased", job_id="job_1")
        svc.journal.record("run_1", "started")
        cloud.ack_status = 500  # ack cannot land yet

        await run_loop(svc, ticks=0)

        entry = svc.journal.get("run_1")
        assert entry["phase"] == "started"  # retried at the next start
        assert flow.calls == []  # and still never re-executed

    @pytest.mark.asyncio
    async def test_duplicate_lease_of_finished_run_reacks_same_outcome(
        self, rig
    ) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        _bundle, digest = make_bundle(config)
        svc.journal.record("run_1", "leased", job_id="job_1")
        svc.journal.record("run_1", "finished", outcome="confirmed")

        async with svc._http_factory() as http:
            client = RunnerClient(http, token="oar_test")
            await svc.handle_job(client, make_job(digest))

        assert flow.calls == []
        assert cloud.acks[-1]["outcome"] == "confirmed"


# ------------------------------------------------------------------ PHI boundary


class TestPhiBoundary:
    @pytest.mark.asyncio
    async def test_no_forbidden_field_ever_serializes(self, rig) -> None:
        svc, cloud, flow, config, _db, _events = rig
        login()
        _bundle, digest = make_bundle(config)
        flow.report = {**TRAPPED_REPORT, "halt": TRAPPED_HALT}
        cloud.jobs.append(make_job(digest))

        await run_loop(svc, ticks=1)

        wire = all_wire_payloads(cloud)
        for forbidden in FORBIDDEN_EVIDENCE_KEYS:
            assert f'"{forbidden}"' not in wire, forbidden
        assert "SENSITIVE" not in wire
        assert "123-45-6789" not in wire
        assert "frame-004.png" not in wire

    def test_assert_phi_free_fails_closed(self) -> None:
        with pytest.raises(PhiBoundaryError):
            assert_phi_free({"step": {"field_values": {"a": 1}}})
        with pytest.raises(PhiBoundaryError):
            assert_phi_free({"halt": [{"evidence": {"matched_records": []}}]})
        with pytest.raises(PhiBoundaryError):
            assert_phi_free({"screenshot": "x"})
        assert_phi_free({"step": {"step_id": "s1", "effect_contract_hashes": []}})

    def test_counts_only_strips_values(self) -> None:
        out = _counts_only({
            "observed_count": 2, "expected_count": 1, "matched_records": ["x"],
            "observed": ["v"], "flag_count": True, "note": "hi",
        })
        assert out == {"observed_count": 2, "expected_count": 1}

    @pytest.mark.asyncio
    async def test_client_rejects_phi_event_before_wire(self, rig) -> None:
        svc, cloud, _flow, _config, _db, _events = rig
        async with svc._http_factory() as http:
            client = RunnerClient(http, token="t")
            with pytest.raises(PhiBoundaryError):
                await client.post_evidence(
                    "run_1", {"kind": "step", "step": {"dom": "<html>"}}
                )
        assert cloud.evidence == []


# ------------------------------------------------------------------ transport


class TestTransport:
    def test_backoff_is_exponential_jittered_and_capped(self) -> None:
        rng = random.Random(42)
        for attempt in range(10):
            delay = backoff_delay(attempt, rng)
            exp = min(BACKOFF_CAP_S, 2.0 ** attempt)
            assert exp / 2 <= delay <= exp
        assert backoff_delay(30, rng) <= BACKOFF_CAP_S

    @pytest.mark.asyncio
    async def test_401_surfaces_reauth_and_stops_polling(self, rig) -> None:
        svc, cloud, _flow, _config, _db, events = rig
        login()
        cloud.poll_status = 401

        async with svc._http_factory() as http:
            client = RunnerClient(http, token="oar_test")
            delay = await svc._tick(client)

        assert delay is None  # loop stops; NO retry-loop on an invalid token
        assert svc.status()["state"] == "reauth_required"
        assert any(e == "runner_state" for e, _ in events)

    @pytest.mark.asyncio
    async def test_5xx_backs_off_then_recovers(self, rig) -> None:
        svc, cloud, _flow, _config, _db, _events = rig
        login()
        cloud.poll_status = 500

        async with svc._http_factory() as http:
            client = RunnerClient(http, token="oar_test")
            first = await svc._tick(client)
            second = await svc._tick(client)
            cloud.poll_status = None
            recovered = await svc._tick(client)

        assert first is not None and first > 0
        assert second is not None and second > 0
        assert recovered == 0.0  # 204 -> immediate re-poll, backoff reset
        assert svc._attempt == 0

    @pytest.mark.asyncio
    async def test_registration_required_before_polling(self, rig) -> None:
        svc, _cloud, _flow, _config, _db, _events = rig
        # No session credential and no runner credential stored.
        async with svc._http_factory() as http:
            client = RunnerClient(http)
            assert await svc.ensure_registered(client) is False
        assert svc.status()["state"] == "reauth_required"


# ------------------------------------------------------------------ journal / verbs


class TestJournalAndVerbs:
    def test_journal_phases_and_last_runs(self, tmp_path: Path) -> None:
        journal = RunnerJournal(tmp_path / "jobs")
        journal.record("run_a", "leased", job_id="j1")
        journal.record("run_a", "started")
        journal.record("run_b", "leased", job_id="j2")
        assert [e["run_id"] for e in journal.unfinished_started()] == ["run_a"]
        journal.record("run_a", "finished", outcome="confirmed")
        assert journal.unfinished_started() == []
        runs = journal.last_runs()
        assert {r["run_id"] for r in runs} == {"run_a", "run_b"}
        # last_runs exposes only PHI-free bookkeeping fields
        assert all(
            set(r) <= {"run_id", "phase", "outcome", "reason", "updated_at",
                       "workflow_id"}
            for r in runs
        )

    def test_dispatcher_runner_verbs(self, rig, monkeypatch) -> None:
        svc, _cloud, _flow, config, db, _events = rig
        services = EngineServices(config, db=db, runner=svc)
        disp = EngineDispatcher(config, services=services)
        # never write the real ~/.openadapt/config.toml from tests
        monkeypatch.setattr(disp, "_persist_config_key", lambda k, v: None)
        monkeypatch.setattr(svc, "start", lambda: None)
        monkeypatch.setattr(svc, "stop", lambda: None)

        assert {"runner_status", "runner_enable", "runner_disable"} <= set(
            disp.commands
        )
        status = disp.dispatch("runner_status", {})
        assert set(status) >= {"enabled", "state", "last_runs"}
        enabled = disp.dispatch("runner_enable", {})
        assert enabled["enabled"] is True
        disabled = disp.dispatch("runner_disable", {})
        assert disabled["enabled"] is False
        assert disabled["state"] == "disabled"
