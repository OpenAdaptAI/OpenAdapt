"""Synthetic local attribution; no actor, producer signature or qualification."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from openadapt_flow import __main__ as cli
from openadapt_flow.deployment import DeploymentConfig
from openadapt_flow.emit import mcp_governed as adapter
from openadapt_flow.ir import RunReport
from openadapt_flow.run_invocation import (
    InvocationContext,
    derive_context,
    prepare_producer_context,
)
from tests.test_mcp_governed import report_for, runner, setup

CHALLENGE = "a" * 64


def cfg_for(surface="web"):
    return DeploymentConfig.model_validate(
        {"backend": {"kind": surface}, "runtime": {"profile": "standard"}}
    )


def context(cfg, challenge=CHALLENGE, **overrides):
    return derive_context(
        challenge,
        cfg=cfg,
        backend=overrides.get("backend", cfg.backend),
        effects=overrides.get("effects", cfg.effects),
        actuation=overrides.get("actuation", cfg.actuation),
        profile="standard",
    )


@pytest.mark.parametrize(
    "surface,field",
    [
        ("web", "url"),
        ("windows", "agent_url"),
        ("macos", "macos_app"),
        ("linux", "linux_app"),
        ("rdp", "rdp_window_title"),
        ("citrix", "rdp_window_title"),
    ],
)
def test_actual_resolved_target_and_readback_each_bind_descriptor(
    surface, field, monkeypatch
):
    cfg = cfg_for(surface)
    old = context(cfg)
    target = cfg.backend.model_copy(update={field: "synthetic-changed-target"})
    changed_effects = cfg.effects.model_copy(
        update={"records_path": "/synthetic-other-records"}
    )
    changed_actuation = cfg.actuation.model_copy(
        update={"base_url": "https://other.invalid"}
    )
    # Actual resolved values change identity even if the parsed config is unchanged.
    assert context(cfg, backend=target).descriptor_sha256 != old.descriptor_sha256
    assert (
        context(cfg, effects=changed_effects).descriptor_sha256 != old.descriptor_sha256
    )
    assert (
        context(cfg, actuation=changed_actuation).descriptor_sha256
        != old.descriptor_sha256
    )
    # Credential/environment resolution is not a context input.
    monkeypatch.setenv("SYNTHETIC_PASSWORD", "first-unresolved-value")
    first = context(cfg)
    monkeypatch.setenv("SYNTHETIC_PASSWORD", "second-unresolved-value")
    assert context(cfg) == first


@pytest.mark.parametrize(
    "field,value",
    [
        ("challenge", []),
        ("challenge", True),
        ("challenge", 1),
        ("challenge", "a" * 63),
        ("challenge", "a" * 64 + "\n"),
        ("challenge", "A" * 64),
        ("descriptor_sha256", []),
        ("descriptor_sha256", True),
        ("descriptor_sha256", "a" * 64 + "\n"),
        ("profile", ["standard"]),
        ("profile", "regulated"),
        ("version", ["openadapt.local-invocation/v1"]),
        ("qualified", True),
    ],
)
def test_context_is_closed_strict_and_bounded(field, value):
    data = context(cfg_for()).model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValidationError):
        InvocationContext.model_validate(data)


def test_report_compatibility_and_profile_binding():
    # Existing reports without invocation evidence keep their original shape.
    old = RunReport(workflow_name="synthetic", started_at="2000-01-01T00:00:00Z")
    assert "invocation_context" not in old.model_dump(mode="json")
    value = old.model_dump(mode="json")
    value["invocation_context"] = context(cfg_for()).model_dump(mode="json")
    with pytest.raises(ValidationError, match="invocation profile"):
        RunReport.model_validate(value)


@pytest.mark.parametrize(
    "value", ["", "a" * 63, "a" * 65, "A" * 64, "a" * 64 + "\n", "x" * 10000]
)
def test_invalid_challenge_refuses_at_parser_before_dispatch(value):
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as error:
        parser.parse_args(
            ["run", "no-bundle-will-be-opened", "--invocation-challenge", value]
        )
    assert error.value.code == 2


def test_actual_producer_derives_not_echoes_request(monkeypatch):
    cfg = cfg_for("windows")
    actual_backend = cfg.backend.model_copy(update={"agent_url": "http://127.0.0.1:2"})
    args = SimpleNamespace(
        profile="standard",
        _execution_profile="standard",
        config="synthetic",
        config_sha256="b" * 64,
        invocation_challenge=CHALLENGE,
        expected_descriptor_sha256="0" * 64,
    )
    prepare_producer_context(
        args, cfg, actual_backend, None, cfg.effects, cfg.actuation
    )
    assert args._run_invocation_context == context(cfg, backend=actual_backend)
    assert args._run_invocation_context != context(cfg)
    assert (
        args._run_invocation_context.descriptor_sha256
        != args.expected_descriptor_sha256
    )


@pytest.mark.parametrize("unsupported", ["regulated", "repair", "pool", "multisurface"])
def test_known_unsupported_producer_refuses_without_child(
    tmp_path, monkeypatch, unsupported
):
    wf, bundle, options = setup(tmp_path)
    data = json.loads(Path(options["config"]).read_text())
    if unsupported == "regulated":
        options["profile"] = "regulated"
    elif unsupported == "repair":
        data["repair"] = {"agent": "synthetic:NoImport"}
    elif unsupported == "pool":
        data["worklist"] = {"accounts": [{"name": "synthetic", "params_env": {}}]}
    else:
        data["surfaces"] = {"other": {"kind": "windows"}}
    DeploymentConfig.model_validate(
        data
    )  # Refuse a valid unsupported posture, not malformed data.
    Path(options["config"]).write_text(json.dumps(data))
    monkeypatch.setattr(
        adapter.subprocess, "run", lambda *a, **k: pytest.fail("no child may launch")
    )
    assert (
        adapter.run_governed(bundle, {"note": "synthetic"}, **options)["outcome"]
        == "REFUSED"
    )


@pytest.mark.parametrize(
    "mode,surface", [("hardlink", "web"), ("copy", "web"), ("target", "windows")]
)
@pytest.mark.parametrize("origin", ["local", "pulled"])
def test_stale_report_occurrence_cannot_become_current_local_claim(
    tmp_path, monkeypatch, mode, surface, origin
):
    wf, bundle, options = setup(tmp_path, surface, origin)
    params = {"note": "same synthetic inputs"}
    report = report_for(wf, params)
    report.started_at = "2000-01-01T00:00:00Z"
    runner(monkeypatch, report)
    first = adapter.run_governed(bundle, params, **options)
    assert first["outcome"] == "UNCERTAIN" and first["production_eligible"] is False
    assert first["runtime_reported"]["outcome"] == "VERIFIED"  # synthetic claim only
    old = Path(first["run_dir"]) / "report.json"
    if mode == "target":
        data = json.loads(Path(options["config"]).read_text())
        data["backend"]["agent_url"] = "http://127.0.0.1:2"
        Path(options["config"]).write_text(json.dumps(data))

    def stale(argv, **kwargs):
        dest = Path(argv[argv.index("--run-dir") + 1]) / "report.json"
        if mode == "hardlink":
            os.link(old, dest)
        else:
            dest.write_bytes(old.read_bytes())
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(adapter.subprocess, "run", stale)
    second = adapter.run_governed(bundle, params, **options)
    assert second["outcome"] == "UNCERTAIN" and second["production_eligible"] is False
    assert (
        second["runtime_reported"] is None
        and second["producer_assurance"] == "UNATTRIBUTED"
    )
    assert second["execution_may_have_occurred"] is True
    assert second["retry_allowed"] is False


@pytest.mark.parametrize("field", ["success", "production_eligible", "retry_allowed"])
@pytest.mark.parametrize("value", [True, 1, 0, "false", []])
def test_outer_observation_can_never_promote_claims(field, value):
    data = adapter._refused()
    data[field] = value
    with pytest.raises(ValidationError):
        adapter.MCPObservation.model_validate(data)


@pytest.mark.parametrize("mode", ["legacy", "correlated"])
def test_common_report_producer_carries_context_before_persistence(
    tmp_path, monkeypatch, mode
):
    # Exercise real common producer with only fake backend/vision, stopping at
    # its existing Standard runtime refusal. No real actor or authority/key.
    from openadapt_flow import at_rest, crypto
    from openadapt_flow.ir import Step, Workflow
    from openadapt_flow.runtime.authorization import (
        GovernedRunAuthorization,
        runtime_inputs_digest,
    )
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_replayer import FakeBackend, FakeVision

    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(at_rest, "plaintext_allowed", lambda *a: True)
    monkeypatch.setattr(
        crypto, "resolve_key", lambda *a, **k: pytest.fail("no real key resolution")
    )
    wf = Workflow(
        name="synthetic producer",
        steps=[Step(id="wait", intent="observe", action="wait")],
    )
    bundle = tmp_path / "bundle"
    wf.save(bundle)
    wf = Workflow.load(bundle)
    authorization = GovernedRunAuthorization(
        bundle_content_digest=wf.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(wf, None, None),
        admitted_policy_name="synthetic",
        execution_profile="standard",
    )
    backend = FakeBackend()
    observed = []
    save = RunReport.save

    def before_save(self, *args, **kwargs):
        observed.append(self.invocation_context)
        return save(self, *args, **kwargs)

    monkeypatch.setattr(RunReport, "save", before_save)
    ctx = context(
        cfg_for(),
        hashlib.sha256(b"synthetic nonsecret invocation challenge").hexdigest(),
    )
    if mode == "legacy":
        authorization, ctx = None, None
    report = Replayer(
        backend,
        vision=FakeVision(),
        governed_authorization=authorization,
        checkpoint_key="SYNTHETIC_UNUSED_KEY",
    ).run(wf, bundle_dir=bundle, run_dir=tmp_path / "run", invocation_context=ctx)
    assert observed and all(item == ctx for item in observed)
    assert report.invocation_context == ctx and backend.actions == []
    raw = json.loads((tmp_path / "run/report.json").read_text())
    if mode == "legacy":
        assert report.success is True and "invocation_context" not in raw
    else:
        assert report.success is False  # Existing Standard refusal, no qualified run.
        assert (
            report.run_id_sha256
            == hashlib.sha256(authorization.authorization_id.encode()).hexdigest()
        )
        assert (
            report.run_id_sha256 != hashlib.sha256(ctx.challenge.encode()).hexdigest()
        )
        assert raw["invocation_context"] == ctx.model_dump(mode="json")
