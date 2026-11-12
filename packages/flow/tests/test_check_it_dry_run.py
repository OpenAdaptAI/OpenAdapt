"""No-input Check-it controls; actual browser/GTK cases live in the companion test."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openadapt_flow.approval_mode.dry_run import DryRunGate
from openadapt_flow.approval_mode.gate import SaveContext
from openadapt_flow.ir import ActionKind, IdentityCheck, Step, StepResult
from openadapt_flow.runtime.replayer import Replayer
from tests.test_execution_profiles import _KEY, _key_workflow, _ReadyVision, _sealed
from tests.test_replayer import FakeBackend


def _context(workflow, result, prior=()):
    return SaveContext(
        step=workflow.steps[-1],
        workflow=workflow,
        result=result,
        prior_results=prior,
        effect_verifier=None,
        governed_authorization=None,
        model_calls=0,
        frame=lambda: None,
    )


@pytest.mark.parametrize(
    "fault",
    [None, "identity", "readiness", "not_observed", "prior_input", "prior_unknown"],
)
def test_gate_requires_actual_affirmative_checks_and_zero_delivery(fault):
    workflow = _key_workflow("dry", with_effect=True)
    result = StepResult(
        step_id="submit",
        intent="submit",
        ok=False,
        identity=IdentityCheck(status="verified", coverage=1.0),
        delivery_attempted=False,
        starting_state_settled=fault != "readiness",
    )
    prior = []
    if fault == "identity":
        result.identity = None
    if fault in {"prior_input", "prior_unknown"}:
        prior = [
            StepResult(
                step_id="prefix",
                intent="prefix",
                ok=True,
                delivery_attempted=True if fault == "prior_input" else None,
            )
        ]
    gate = DryRunGate()
    if fault != "not_observed":
        gate.record_preconditions(workflow.steps[-1], result)
    verdict = gate.before_save(_context(workflow, result, prior))
    assert not verdict.proceed
    assert verdict.decision.reason == (
        "dry_run" if fault is None else "dry_run_refused"
    )
    assert gate.approval_store is None
    assert gate._approver_keys == {}


def test_reused_gate_does_not_reuse_a_prior_run_check():
    workflow = _key_workflow("dry", with_effect=True)
    result = StepResult(
        step_id="submit",
        intent="submit",
        ok=False,
        identity=IdentityCheck(status="verified", coverage=1.0),
        delivery_attempted=False,
    )
    gate = DryRunGate()
    gate.record_preconditions(workflow.steps[-1], result)
    gate.begin_item(run_id="next", bundle_content_digest=None, profile="demo")
    assert (
        gate.before_save(_context(workflow, result)).decision.reason
        == "dry_run_refused"
    )


@pytest.mark.parametrize("prefix", [None, "wait", "type", "click", "key"])
def test_real_replayer_stops_without_input_or_verifier_preparation(
    tmp_path, monkeypatch, prefix
):
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    workflow = _key_workflow("no-input", with_effect=True)
    if prefix is not None:
        step = Step(
            id="prefix",
            intent="unproved prefix",
            action=ActionKind(prefix),
            key="Tab" if prefix == "key" else None,
            text="autosave" if prefix == "type" else None,
        )
        if prefix == "click":
            step.anchor = workflow.steps[-1].anchor.model_copy(deep=True)
        workflow.steps.insert(0, step)
    workflow, bundle = _sealed(tmp_path, workflow, encrypted=False)
    backend = FakeBackend()

    class NoReadback:
        def capture_pre_state(self, *args):
            pytest.fail("dry-run must stop before verifier preparation")

    report = Replayer(
        backend,
        vision=_ReadyVision(),
        approval_gate=DryRunGate(),
        effect_verifier=NoReadback(),
        require_settled=True,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run")
    assert backend.actions == []
    assert report.execution_outcome == "HALTED"
    assert report.transaction_outcome == "HALTED_BEFORE_EFFECT"
    assert all(result.delivery_attempted is False for result in report.results)
    assert report.approval.decision_reason == (
        "dry_run" if prefix in (None, "wait") else "dry_run_refused"
    )


def test_dry_mode_never_constructs_or_prepares_api_actuator(tmp_path, monkeypatch):
    from tests.test_api_tier_web_form import _span_workflow

    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)

    class ForbiddenActuator:
        def prepare(self, *args):
            pytest.fail("request preparation can execute scripts")

        def actuate(self, *args):
            pytest.fail("no request may be sent")

    workflow, bundle = _sealed(tmp_path, _span_workflow(), encrypted=False)
    backend = FakeBackend()
    engine = Replayer(
        backend,
        vision=_ReadyVision(),
        approval_gate=DryRunGate(),
        web_form_actuator=ForbiddenActuator(),
        api_actuator=ForbiddenActuator(),
    )
    assert engine.api_steps is False
    assert engine._actuator_for(workflow.steps[-1].api_binding) is None
    report = engine.run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run")
    assert backend.actions == []
    assert report.execution_outcome == "HALTED"


def test_cli_refuses_navigation_before_building_a_backend(tmp_path, monkeypatch):
    import openadapt_flow.__main__ as main

    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    workflow, bundle = _sealed(
        tmp_path, _key_workflow("dry", with_effect=True), encrypted=False
    )
    args = main.build_parser().parse_args(
        [
            "replay",
            str(bundle),
            "--stop-before-effect",
            "--url",
            "http://127.0.0.1:1/write-on-navigation",
        ]
    )
    import openadapt_flow.backends.factory as factory

    monkeypatch.setattr(
        factory,
        "build_backend",
        lambda *a, **k: pytest.fail("navigation was not refused before startup"),
    )
    assert args.func(args) == 2
    assert (
        main._approval_gate_for_run(
            SimpleNamespace(stop_before_effect=True), tmp_path
        ).__class__
        is DryRunGate
    )


def test_cli_refuses_network_session_start_before_backend_construction(
    tmp_path, monkeypatch
):
    import openadapt_flow.__main__ as main
    import openadapt_flow.backends.factory as factory

    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    _workflow, bundle = _sealed(
        tmp_path, _key_workflow("dry", with_effect=True), encrypted=False
    )
    args = main.build_parser().parse_args(
        [
            "replay",
            str(bundle),
            "--stop-before-effect",
            "--backend",
            "rdp",
            "--rdp-host",
            "127.0.0.1",
        ]
    )
    monkeypatch.setattr(
        factory,
        "build_backend",
        lambda *a, **k: pytest.fail(
            "dry-run must not create a network desktop session"
        ),
    )
    assert args.func(args) == 2


def test_dry_run_uses_existing_sealed_run_report_without_value_sidecar(
    tmp_path, monkeypatch
):
    import json

    from openadapt_flow import sealed_set

    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    from openadapt_flow.bundle_sealing import seal_bundle
    from openadapt_flow.ir import Workflow

    _workflow, plain = _sealed(
        tmp_path, _key_workflow("dry", with_effect=True), encrypted=False
    )
    bundle = tmp_path / "sealed-bundle"
    seal_bundle(plain, bundle)
    workflow = Workflow.load(bundle)
    run = tmp_path / "run"
    from tests.test_sealed_at_rest import watching_disk

    with watching_disk() as writes:
        report = Replayer(
            FakeBackend(),
            vision=_ReadyVision(),
            approval_gate=DryRunGate(),
            require_settled=True,
        ).run(workflow, bundle_dir=bundle, run_dir=run)
    assert (
        writes.leaks(
            [b"Synthetic record", b"stopped_before_delivery", b"\x89PNG\r\n\x1a\n"]
        )
        == []
    )
    assert report.approval.decision_reason == "dry_run"
    assert sealed_set.is_sealed(run)
    sealed_set.SealedSet.open(run, _KEY).verify_all()
    raw = json.loads(sealed_set.read_bytes(run / "report.json"))
    assert raw["approval"]["decision_reason"] == "dry_run"
    assert all(
        set(check) <= {"code", "passed", "detail", "step_id"}
        for check in raw["approval"]["pre_checks"]
    )
    assert not (run / "report.json").exists()
    assert not list(run.rglob("*.png"))


@pytest.mark.parametrize(
    "kind",
    ["ordinary", "subclass", "mode", "approval_store", "log", "approver", "record"],
)
def test_sealed_dry_run_exception_never_admits_other_gate_state(
    tmp_path, monkeypatch, kind
):
    from openadapt_flow import sealed_set
    from openadapt_flow.approval_mode.gate import ApprovalGate
    from openadapt_flow.approval_mode.model import ApprovalMode
    from openadapt_flow.bundle_sealing import seal_bundle
    from openadapt_flow.ir import Workflow

    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    _workflow, plain = _sealed(
        tmp_path, _key_workflow("dry", with_effect=True), encrypted=False
    )
    bundle = tmp_path / "sealed-bundle"
    seal_bundle(plain, bundle)
    workflow = Workflow.load(bundle)

    class UntrustedSubclass(DryRunGate):
        def before_step(self, ctx):
            pytest.fail("a subclass must be refused before any gate callback")

    gate = (
        ApprovalGate("not_gated", pinned_approver_keys={})
        if kind == "ordinary"
        else (UntrustedSubclass() if kind == "subclass" else DryRunGate())
    )
    if kind == "mode":
        gate.mode = ApprovalMode.EACH_ITEM
    elif kind not in {"ordinary", "subclass"}:
        setattr(gate, kind, object())
    backend = FakeBackend()
    with pytest.raises(sealed_set.SealedSetError, match="approval gate"):
        Replayer(backend, vision=_ReadyVision(), approval_gate=gate).run(
            workflow, bundle_dir=bundle, run_dir=tmp_path / "run"
        )
    assert backend.actions == []
    assert not (tmp_path / "run").exists()


def test_cli_dry_run_never_constructs_effect_or_api_plugins(monkeypatch):
    from types import SimpleNamespace

    import openadapt_flow.__main__ as main
    import openadapt_flow.deployment as deployment

    def forbidden(*args, **kwargs):
        pytest.fail("dry-run constructed an effect or API plugin")

    monkeypatch.setattr(deployment, "build_effect_verifier", forbidden)
    monkeypatch.setattr(deployment, "build_api_actuator", forbidden)
    _cfg, verifier, actuator, _durable, _egress = main._deployment_runtime(
        SimpleNamespace(stop_before_effect=True)
    )
    assert verifier is None
    assert actuator is None
