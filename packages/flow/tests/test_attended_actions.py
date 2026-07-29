"""Adversarial contracts for the target-state attended action path."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from openadapt_flow.console.app import create_app
from openadapt_flow.console.attention import attention_item
from openadapt_flow.console.human_decisions import (
    RemoteAttendedActionRequest,
    RemoteDecisionPrincipal,
    decision_receipt,
    execute_remote_attended_action,
    portable_remote_decision_task,
)
from openadapt_flow.deployment import DeploymentConfig
from openadapt_flow.execution_profiles import _program_action_trace
from openadapt_flow.ir import (
    ActionDeliveryUncertainty,
    ActionKind,
    Anchor,
    ApiBinding,
    Guard,
    HaltObservation,
    LoopSpec,
    Postcondition,
    PostconditionKind,
    Predicate,
    PredicateKind,
    ProgramGraph,
    Relation,
    RunReport,
    State,
    StateKind,
    Step,
    StepResult,
    Transition,
    Workflow,
)
from openadapt_flow.privacy import reset_scrubbers, set_image_scrubber
from openadapt_flow.qualification import (
    ActionRiskClassification,
    EnvironmentBoundary,
    init_project,
    set_action_classification,
    workflow_contract_sha256,
)
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.durable.approval import (
    ApprovalRecord,
    ApprovalRequired,
    approval_pause_digest,
    enforce_resume_authorization,
)
from openadapt_flow.runtime.durable.attended import (
    AttendedActionRefused,
    AttendedActionRequest,
    AttendedActionStore,
    AttendedDecision,
    AttendedExecutionResult,
    BoundAttendedExecutor,
    TransitionObservation,
    _digest,
    attended_decision_payload,
    execute_attended_action,
    issue_attended_capability,
    validate_attended_program_receipt,
)
from openadapt_flow.runtime.durable.attended_service import AttendedActionService
from openadapt_flow.runtime.durable.authority import (
    DurableAuthority,
    DurableAuthorityBusy,
)
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    PendingEscalation,
    RunManifest,
)
from openadapt_flow.runtime.durable.continuation import ContinuationLeaseRecord
from openadapt_flow.runtime.durable.program_checkpoint import ProgramCheckpoint
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    Verdict,
)
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.verification import VerificationTier
from tests.test_replayer import (
    FakeBackend,
    FakeVision,
    Match,
    RemoteLeaseBackend,
    click_step,
    make_png,
)


def _digest_fixture_decision(
    *,
    schema_version: int = 2,
    decided_by: str = "unknown",
) -> AttendedDecision:
    return AttendedDecision(
        schema_version=schema_version,
        decision_id="0123456789abcdef0123456789abcdef",
        pause_id="fedcba9876543210fedcba9876543210",
        capability_digest="sha256:" + "a" * 64,
        request_digest="sha256:" + "b" * 64,
        idempotency_key="digest-fixture-key-0001",
        action="continue",
        operator="José",
        decided_by=decided_by,
        status="completed",
        message="vérifié",
        created_at="2026-07-01T00:00:00+00:00",
        report_success=True,
        transition_receipt_digest="sha256:" + "c" * 64,
    )


def test_v1_unicode_decision_keeps_exact_engine_and_portable_payloads():
    current = _digest_fixture_decision(decided_by="unknown")
    legacy_wire = current.model_dump(mode="json")
    legacy_wire["schema_version"] = 1
    legacy_wire.pop("decided_by")
    legacy = AttendedDecision.model_validate(legacy_wire)

    assert legacy.decided_by == "unknown"
    assert attended_decision_payload(legacy) == legacy_wire
    portable = json.dumps(
        legacy_wire, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    assert decision_receipt(legacy).decision_digest == (
        "sha256:" + hashlib.sha256(portable).hexdigest()
    )
    engine = json.dumps(
        legacy_wire, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert engine != portable


def test_v2_decision_digest_commits_to_trusted_provenance():
    human = _digest_fixture_decision(decided_by="human")
    automated = _digest_fixture_decision(decided_by="automation")

    assert human.schema_version == automated.schema_version == 2
    assert attended_decision_payload(human)["decided_by"] == "human"
    assert attended_decision_payload(automated)["decided_by"] == "automation"
    assert (
        decision_receipt(human).decision_digest
        != decision_receipt(automated).decision_digest
    )


def test_v1_cannot_claim_provenance_and_untrusted_request_cannot_supply_it():
    with pytest.raises(ValidationError, match="schema v1"):
        _digest_fixture_decision(schema_version=1, decided_by="human")
    with pytest.raises(ValidationError):
        AttendedActionRequest.model_validate(
            {
                "capability_digest": "sha256:" + "0" * 64,
                "idempotency_key": "forged-provenance-0001",
                "action": "escalate",
                "disposition": "needs_assistance",
                "decided_by": "human",
            }
        )


def _step(step_id: str, key: str, *, expect: str | None = None) -> Step:
    return Step(
        id=step_id,
        intent=f"press {key}",
        action=ActionKind.KEY,
        key=key,
        expect=(
            [
                Postcondition(
                    kind=PostconditionKind.TEXT_PRESENT,
                    text=expect,
                    timeout_s=0.01,
                )
            ]
            if expect
            else []
        ),
    )


_AUTHORITY_DIGESTS: dict[str, str] = {}


def _write_v2_manifest(store: CheckpointStore, manifest: RunManifest) -> None:
    committed = manifest.model_copy(
        update={
            "namespace_id": f"namespace-{manifest.run_id}",
            "canonical_run_dir": str(store.run_dir.resolve()),
        }
    )
    store.write_fresh_manifest(committed)
    _AUTHORITY_DIGESTS[str(store.run_dir.resolve())] = (
        DurableAuthority(store.run_dir, store).validate(committed).progress_digest
    )


def _sync_v2_authority(store: CheckpointStore) -> None:
    manifest = store.read_manifest()
    assert manifest is not None
    pending = store.read_pending()
    key = str(store.run_dir.resolve())
    _AUTHORITY_DIGESTS[key] = DurableAuthority(store.run_dir, store).advance(
        manifest,
        expected_progress_digest=_AUTHORITY_DIGESTS[key],
        phase="paused" if pending is not None else "active",
        pause_binding_sha256=(
            approval_pause_digest(pending) if pending is not None else ""
        ),
    )


def _paused(
    tmp_path: Path,
    *,
    workflow: Workflow | None = None,
    result: StepResult | None = None,
    transition_observation: TransitionObservation | None = None,
):
    workflow = workflow or Workflow(
        name="attended",
        steps=[_step("human", "A", expect="DONE"), _step("next", "B")],
    )
    bundle = tmp_path / "bundle"
    run = tmp_path / "run"
    workflow.save(bundle)
    store = CheckpointStore(run)
    _write_v2_manifest(
        store,
        RunManifest(
            run_id="run-instance-a",
            workflow_name=workflow.name,
            bundle_dir=str(bundle),
            params={},
        ),
    )
    pending = PendingEscalation(
        run_id="run-instance-a",
        workflow_name=workflow.name,
        step_index=0,
        step_id=workflow.steps[0].id,
        intent=workflow.steps[0].intent,
        category="human_required",
        reason="Please verify you are human",
        proposed_options=["complete in live app", "resume"],
        resume_from_index=0,
    )
    store.write_pending(pending)
    RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-18T12:00:00+00:00",
        success=False,
        results=[
            result
            or StepResult(
                step_id=workflow.steps[0].id,
                intent=workflow.steps[0].intent,
                ok=False,
                error="Please verify you are human",
            )
        ],
        halt=HaltObservation(
            state_id=workflow.steps[0].id,
            intent=workflow.steps[0].intent,
            reason="Please verify you are human",
        ),
    ).save(run)
    capability = issue_attended_capability(
        run,
        store=store,
        pending=pending,
        workflow=workflow,
        result=result
        or StepResult(
            step_id=workflow.steps[0].id,
            intent=workflow.steps[0].intent,
            ok=False,
            error="Please verify you are human",
        ),
        transition_observation=transition_observation,
    )
    _sync_v2_authority(store)
    return workflow, bundle, run, store, capability


def _request(capability, action="continue", key="request-key-0001"):
    return AttendedActionRequest(
        capability_digest=capability.digest,
        idempotency_key=key,
        action=action,
        disposition=(
            "completed_by_operator" if action == "continue" else "not_applicable"
        ),
    )


def _remote_deployment() -> DeploymentConfig:
    return DeploymentConfig.model_validate(
        {
            "human_decisions": {
                "remote": {
                    "enabled": True,
                    "tenant_id": "tenant_exact_01",
                    "runner_id": "runner_exact_01",
                }
            }
        }
    )


def _remote_request(projection, capability, *, key="remote-request-key-01"):
    return RemoteAttendedActionRequest(
        capability_digest=capability.digest,
        idempotency_key=key,
        action="continue",
        disposition="completed_by_operator",
        task_digest=projection.task_digest,
        task_signature=projection.task.signature,
        tenant_id="tenant_exact_01",
        runner_id="runner_exact_01",
        phase=projection.phase,
        event_sequence=projection.event_sequence,
        idempotency_scope_digest=projection.idempotency_scope_digest,
        binding_digest=projection.binding_digest,
    )


def _remote_principal() -> RemoteDecisionPrincipal:
    return RemoteDecisionPrincipal(
        subject="operator_subject_01",
        tenant_id="tenant_exact_01",
        runner_id="runner_exact_01",
        assurance="aal2",
    )


class _ResultExecutor:
    def __init__(self):
        self.calls = 0

        def replayer_for(manifest):
            workflow = Workflow.load(manifest.bundle_dir)
            steps = list(workflow.steps)
            if workflow.program is not None:
                steps.extend(
                    state.step
                    for state in workflow.program.states.values()
                    if state.step is not None
                )
            for subflow in workflow.subflows.values():
                steps.extend(
                    state.step
                    for state in subflow.states.values()
                    if state.step is not None
                )
            vision = FakeVision()
            vision.text_results = {
                postcondition.text: Match(
                    point=(10, 10),
                    region=(0, 0, 20, 20),
                    confidence=1.0,
                )
                for step in steps
                for postcondition in step.expect
                if postcondition.text
            }
            return Replayer(FakeBackend(), vision=vision, poll_interval_s=0.0)

        self.bound = BoundAttendedExecutor(replayer_for)

    def continue_run(self, run_dir, capability, approval):
        self.calls += 1
        return self.bound.continue_run(run_dir, capability, approval)

    def skip_run(self, run_dir, capability, approval):
        self.calls += 1
        return self.bound.skip_run(run_dir, capability, approval)


def test_capability_binds_run_bundle_pause_and_transition(tmp_path):
    _workflow, _bundle, _run, store, capability = _paused(tmp_path)
    assert capability.run_id == "run-instance-a"
    assert capability.step_id == "human"
    assert capability.expected_next_transition == "next"
    assert capability.bundle_version.startswith("sha256:")
    assert capability.expected_transition_digest.startswith("sha256:")
    assert AttendedActionStore(store.run_dir).read() == capability

    pending = store.read_pending()
    assert pending is not None
    store.write_pending(pending.model_copy(update={"step_id": "other"}))
    with pytest.raises(AttendedActionRefused, match="pause changed"):
        execute_attended_action(
            store.run_dir,
            _request(capability),
            operator="staff",
            executor=_ResultExecutor(),
        )


def test_capability_derives_only_semantically_supported_actions(tmp_path):
    _workflow, _bundle, _run, _store, verified = _paused(tmp_path / "verified")
    assert verified.allowed_actions == ("continue", "reject", "teach", "escalate")

    unverified_workflow = Workflow(
        name="unverified",
        steps=[_step("human", "A")],
    )
    _workflow, _bundle, _run, _store, unverified = _paused(
        tmp_path / "unverified", workflow=unverified_workflow
    )
    assert unverified.allowed_actions == ("reject", "teach", "escalate")

    optional_workflow = Workflow(
        name="optional",
        steps=[
            Step(
                id="human",
                intent="optional dismissal",
                action=ActionKind.KEY,
                key="A",
                guard=Guard(
                    predicate=Predicate(
                        kind=PredicateKind.TEXT_PRESENT, text="OPTIONAL"
                    ),
                    on_unmet="skip",
                ),
            )
        ],
    )
    _workflow, _bundle, _run, _store, optional = _paused(
        tmp_path / "optional", workflow=optional_workflow
    )
    assert optional.allowed_actions == ("skip", "reject", "teach", "escalate")

    absolute_effect_workflow = Workflow(
        name="absolute-effect",
        steps=[
            Step(
                id="human",
                intent="save",
                action=ActionKind.KEY,
                key="A",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"id": "row-1"},
                        forbid_collateral_loss=False,
                    )
                ],
            )
        ],
    )
    _workflow, _bundle, _run, _store, absolute = _paused(
        tmp_path / "absolute", workflow=absolute_effect_workflow
    )
    assert absolute.allowed_actions == ("continue", "reject", "teach", "escalate")

    delta_effect_workflow = absolute_effect_workflow.model_copy(deep=True)
    delta_effect_workflow.name = "delta-effect"
    delta_effect_workflow.steps[0].effects[0].count_new_only = True
    _workflow, _bundle, _run, _store, delta = _paused(
        tmp_path / "delta", workflow=delta_effect_workflow
    )
    assert delta.allowed_actions == ("reject", "teach", "escalate")


def test_transition_baseline_is_keyed_signed_and_contains_no_raw_phi(tmp_path):
    raw_url = "https://payer.example/eligibility?patient=Jane-Roe&member=ABC123"
    raw_title = "Jane Roe — Eligibility ABC123"
    workflow = Workflow(
        name="relative",
        steps=[
            Step(
                id="human",
                intent="complete login",
                action=ActionKind.KEY,
                key="A",
                expect=[Postcondition(kind=PostconditionKind.URL_CHANGED)],
            )
        ],
    )
    _workflow, _bundle, run, _store, capability = _paused(
        tmp_path,
        workflow=workflow,
        transition_observation=TransitionObservation(
            url=raw_url,
            page_title=raw_title,
            page_count=1,
        ),
    )
    serialized = (run / "attended_capability.json").read_text()
    assert raw_url not in serialized
    assert raw_title not in serialized
    assert "Jane Roe" not in serialized
    assert capability.transition_baseline.url_digest.startswith("hmac-sha256:")
    assert capability.transition_baseline.title_digest.startswith("hmac-sha256:")
    assert capability.transition_baseline.page_count == 1
    assert capability.allowed_actions == ("continue", "reject", "teach", "escalate")
    store = AttendedActionStore(run)
    assert store.transition_value_digest("url", raw_url) == (
        capability.transition_baseline.url_digest
    )
    assert store.read() == capability


@pytest.mark.parametrize(
    ("kind", "baseline", "attribute", "changed", "unchanged"),
    [
        (
            PostconditionKind.URL_CHANGED,
            TransitionObservation(url="https://payer.example/login"),
            "url",
            "https://payer.example/home",
            "https://payer.example/login",
        ),
        (
            PostconditionKind.TITLE_CHANGED,
            TransitionObservation(page_title="Sign in"),
            "page_title",
            "Eligibility",
            "Sign in",
        ),
        (
            PostconditionKind.NEW_TAB_OPENED,
            TransitionObservation(page_count=1),
            "page_count",
            2,
            1,
        ),
    ],
)
def test_signed_relative_transition_confirms_common_human_redirects(
    tmp_path, kind, baseline, attribute, changed, unchanged
):
    workflow = Workflow(
        name=f"relative-{kind.value}",
        steps=[
            Step(
                id="human",
                intent="complete human challenge",
                action=ActionKind.KEY,
                key="A",
                expect=[Postcondition(kind=kind)],
            )
        ],
    )
    _workflow, _bundle, run, _store, capability = _paused(
        tmp_path / "changed",
        workflow=workflow,
        transition_observation=baseline,
    )
    backend = FakeBackend()
    setattr(backend, attribute, changed)
    accepted = execute_attended_action(
        run,
        _request(capability, key=f"relative-{kind.value}-changed"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                backend, vision=FakeVision(), poll_interval_s=0.0
            )
        ),
    )
    assert accepted.status == "completed"
    assert not backend.actions

    _workflow, _bundle, run, store, capability = _paused(
        tmp_path / "unchanged",
        workflow=workflow,
        transition_observation=baseline,
    )
    backend = FakeBackend()
    setattr(backend, attribute, unchanged)
    refused = execute_attended_action(
        run,
        _request(capability, key=f"relative-{kind.value}-unchanged"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                backend, vision=FakeVision(), poll_interval_s=0.0
            )
        ),
    )
    assert refused.status == "refused"
    assert "unchanged" in refused.message
    assert store.read_pending() is not None
    assert not backend.actions


def test_relative_continue_is_not_advertised_without_signed_baseline(tmp_path):
    workflow = Workflow(
        name="relative-no-baseline",
        steps=[
            Step(
                id="human",
                intent="complete login",
                action=ActionKind.KEY,
                key="A",
                expect=[Postcondition(kind=PostconditionKind.URL_CHANGED)],
            )
        ],
    )
    _workflow, _bundle, run, _store, capability = _paused(tmp_path, workflow=workflow)
    assert capability.allowed_actions == ("reject", "teach", "escalate")
    with pytest.raises(AttendedActionRefused, match="does not allow"):
        execute_attended_action(
            run,
            _request(capability, key="relative-missing-baseline"),
            operator="staff",
            executor=_ResultExecutor(),
        )


def test_durable_halt_automatically_captures_protected_transition_baseline(tmp_path):
    raw_url = "https://payer.example/member/Jane-Roe-ABC123"
    workflow = Workflow(
        name="auto-baseline",
        steps=[
            Step(
                id="human",
                intent="complete challenge",
                action=ActionKind.KEY,
                key="A",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.URL_CHANGED,
                        timeout_s=0.01,
                    )
                ],
            )
        ],
    )
    bundle = tmp_path / "bundle"
    run = tmp_path / "run"
    workflow.save(bundle)
    backend = FakeBackend()
    backend.url = raw_url
    report = Replayer(
        backend,
        vision=FakeVision(),
        durable=True,
        poll_interval_s=0.0,
    ).run(workflow, bundle_dir=bundle, run_dir=run)
    assert report.success is False
    capability = AttendedActionStore(run).read()
    assert capability.transition_baseline.url_digest is not None
    assert "continue" in capability.allowed_actions
    assert raw_url not in (run / "attended_capability.json").read_text()


def test_program_pause_never_advertises_generic_continue_or_skip(tmp_path):
    workflow, _bundle, run, store, _first = _paused(tmp_path)
    pending = store.read_pending()
    assert pending is not None
    program_pending = pending.model_copy(
        update={
            "program": True,
            "state_id": "challenge-state",
            "created_at": "2026-07-18T13:30:00+00:00",
        }
    )
    store.write_pending(program_pending)
    capability = issue_attended_capability(
        run,
        store=store,
        pending=program_pending,
        workflow=workflow,
        result=StepResult(
            step_id="challenge-state",
            intent="complete challenge",
            ok=False,
            error="MFA required",
        ),
        transition_observation=TransitionObservation(url="https://payer.example/mfa"),
    )
    assert capability.allowed_actions == ("reject", "teach", "escalate")


def _attended_program(*, guarded_transition: bool = False, skippable: bool = False):
    transitions = (
        [
            Transition(
                guard=Predicate(
                    kind=PredicateKind.TEXT_PRESENT,
                    text="ROUTE_A",
                ),
                target="route-a",
            ),
            Transition(target="route-b"),
        ]
        if guarded_transition
        else [Transition(target="next")]
    )
    human = Step(
        id="human-step",
        intent="complete challenge",
        action=ActionKind.KEY,
        key="A",
        expect=[
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT,
                text="DONE",
                timeout_s=0.01,
            )
        ],
        guard=(
            Guard(
                predicate=Predicate(
                    kind=PredicateKind.TEXT_PRESENT,
                    text="OPTIONAL",
                ),
                on_unmet="skip",
            )
            if skippable
            else None
        ),
    )
    states = {
        "human": State(
            id="human",
            kind=StateKind.ACTION,
            step=human,
            transitions=transitions,
        ),
        "next": State(
            id="next",
            kind=StateKind.ACTION,
            step=_step("next-step", "B"),
            transitions=[Transition(target="done")],
        ),
        "route-a": State(
            id="route-a",
            kind=StateKind.ACTION,
            step=_step("route-a-step", "X"),
            transitions=[Transition(target="done")],
        ),
        "route-b": State(
            id="route-b",
            kind=StateKind.ACTION,
            step=_step("route-b-step", "Y"),
            transitions=[Transition(target="done")],
        ),
        "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
    }
    return Workflow(
        name="attended-program",
        program=ProgramGraph(entry="human", states=states),
    )


def _run_attended_program_to_pause(tmp_path, workflow, *, optional_visible=False):
    bundle = tmp_path / "bundle"
    run = tmp_path / "run"
    workflow.save(bundle)
    vision = FakeVision()
    if optional_visible:
        vision.text_results["OPTIONAL"] = Match(
            point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
        )
    initial_backend = FakeBackend()
    report = Replayer(
        initial_backend,
        vision=vision,
        durable=True,
        poll_interval_s=0.0,
    ).run(workflow, bundle_dir=bundle, run_dir=run)
    assert report.success is False
    return (
        bundle,
        run,
        initial_backend,
        CheckpointStore(run),
        AttendedActionStore(run).read(),
    )


def _attended_effect_program() -> Workflow:
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"id": "row-1"},
        forbid_collateral_loss=False,
    )
    return Workflow(
        name="attended-effect-program",
        program=ProgramGraph(
            entry="human",
            states={
                "human": State(
                    id="human",
                    kind=StateKind.ACTION,
                    step=Step(
                        id="human-step",
                        intent="save record",
                        action=ActionKind.KEY,
                        key="A",
                        effects=[effect],
                    ),
                    transitions=[Transition(target="done")],
                ),
                "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
            },
        ),
    )


def test_linear_attended_verification_uses_admitted_postconditions(tmp_path):
    workflow = Workflow(
        name="linear-attended-snapshot",
        steps=[
            Step(
                id="human-step",
                intent="save record",
                action=ActionKind.KEY,
                key="A",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="ORIGINAL_REQUIRED_RESULT",
                        timeout_s=0.01,
                    )
                ],
            )
        ],
    )

    class MutatingVision(FakeVision):
        def wait_settled(self, backend, **kwargs):
            workflow.steps[0].expect[0].text = "EASY_REPLACEMENT"
            return super().wait_settled(backend, **kwargs)

    vision = MutatingVision()
    vision.text_results["EASY_REPLACEMENT"] = Match((1, 1), (0, 0, 2, 2))
    result = Replayer(FakeBackend(), vision=vision).revalidate_attended_completion(
        workflow,
        step_index=0,
        params={},
        bundle_dir=tmp_path,
        run_dir=tmp_path / "run",
        run_id="run-linear",
        transition_baseline=TransitionObservation(),
        transition_digest=lambda field, value: f"{field}:{value}",
    )

    assert result.ok is False
    assert result.postconditions_ok is False


@pytest.mark.parametrize("mutate_authorization", [False, True])
def test_program_attended_verification_keeps_the_admitted_effect_tier(
    tmp_path, mutate_authorization
):
    workflow = _attended_effect_program()
    authorization = GovernedRunAuthorization(
        bundle_content_digest="a" * 64,
        runtime_inputs_digest="b" * 64,
        admitted_policy_name="test",
        execution_profile="standard",
        minimum_effect_tier=int(VerificationTier.INDEPENDENT_SYSTEM),
    )

    class CurrentRecords:
        substrate = "test"
        verification_tier = (
            VerificationTier.INDEPENDENT_SYSTEM
            if mutate_authorization
            else VerificationTier.IMMEDIATE_SCREEN
        )

        def capture_pre_state(self, context=None):
            if mutate_authorization:
                object.__setattr__(
                    authorization,
                    "minimum_effect_tier",
                    int(VerificationTier.IMMEDIATE_SCREEN),
                )
            return EffectState(
                substrate=self.substrate,
                reachable=True,
                records=[{"id": "row-1"}],
            )

    result, target = Replayer(
        FakeBackend(),
        vision=FakeVision(),
        effect_verifier=CurrentRecords(),
        governed_authorization=authorization,
    ).revalidate_attended_program_completion(
        workflow,
        graph_id="__program__",
        state_id="human",
        params={},
        bundle_dir=tmp_path,
        run_dir=tmp_path / "run",
        run_id="run-program",
        transition_baseline=TransitionObservation(),
        transition_digest=lambda field, value: f"{field}:{value}",
    )

    assert result.ok is False
    assert result.safety_halt is True
    assert target is None


def test_attended_final_persistence_callback_cannot_verify_changed_semantics(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "auto")
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB_IMAGES", "1")
    workflow = Workflow(
        name="attended-final-callback",
        steps=[
            Step(
                id="human",
                intent="save",
                action=ActionKind.KEY,
                key="A",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="ORIGINAL_RESULT",
                        timeout_s=0.01,
                    )
                ],
            )
        ],
    )
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    assert workflow.manifest is not None
    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(workflow, {}, None),
        admitted_policy_name="test",
    )

    class MutatingImageScrubber:
        calls = 0

        def scrub_image(self, image, fill_color=None):
            self.calls += 1
            if self.calls == 2:
                workflow.steps[0].expect[0].text = "REPLACED_AFTER_CHECK"
                object.__setattr__(authorization, "runtime_inputs_digest", "f" * 64)
            return image

    set_image_scrubber(MutatingImageScrubber())
    try:
        vision = FakeVision()
        vision.text_results["ORIGINAL_RESULT"] = Match(
            point=(1, 1), region=(0, 0, 2, 2), confidence=1.0
        )
        result = Replayer(
            FakeBackend(), vision=vision, governed_authorization=authorization
        ).revalidate_attended_completion(
            workflow,
            step_index=0,
            params={},
            bundle_dir=bundle,
            run_dir=tmp_path / "run",
            run_id="run-final-callback",
            transition_baseline=TransitionObservation(),
            transition_digest=lambda field, value: f"{field}:{value}",
        )
    finally:
        reset_scrubbers()

    assert result.ok is False
    assert result.safety_halt is True


def test_program_continue_commits_exact_receipt_without_reactuating_source(tmp_path):
    workflow = _attended_program()
    _bundle, run, initial_backend, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    assert initial_backend.actions == [("press", "A")]
    assert capability.allowed_actions == ("continue", "reject", "teach", "escalate")
    pending = store.read_pending()
    assert pending is not None
    assert [frame.state_id for frame in pending.program_frames] == ["human"]
    assert capability.program_cursor_digest is not None

    backend = FakeBackend()
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )
    decision = execute_attended_action(
        run,
        _request(capability, key="program-continue-request"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(backend, vision=vision, poll_interval_s=0.0)
        ),
    )
    assert decision.status == "completed"
    assert decision.next_transition == "next"
    assert decision.transition_receipt_digest is not None
    assert backend.actions == [("press", "B")]
    checkpoint = store.program_checkpoints()[0]
    assert checkpoint is not None
    assert checkpoint.verified_state_id == "human"
    assert checkpoint.attended_transition is not None
    assert checkpoint.attended_transition.source_state_id == "human"
    assert checkpoint.attended_transition.target_state_id == "next"
    assert checkpoint.attended_transition.action == "continue"
    assert checkpoint.attended_transition.run_id == capability.run_id
    assert checkpoint.attended_transition.workflow_name == workflow.name
    assert checkpoint.attended_transition.bundle_version == capability.bundle_version
    assert checkpoint.attended_transition.pause_id == capability.pause_id
    assert checkpoint.attended_transition.pause_digest == capability.pause_digest
    assert checkpoint.attended_transition.signature.startswith("hmac-sha256:")
    receipt_path = (
        run
        / ".attended_program_receipts"
        / f"{checkpoint.attended_transition.pause_id}.json"
    )
    receipt_bytes = receipt_path.read_bytes()
    assert json.loads(receipt_bytes) == checkpoint.attended_transition.model_dump(
        mode="json"
    )
    assert AttendedActionStore(run).read_program_receipt(capability.pause_id) == (
        checkpoint.attended_transition
    )
    assert not any(
        sensitive in receipt_bytes.lower()
        for sensitive in (b"url", b"title", b"observed_text", b"done")
    )
    if os.name != "nt":
        assert receipt_path.parent.stat().st_mode & 0o077 == 0
        assert receipt_path.stat().st_mode & 0o077 == 0
    resumed_report = RunReport.model_validate_json((run / "report.json").read_bytes())
    assert len(resumed_report.attended_program_transition_evidence) == 1
    transition_evidence = resumed_report.attended_program_transition_evidence[0]
    assert transition_evidence.state_id == "human"
    assert transition_evidence.target_state_id == "next"
    assert transition_evidence.action == "continue"
    assert transition_evidence.decision_index == 0
    assert [
        item.decision_index for item in resumed_report.program_transition_evidence
    ] == [1]
    assert (
        _program_action_trace(
            workflow,
            resumed_report.visited_states,
            runtime_params=resumed_report.params,
            transition_evidence=resumed_report.program_transition_evidence,
            attended_transition_evidence=(
                resumed_report.attended_program_transition_evidence
            ),
            transition_evidence_root=run,
            governed_runtime_inputs_digest=(
                resumed_report.governed_runtime_inputs_digest
            ),
            run_id_sha256=resumed_report.run_id_sha256,
            workflow_contract_digest=workflow_contract_sha256(workflow),
            reported_results=resumed_report.results,
        )
        is not None
    )
    assert (
        _program_action_trace(
            workflow,
            resumed_report.visited_states,
            runtime_params=resumed_report.params,
            transition_evidence=resumed_report.program_transition_evidence,
            attended_transition_evidence=(
                resumed_report.attended_program_transition_evidence
            ),
            transition_evidence_root=run,
            governed_runtime_inputs_digest=(
                resumed_report.governed_runtime_inputs_digest
            ),
            run_id_sha256="0" * 64,
            workflow_contract_digest=workflow_contract_sha256(workflow),
            reported_results=resumed_report.results,
        )
        is None
    )
    rebound_inputs = resumed_report.model_copy(deep=True)
    rebound_inputs.governed_runtime_inputs_digest = "b" * 64
    rebound_inputs.program_transition_evidence = [
        item.model_copy(update={"governed_runtime_inputs_digest": "b" * 64})
        for item in rebound_inputs.program_transition_evidence
    ]
    rebound_inputs.attended_program_transition_evidence = [
        item.model_copy(update={"governed_runtime_inputs_digest": "b" * 64})
        for item in rebound_inputs.attended_program_transition_evidence
    ]
    assert (
        _program_action_trace(
            workflow,
            rebound_inputs.visited_states,
            runtime_params=rebound_inputs.params,
            transition_evidence=rebound_inputs.program_transition_evidence,
            attended_transition_evidence=(
                rebound_inputs.attended_program_transition_evidence
            ),
            transition_evidence_root=run,
            governed_runtime_inputs_digest=(
                rebound_inputs.governed_runtime_inputs_digest
            ),
            run_id_sha256=rebound_inputs.run_id_sha256,
            workflow_contract_digest=workflow_contract_sha256(workflow),
            reported_results=rebound_inputs.results,
        )
        is None
    )
    tampered = resumed_report.model_copy(deep=True)
    tampered.attended_program_transition_evidence[0] = (
        tampered.attended_program_transition_evidence[0].model_copy(
            update={"receipt_sha256": "0" * 64}
        )
    )
    assert (
        _program_action_trace(
            workflow,
            tampered.visited_states,
            runtime_params=tampered.params,
            transition_evidence=tampered.program_transition_evidence,
            attended_transition_evidence=(
                tampered.attended_program_transition_evidence
            ),
            transition_evidence_root=run,
            governed_runtime_inputs_digest=tampered.governed_runtime_inputs_digest,
            run_id_sha256=tampered.run_id_sha256,
            workflow_contract_digest=workflow_contract_sha256(workflow),
            reported_results=tampered.results,
        )
        is None
    )


def test_program_continue_persists_current_readback_effect_evidence(tmp_path):
    workflow = _attended_effect_program()
    bundle = tmp_path / "bundle"
    run = tmp_path / "run"
    workflow.save(bundle)

    class InitialRefutingRecords:
        substrate = "independent-test-records"
        verification_tier = VerificationTier.INDEPENDENT_SYSTEM

        def capture_pre_state(self, context=None):
            return EffectState(substrate=self.substrate, reachable=True, records=[])

        def verify(self, effect, before):
            return EffectVerdict(
                verdict=Verdict.REFUTED,
                kind=effect.kind,
                substrate=self.substrate,
                reason="the first verification did not find the record",
            )

    initial_backend = FakeBackend()
    report = Replayer(
        initial_backend,
        vision=FakeVision(),
        effect_verifier=InitialRefutingRecords(),
        durable=True,
        poll_interval_s=0.0,
    ).run(workflow, bundle_dir=bundle, run_dir=run)
    assert report.success is False
    store = CheckpointStore(run)
    capability = AttendedActionStore(run).read()
    assert initial_backend.actions == [("press", "A")]

    class CurrentRecords:
        substrate = "independent-test-records"
        verification_tier = VerificationTier.INDEPENDENT_SYSTEM

        def capture_pre_state(self, context=None):
            return EffectState(
                substrate=self.substrate,
                reachable=True,
                records=[{"id": "row-1"}],
            )

        def verify(self, effect, before):
            return EffectVerdict(
                verdict=Verdict.CONFIRMED,
                kind=effect.kind,
                substrate=self.substrate,
                observed_effect="present",
            )

    decision = execute_attended_action(
        run,
        _request(capability, key="program-current-readback-request"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                FakeBackend(),
                vision=FakeVision(),
                effect_verifier=CurrentRecords(),
                poll_interval_s=0.0,
            )
        ),
    )

    assert decision.status == "completed"
    checkpoint = store.program_checkpoints()[0]
    assert checkpoint is not None
    assert len(checkpoint.new_effect_keys) == 1
    assert len(checkpoint.new_effects) == 1
    assert len(checkpoint.new_effect_evidence) == 1
    evidence = checkpoint.new_effect_evidence[0]
    assert evidence.effect_contract_hash == checkpoint.new_effect_keys[0]
    assert evidence.substrate == "independent-test-records"
    assert evidence.verification_tier == int(VerificationTier.INDEPENDENT_SYSTEM)


def test_program_continue_rebinds_exact_pause_before_transition_commit(tmp_path):
    workflow = _attended_program()
    _bundle, run, _initial, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    backend = FakeBackend()
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )

    def factory(_manifest):
        replayer = Replayer(backend, vision=vision, poll_interval_s=0.0)
        original = replayer.revalidate_attended_program_completion

        def replace_pause_after_live_verification(*args, **kwargs):
            result = original(*args, **kwargs)
            pending = store.read_pending()
            assert pending is not None
            store.write_pending(
                pending.model_copy(
                    update={
                        "step_id": "independently-replaced-program-pause",
                        "created_at": "2026-07-18T13:00:00+00:00",
                    }
                )
            )
            return result

        replayer.revalidate_attended_program_completion = (
            replace_pause_after_live_verification
        )
        return replayer

    with pytest.raises(AttendedActionRefused, match="durable state"):
        execute_attended_action(
            run,
            _request(capability, key="program-pause-race-request"),
            operator="staff",
            executor=BoundAttendedExecutor(factory),
        )
    assert not backend.actions
    assert store.program_checkpoints() == []
    assert store.read_approval() is None
    assert not (run / ".attended_program_receipts").exists()
    pending = store.read_pending()
    assert pending is not None
    assert pending.step_id == "independently-replaced-program-pause"


def test_program_guarded_edge_is_selected_once_and_receipted(tmp_path):
    workflow = _attended_program(guarded_transition=True)
    _bundle, run, _initial, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    backend = FakeBackend()
    vision = FakeVision()
    match = Match(point=(10, 10), region=(0, 0, 20, 20), confidence=1.0)
    vision.text_results["DONE"] = match
    # The guarded edge sees ROUTE_A once. Resume must consume the receipt
    # rather than evaluating the guard a second time (which would now fail).
    vision.text_results["ROUTE_A"] = [match, None]
    decision = execute_attended_action(
        run,
        _request(capability, key="program-guarded-request"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(backend, vision=vision, poll_interval_s=0.0)
        ),
    )
    assert decision.status == "completed"
    assert decision.next_transition == "route-a"
    assert backend.actions == [("press", "X")]
    checkpoint = store.program_checkpoints()[0]
    assert checkpoint is not None and checkpoint.attended_transition is not None
    assert checkpoint.attended_transition.target_state_id == "route-a"


def test_program_skip_uses_declared_guard_and_exact_receipt(tmp_path):
    workflow = _attended_program(skippable=True)
    _bundle, run, initial_backend, store, capability = _run_attended_program_to_pause(
        tmp_path,
        workflow,
        optional_visible=True,
    )
    assert initial_backend.actions == [("press", "A")]
    assert "skip" in capability.allowed_actions
    backend = FakeBackend()
    decision = execute_attended_action(
        run,
        _request(capability, action="skip", key="program-skip-request"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                backend, vision=FakeVision(), poll_interval_s=0.0
            )
        ),
    )
    assert decision.status == "completed"
    assert backend.actions == [("press", "B")]
    checkpoint = store.program_checkpoints()[0]
    assert checkpoint is not None and checkpoint.attended_transition is not None
    assert checkpoint.attended_transition.action == "skip"
    assert checkpoint.attended_transition.target_state_id == "next"


def test_program_cursor_tamper_refuses_before_executor(tmp_path):
    workflow = _attended_program()
    _bundle, run, _initial, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    pending = store.read_pending()
    assert pending is not None
    frames = list(pending.program_frames)
    frames[-1] = frames[-1].model_copy(update={"state_id": "next"})
    store.write_pending(pending.model_copy(update={"program_frames": frames}))
    executor = _ResultExecutor()
    with pytest.raises(AttendedActionRefused, match="pause changed"):
        execute_attended_action(
            run,
            _request(capability, key="program-cursor-tamper"),
            operator="staff",
            executor=executor,
        )
    assert executor.calls == 0


def test_program_receipt_preserves_nested_loop_cursor_and_remaining_rows(tmp_path):
    body = ProgramGraph(
        entry="human",
        states={
            "human": State(
                id="human",
                kind=StateKind.ACTION,
                step=Step(
                    id="human-step",
                    intent="complete row",
                    action=ActionKind.KEY,
                    key="A",
                    expect=[
                        Postcondition(
                            kind=PostconditionKind.TEXT_PRESENT,
                            text="DONE",
                            timeout_s=0.01,
                        )
                    ],
                ),
                transitions=[Transition(target="body-done")],
            ),
            "body-done": State(
                id="body-done",
                kind=StateKind.TERMINAL,
                outcome="success",
            ),
        },
    )
    workflow = Workflow(
        name="attended-loop",
        program=ProgramGraph(
            entry="loop",
            states={
                "loop": State(
                    id="loop",
                    kind=StateKind.LOOP,
                    loop=LoopSpec(relation="queue", body="body"),
                    transitions=[Transition(target="done")],
                ),
                "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
            },
        ),
        subflows={"body": body},
        data_sources=(
            {
                "queue": Relation(
                    name="queue",
                    rows=[{"row": "one"}, {"row": "two"}],
                )
            }
        ),
    )
    _bundle, run, initial_backend, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    assert initial_backend.actions == [("press", "A")]
    pending = store.read_pending()
    assert pending is not None
    assert [frame.graph_id for frame in pending.program_frames] == [
        "__program__",
        "body",
    ]
    assert pending.program_frames[-1].loop is not None
    assert pending.program_frames[-1].loop.row_index == 0

    backend = FakeBackend()
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )
    decision = execute_attended_action(
        run,
        _request(capability, key="program-loop-receipt"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(backend, vision=vision, poll_interval_s=0.0)
        ),
    )
    assert decision.status == "completed"
    # Row one was completed by the person. Only row two is actuated by resume.
    assert backend.actions == [("press", "A")]
    receipt_checkpoint = store.program_checkpoints()[0]
    assert receipt_checkpoint.attended_transition is not None
    assert receipt_checkpoint.frames[-1].loop is not None
    assert receipt_checkpoint.frames[-1].loop.row_index == 0
    assert receipt_checkpoint.attended_transition.target_state_id == "body-done"


def test_program_transition_refuses_a_different_checkpoint_at_reserved_sequence(
    tmp_path,
):
    workflow = _attended_program()
    _bundle, run, _initial, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    pending = store.read_pending()
    assert pending is not None
    different_frames = list(pending.program_frames)
    different_frames[-1] = different_frames[-1].model_copy(
        update={"state_id": "unrelated"}
    )
    store.write_program_checkpoint(
        ProgramCheckpoint(
            workflow_name=workflow.name,
            seq=1,
            verified_state_id="unrelated",
            frames=different_frames,
            bound_params={},
            bundle_version=capability.bundle_version,
        )
    )
    backend = FakeBackend()
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )
    with pytest.raises(AttendedActionRefused, match="monotonic authority"):
        execute_attended_action(
            run,
            _request(capability, key="program-sequence-conflict"),
            operator="staff",
            executor=BoundAttendedExecutor(
                lambda _manifest: Replayer(backend, vision=vision, poll_interval_s=0.0)
            ),
        )
    assert not backend.actions
    assert store.read_pending() is not None


def test_program_resume_refuses_tampered_receipt_target(tmp_path):
    workflow = _attended_program(guarded_transition=True)
    _bundle, run, _initial, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    pending = store.read_pending()
    assert pending is not None
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )
    vision.text_results["ROUTE_A"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )
    decision = execute_attended_action(
        run,
        _request(capability, key="program-receipt-before-tamper"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                FakeBackend(), vision=vision, poll_interval_s=0.0
            )
        ),
    )
    assert decision.status == "completed"
    checkpoint = store.program_checkpoints()[0]
    assert checkpoint is not None and checkpoint.attended_transition is not None
    assert checkpoint.attended_transition.target_state_id == "route-a"
    tampered_receipt = checkpoint.attended_transition.model_copy(
        # route-b is also declared, so structure-only validation would accept
        # it. The signed atomic receipt must still reject the substitution.
        update={"target_state_id": "route-b"}
    )
    manifest = store.read_manifest()
    assert manifest is not None
    with pytest.raises(AttendedActionRefused, match="atomic transition receipt"):
        validate_attended_program_receipt(
            run,
            checkpoint=checkpoint.model_copy(
                update={"attended_transition": tampered_receipt}
            ),
            pending=pending.model_copy(update={"status": "approved"}),
            manifest=manifest,
            workflow=workflow,
            live_bundle_version=capability.bundle_version,
        )


@pytest.mark.parametrize(
    "binding",
    ["workflow_contract", "runtime_inputs", "bound_params"],
)
def test_program_resume_refuses_receipt_with_changed_qualification_binding(
    tmp_path,
    binding,
):
    workflow = _attended_program()
    _bundle, run, _initial, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    pending = store.read_pending()
    assert pending is not None
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )
    decision = execute_attended_action(
        run,
        _request(capability, key=f"program-binding-{binding}"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                FakeBackend(), vision=vision, poll_interval_s=0.0
            )
        ),
    )
    assert decision.status == "completed"
    checkpoint = store.program_checkpoints()[0]
    assert checkpoint.attended_transition is not None
    manifest = store.read_manifest()
    assert manifest is not None

    checked_workflow = workflow.model_copy(deep=True)
    checked_checkpoint = checkpoint
    if binding == "workflow_contract":
        assert checked_workflow.program is not None
        source = checked_workflow.program.states[
            checkpoint.attended_transition.source_state_id
        ]
        assert source.step is not None
        source.step.intent += " changed"
    else:
        update = {
            "pause_id": hashlib.sha256(binding.encode()).hexdigest()[:32],
            "signature": "",
            (
                "governed_runtime_inputs_digest"
                if binding == "runtime_inputs"
                else "bound_params_sha256"
            ): "f" * 64,
        }
        actions = AttendedActionStore(run)
        changed_receipt = actions.seal_program_receipt(
            checkpoint.attended_transition.model_copy(update=update)
        )
        actions.write_program_receipt(changed_receipt)
        checked_checkpoint = checkpoint.model_copy(
            update={"attended_transition": changed_receipt}
        )

    with pytest.raises(AttendedActionRefused, match="lineage"):
        validate_attended_program_receipt(
            run,
            checkpoint=checked_checkpoint,
            pending=pending.model_copy(update={"status": "approved"}),
            manifest=manifest,
            workflow=checked_workflow,
            live_bundle_version=capability.bundle_version,
        )


def test_program_receipt_cannot_replay_across_run_identity(tmp_path):
    workflow = _attended_program()
    _bundle, run, _initial, store, capability = _run_attended_program_to_pause(
        tmp_path / "source", workflow
    )
    pending = store.read_pending()
    assert pending is not None
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )
    decision = execute_attended_action(
        run,
        _request(capability, key="program-cross-run-source"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                FakeBackend(), vision=vision, poll_interval_s=0.0
            )
        ),
    )
    assert decision.status == "completed"

    copied = tmp_path / "copied-run"
    shutil.copytree(run, copied)
    copied_store = CheckpointStore(copied)
    manifest = copied_store.read_manifest()
    assert manifest is not None
    copied_store.write_manifest(manifest.model_copy(update={"run_id": "other-run"}))
    copied_store.write_pending(pending.model_copy(update={"status": "approved"}))
    copied_manifest = copied_store.read_manifest()
    receipt_checkpoint = copied_store.program_checkpoints()[0]
    copied_pending = copied_store.read_pending()
    assert copied_manifest is not None and copied_pending is not None
    with pytest.raises(AttendedActionRefused, match="run|source capability"):
        validate_attended_program_receipt(
            copied,
            checkpoint=receipt_checkpoint,
            pending=copied_pending,
            manifest=copied_manifest,
            workflow=workflow,
            live_bundle_version=capability.bundle_version,
        )


def test_bound_executor_serializes_its_shared_live_session(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    executor = BoundAttendedExecutor(lambda _manifest: pytest.fail("factory called"))
    approval = ApprovalRecord(
        approver="staff",
        resolution="completed by operator",
        bundle_version=capability.bundle_version,
        workflow_name=capability.workflow_name,
        run_dir=str(run),
    )
    assert executor._live_session_lock.acquire(blocking=False)
    try:
        result = executor.continue_run(run, capability, approval)
    finally:
        executor._live_session_lock.release()
    assert result.status == "refused"
    assert "serving another attended action" in result.message


def test_repeated_halt_on_same_step_gets_a_new_exact_pause_capability(tmp_path):
    workflow, _bundle, run, store, first = _paused(tmp_path)
    pending = store.read_pending()
    assert pending is not None
    repeated = pending.model_copy(update={"created_at": "2026-07-18T13:00:00+00:00"})
    store.write_pending(repeated)
    second = issue_attended_capability(
        run,
        store=store,
        pending=repeated,
        workflow=workflow,
        result=StepResult(
            step_id="human",
            intent="press A",
            ok=False,
            error="Please verify you are human",
        ),
    )
    _sync_v2_authority(store)
    assert second.pause_id != first.pause_id
    assert second.pause_digest != first.pause_digest
    assert (first.event_sequence, second.event_sequence) == (1, 2)
    history = json.loads((run / "attended_capability_history.json").read_text())
    assert [item["pause_id"] for item in history] == [first.pause_id]
    with pytest.raises(AttendedActionRefused, match="stale"):
        execute_attended_action(
            run,
            _request(first),
            operator="staff",
            executor=_ResultExecutor(),
        )


def test_tampered_capability_and_stale_page_refuse(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    path = run / "attended_capability.json"
    raw = json.loads(path.read_text())
    raw["step_id"] = "attacker"
    path.write_text(json.dumps(raw))
    with pytest.raises(AttendedActionRefused, match="signature"):
        AttendedActionStore(run).read()

    # Rebuild and present a stale UI digest.
    other = tmp_path / "other"
    _workflow, _bundle, run, _store, capability = _paused(other)
    stale = _request(capability).model_copy(
        update={"capability_digest": "sha256:" + "0" * 64}
    )
    with pytest.raises(AttendedActionRefused, match="stale"):
        execute_attended_action(
            run, stale, operator="staff", executor=_ResultExecutor()
        )


def test_capability_cannot_be_replayed_into_another_run(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path / "one")
    copied = tmp_path / "two" / "run"
    shutil.copytree(run, copied)
    copied_store = CheckpointStore(copied)
    manifest = copied_store.read_manifest()
    assert manifest is not None
    copied_store.write_manifest(manifest.model_copy(update={"run_id": "other-run"}))
    with pytest.raises(AttendedActionRefused, match="canonical path"):
        execute_attended_action(
            copied,
            _request(capability),
            operator="staff",
            executor=_ResultExecutor(),
        )


def test_bundle_revision_change_refuses_before_executor(tmp_path):
    workflow, bundle, run, _store, capability = _paused(tmp_path)
    workflow.steps.append(_step("changed", "C"))
    workflow.save(bundle)
    executor = _ResultExecutor()
    with pytest.raises(Exception, match="bundle"):
        execute_attended_action(
            run,
            _request(capability),
            operator="staff",
            executor=executor,
        )
    assert executor.calls == 0


def test_expired_capability_refuses_before_executor(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    executor = _ResultExecutor()
    after_expiry = datetime.fromisoformat(capability.expires_at) + timedelta(seconds=1)
    with pytest.raises(Exception, match="expired"):
        execute_attended_action(
            run,
            _request(capability),
            operator="staff",
            executor=executor,
            now=after_expiry,
        )
    assert executor.calls == 0


def test_same_request_is_idempotent_and_conflicting_reuse_refuses(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    executor = _ResultExecutor()
    request = _request(capability)
    first = execute_attended_action(
        run,
        request,
        operator="staff",
        decided_by="human",
        executor=executor,
    )
    second = execute_attended_action(
        run,
        request,
        operator="staff",
        decided_by="automation",
        executor=executor,
    )
    assert first == second
    assert first.decided_by == "human"
    assert {
        item.decided_by for item in AttendedActionStore(run)._read_log().decisions
    } == {"human"}
    assert executor.calls == 1
    conflict = request.model_copy(
        update={"action": "skip", "disposition": "not_applicable"}
    )
    with pytest.raises(AttendedActionRefused, match="different request"):
        execute_attended_action(run, conflict, operator="staff", executor=executor)


def test_crash_after_delivery_started_becomes_uncertain_and_never_retries(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)

    class Explodes(_ResultExecutor):
        def continue_run(self, run_dir, capability, approval):
            self.calls += 1
            raise RuntimeError("worker died after delivery boundary")

    executor = Explodes()
    request = _request(capability)
    with pytest.raises(RuntimeError):
        execute_attended_action(
            run,
            request,
            operator="staff",
            decided_by="automation",
            executor=executor,
        )
    journal = json.loads((run / "attended_decisions.json").read_text())["decisions"]
    statuses = [item["status"] for item in journal]
    assert statuses == ["prepared", "delivery_started", "delivery_uncertain"]
    assert {item["decided_by"] for item in journal} == {"automation"}
    with pytest.raises(AttendedActionRefused, match="automatic retry"):
        execute_attended_action(run, request, operator="staff", executor=executor)
    with pytest.raises(AttendedActionRefused, match="another request"):
        execute_attended_action(
            run,
            request.model_copy(update={"idempotency_key": "request-key-0002"}),
            operator="staff",
            executor=executor,
        )
    assert executor.calls == 1


@pytest.mark.parametrize(
    ("action", "disposition", "status"),
    [
        ("teach", "teach_requested", "needs_demonstration"),
        ("escalate", "needs_assistance", "escalated"),
        ("reject", "rejected_by_operator", "rejected"),
    ],
)
def test_non_actuating_and_reject_decisions_keep_trusted_provenance(
    tmp_path, action, disposition, status
):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    request = AttendedActionRequest(
        capability_digest=capability.digest,
        idempotency_key=f"{action}-provenance-key-0001",
        action=action,
        disposition=disposition,
    )

    decision = execute_attended_action(
        run,
        request,
        operator="staff",
        decided_by="human",
    )

    assert decision.status == status
    assert decision.decided_by == "human"


def test_challenge_payload_has_no_answer_code_or_raw_path_surface():
    with pytest.raises(ValidationError):
        AttendedActionRequest.model_validate(
            {
                "capability_digest": "sha256:" + "0" * 64,
                "idempotency_key": "request-key-0001",
                "action": "continue",
                "captcha_answer": "solve-me",
            }
        )
    with pytest.raises(ValidationError):
        AttendedActionRequest.model_validate(
            {
                "capability_digest": "sha256:" + "0" * 64,
                "idempotency_key": "request-key-0001",
                "action": "teach",
                "fix_path": "../../secret.json",
            }
        )


def test_all_actions_require_operator_identity_and_matching_disposition(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    with pytest.raises(Exception, match="authenticated operator"):
        execute_attended_action(
            run,
            _request(capability),
            operator="",
            executor=_ResultExecutor(),
        )
    request = _request(capability).model_copy(update={"disposition": "cannot_complete"})
    with pytest.raises(AttendedActionRefused, match="disposition"):
        execute_attended_action(
            run,
            request,
            operator="staff",
            executor=_ResultExecutor(),
        )


def test_missing_or_insecure_capability_secret_never_recreates_authority(tmp_path):
    _workflow, _bundle, run, _store, _capability = _paused(tmp_path / "missing")
    secret = run / ".attended_capability.key"
    secret.unlink()
    with pytest.raises(AttendedActionRefused, match="key is missing"):
        AttendedActionStore(run).read()
    assert not secret.exists()

    if os.name != "nt":
        _workflow, _bundle, run, _store, _capability = _paused(tmp_path / "permissions")
        secret = run / ".attended_capability.key"
        secret.chmod(0o644)
        with pytest.raises(AttendedActionRefused, match="permissions"):
            AttendedActionStore(run).read()


def test_bound_continue_verifies_then_resumes_after_human_step(tmp_path):
    _workflow, _bundle, run, store, capability = _paused(tmp_path)
    backends: list[FakeBackend] = []

    def factory(_manifest):
        backend = FakeBackend()
        backends.append(backend)
        vision = FakeVision()
        vision.text_results = {
            "DONE": Match(point=(10, 10), region=(0, 0, 20, 20), confidence=1.0)
        }
        return Replayer(backend, vision=vision, poll_interval_s=0.0)

    decision = execute_attended_action(
        run,
        _request(capability),
        operator="front-desk",
        executor=BoundAttendedExecutor(factory),
    )
    assert decision.status == "completed"
    assert decision.report_success is True
    assert len(backends) == 1  # verify and continue the exact live session
    assert all(("press", "A") not in backend.actions for backend in backends)
    assert ("press", "B") in backends[0].actions
    checkpoints = store.checkpoints()
    assert [checkpoint.step_id for checkpoint in checkpoints] == ["human", "next"]
    assert checkpoints[0].actuation == "human_attended"
    assert store.read_pending() is None
    manifest = store.read_manifest()
    assert manifest is not None and manifest.run_id == "run-instance-a"


def test_linear_continue_rebinds_exact_pause_before_checkpoint_commit(tmp_path):
    _workflow, _bundle, run, store, capability = _paused(tmp_path)
    backend = FakeBackend()
    vision = FakeVision()
    vision.text_results = {
        "DONE": Match(point=(10, 10), region=(0, 0, 20, 20), confidence=1.0)
    }

    def factory(_manifest):
        replayer = Replayer(backend, vision=vision, poll_interval_s=0.0)
        original = replayer.revalidate_attended_completion

        def replace_pause_after_live_verification(*args, **kwargs):
            result = original(*args, **kwargs)
            pending = store.read_pending()
            assert pending is not None
            store.write_pending(
                pending.model_copy(
                    update={
                        "step_id": "independently-replaced-pause",
                        "created_at": "2026-07-18T13:00:00+00:00",
                    }
                )
            )
            return result

        replayer.revalidate_attended_completion = replace_pause_after_live_verification
        return replayer

    with pytest.raises(AttendedActionRefused, match="durable state"):
        execute_attended_action(
            run,
            _request(capability, key="request-key-pause-race"),
            operator="front-desk",
            executor=BoundAttendedExecutor(factory),
        )
    assert not backend.actions
    assert store.checkpoints() == []
    assert store.read_approval() is None
    pending = store.read_pending()
    assert pending is not None
    assert pending.step_id == "independently-replaced-pause"


def test_linear_completion_refused_by_remote_permit_keeps_local_progress_unchanged(
    tmp_path, monkeypatch
):
    """A refused production permit cannot commit attended local state."""

    _workflow, _bundle, run, store, capability = _paused(tmp_path)
    monkeypatch.setattr(
        DurableAuthority,
        "_require_remote_delivery_permit",
        lambda _self, _manifest, _record, **_kwargs: (_ for _ in ()).throw(
            DurableAuthorityBusy("remote delivery authority refused")
        ),
    )
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )

    decision = execute_attended_action(
        run,
        _request(capability, key="linear-refused-remote-permit"),
        operator="front-desk",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                FakeBackend(), vision=vision, poll_interval_s=0.0
            )
        ),
    )

    assert decision.status == "refused"
    assert "remote delivery authority refused" in decision.message
    assert store.checkpoints() == []
    assert store.read_approval() is None
    assert store.read_pending() is not None


def test_program_completion_refused_by_remote_permit_keeps_local_progress_unchanged(
    tmp_path, monkeypatch
):
    """The program receipt and checkpoint use the same production fence."""

    workflow = _attended_program()
    _bundle, run, _initial_backend, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    monkeypatch.setattr(
        DurableAuthority,
        "_require_remote_delivery_permit",
        lambda _self, _manifest, _record, **_kwargs: (_ for _ in ()).throw(
            DurableAuthorityBusy("remote delivery authority refused")
        ),
    )
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )

    decision = execute_attended_action(
        run,
        _request(capability, key="program-refused-remote-permit"),
        operator="front-desk",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                FakeBackend(), vision=vision, poll_interval_s=0.0
            )
        ),
    )

    assert decision.status == "refused"
    assert "remote delivery authority refused" in decision.message
    assert store.program_checkpoints() == []
    assert store.read_approval() is None
    assert store.read_pending() is not None
    assert not (
        run / ".attended_program_receipts" / f"{capability.pause_id}.json"
    ).exists()


def test_continue_refuses_live_postcondition_failure_without_actuation(tmp_path):
    _workflow, _bundle, run, store, capability = _paused(tmp_path)
    backends: list[FakeBackend] = []

    def factory(_manifest):
        backend = FakeBackend()
        backends.append(backend)
        return Replayer(backend, vision=FakeVision(), poll_interval_s=0.0)

    decision = execute_attended_action(
        run,
        _request(capability, key="request-key-refuse1"),
        operator="staff",
        executor=BoundAttendedExecutor(factory),
    )
    assert decision.status == "refused"
    assert decision.report_success is False
    assert all(not backend.actions for backend in backends)
    assert store.read_pending() is not None


def test_continue_that_halts_later_rotates_to_the_new_exact_pause(tmp_path):
    workflow = Workflow(
        name="attended-chain",
        steps=[
            _step("human", "A", expect="DONE"),
            _step("next", "B", expect="FINISHED"),
        ],
    )
    _workflow, _bundle, run, store, first = _paused(tmp_path, workflow=workflow)
    backend = FakeBackend()
    vision = FakeVision()
    vision.text_results = {
        "DONE": Match(point=(10, 10), region=(0, 0, 20, 20), confidence=1.0)
    }
    decision = execute_attended_action(
        run,
        _request(first, key="request-key-chain01"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                backend,
                vision=vision,
                poll_interval_s=0.0,
            )
        ),
    )
    assert decision.status == "halted"
    assert ("press", "A") not in backend.actions
    assert ("press", "B") in backend.actions
    pending = store.read_pending()
    assert pending is not None and pending.step_id == "next"
    second = AttendedActionStore(run).read()
    assert second.pause_id != first.pause_id
    assert second.step_id == "next"
    history = json.loads((run / "attended_capability_history.json").read_text())
    assert [item["pause_id"] for item in history] == [first.pause_id]


def test_continue_refuses_effect_that_needs_missing_delivery_baseline(tmp_path):
    effectful = Workflow(
        name="effectful",
        steps=[
            Step(
                id="human",
                intent="human save",
                action=ActionKind.KEY,
                key="A",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"id": "row-1"},
                        count_new_only=True,
                    )
                ],
            )
        ],
    )
    _workflow, _bundle, run, store, capability = _paused(tmp_path, workflow=effectful)

    class CurrentRecords:
        substrate = "fake"

        def capture_pre_state(self, context=None):
            return EffectState(
                substrate="fake",
                reachable=True,
                records=[{"id": "row-1"}],
            )

        def verify(self, expected, before, context=None):
            raise AssertionError("attended readback must not reuse delivery verify")

    def factory(_manifest):
        return Replayer(
            FakeBackend(),
            vision=FakeVision(),
            effect_verifier=CurrentRecords(),
            poll_interval_s=0.0,
        )

    assert "continue" not in capability.allowed_actions
    with pytest.raises(AttendedActionRefused, match="does not allow"):
        execute_attended_action(
            run,
            _request(capability, key="request-key-effect1"),
            operator="staff",
            executor=BoundAttendedExecutor(factory),
        )
    assert store.read_pending() is not None


def test_continue_confirms_absolute_effect_from_current_record_readback(tmp_path):
    effectful = Workflow(
        name="absolute-effect",
        steps=[
            Step(
                id="human",
                intent="human save",
                action=ActionKind.KEY,
                key="A",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"id": "row-1"},
                        forbid_collateral_loss=False,
                    )
                ],
            )
        ],
    )
    _workflow, _bundle, run, store, capability = _paused(tmp_path, workflow=effectful)
    assert "continue" in capability.allowed_actions

    class CurrentRecords:
        substrate = "fake"

        def capture_pre_state(self, context=None):
            return EffectState(
                substrate="fake",
                reachable=True,
                records=[{"id": "row-1"}],
            )

        def verify(self, expected, before, context=None):
            raise AssertionError("attended readback must not reuse delivery verify")

    backend = FakeBackend()
    decision = execute_attended_action(
        run,
        _request(capability, key="request-key-absolute-effect"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                backend,
                vision=FakeVision(),
                effect_verifier=CurrentRecords(),
                poll_interval_s=0.0,
            )
        ),
    )
    assert decision.status == "completed"
    assert store.checkpoints()[0].effect_verified is True
    assert not backend.actions


def test_reconcile_proves_uncertain_write_without_re_dispatch(tmp_path):
    """Reconciliation reads the current effect and never sends the old key."""

    workflow = Workflow(
        name="reconcile-absolute-effect",
        steps=[
            Step(
                id="submit",
                intent="submit the reviewed record",
                action=ActionKind.KEY,
                key="ENTER",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"id": "row-1"},
                        forbid_collateral_loss=False,
                    )
                ],
            )
        ],
    )
    _workflow, _bundle, run, store, _capability = _paused(tmp_path, workflow=workflow)
    uncertainty = ActionDeliveryUncertainty(
        operation="key",
        native=True,
        observed_at="2026-07-29T00:00:01+00:00",
        cause_type="TimeoutError",
    )
    pending = store.read_pending()
    assert pending is not None
    pending = pending.model_copy(
        update={
            "category": "delivery_uncertain",
            "reason": "the submit action may already have been delivered",
            "delivery_uncertainty": uncertainty,
        }
    )
    store.write_pending(pending)
    uncertain_result = StepResult(
        step_id="submit",
        intent="submit the reviewed record",
        ok=False,
        error="the submit action may already have been delivered",
        delivery_attempted=True,
        delivery_uncertainty=uncertainty,
    )
    capability = issue_attended_capability(
        run,
        store=store,
        pending=pending,
        workflow=workflow,
        result=uncertain_result,
    )
    _sync_v2_authority(store)
    assert capability.delivery_state == "unknown"
    assert "reconcile" in capability.allowed_actions
    assert "reject" not in capability.allowed_actions

    class CurrentRecords:
        substrate = "fake"

        def capture_pre_state(self, context=None):
            return EffectState(
                substrate="fake", reachable=True, records=[{"id": "row-1"}]
            )

    backend = FakeBackend()
    decision = execute_attended_action(
        run,
        AttendedActionRequest(
            capability_digest=capability.digest,
            idempotency_key="reconcile-uncertain-write-001",
            action="reconcile",
            disposition="reconciliation_requested",
        ),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                backend,
                vision=FakeVision(),
                effect_verifier=CurrentRecords(),
                poll_interval_s=0.0,
            )
        ),
    )
    assert decision.status == "completed"
    assert decision.report_success is True
    assert decision.transition_receipt_digest is not None
    assert backend.actions == []
    receipt = AttendedActionStore(run).read_reconciliation_receipt(capability.pause_id)
    assert receipt.action == "reconcile"
    assert receipt.request_digest == decision.request_digest
    assert receipt.effect_contract_hashes
    assert receipt.transition_receipt_digest == decision.transition_receipt_digest
    portable = decision_receipt(decision)
    assert portable.action.value == "reconcile"
    assert portable.reason_code.value == "reconciled_and_resumed"
    assert portable.report_success is True
    assert portable.transition_receipt_digest == receipt.transition_receipt_digest


def test_linear_reconciliation_recovers_receipt_and_refuses_zero_or_multiple_matches(
    tmp_path, monkeypatch
):
    """Recovery selects one bound checkpoint and never re-dispatches the action."""

    workflow = Workflow(
        name="reconcile-receipt-recovery",
        steps=[
            Step(
                id="submit",
                intent="submit the reviewed record",
                action=ActionKind.KEY,
                key="ENTER",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"id": "row-1"},
                        forbid_collateral_loss=False,
                    )
                ],
            ),
            Step(
                id="settle",
                intent="wait for the saved record",
                action=ActionKind.WAIT,
            ),
        ],
    )
    _workflow, _bundle, run, store, capability = _paused(tmp_path, workflow=workflow)
    uncertainty = ActionDeliveryUncertainty(
        operation="key",
        native=True,
        observed_at="2026-07-29T00:00:01+00:00",
        cause_type="TimeoutError",
    )
    pending = store.read_pending()
    assert pending is not None
    pending = pending.model_copy(
        update={
            "category": "delivery_uncertain",
            "reason": "the submit action may already have been delivered",
            "delivery_uncertainty": uncertainty,
        }
    )
    store.write_pending(pending)
    capability = issue_attended_capability(
        run,
        store=store,
        pending=pending,
        workflow=workflow,
        result=StepResult(
            step_id=capability.step_id,
            intent="submit the reviewed record",
            ok=False,
            error="the submit action may already have been delivered",
            delivery_attempted=True,
            delivery_uncertainty=uncertainty,
        ),
    )
    _sync_v2_authority(store)

    class CurrentRecords:
        substrate = "fake"

        def capture_pre_state(self, context=None):
            return EffectState(
                substrate="fake", reachable=True, records=[{"id": "row-1"}]
            )

    backend = FakeBackend()
    original = AttendedActionStore.write_reconciliation_receipt
    calls = 0

    def fail_after_resume(self, receipt):
        nonlocal calls
        calls += 1
        raise OSError("receipt persistence interrupted after durable resume")

    request = AttendedActionRequest(
        capability_digest=capability.digest,
        idempotency_key="reconcile-crash-after-resume-001",
        action="reconcile",
        disposition="reconciliation_requested",
    )
    monkeypatch.setattr(
        AttendedActionStore, "write_reconciliation_receipt", fail_after_resume
    )
    with pytest.raises(OSError, match="interrupted after durable resume"):
        execute_attended_action(
            run,
            request,
            operator="staff",
            executor=BoundAttendedExecutor(
                lambda _manifest: Replayer(
                    backend,
                    vision=FakeVision(),
                    effect_verifier=CurrentRecords(),
                    poll_interval_s=0.0,
                )
            ),
        )
    monkeypatch.setattr(AttendedActionStore, "write_reconciliation_receipt", original)
    # Construct a new executor and checkpoint store to model a process that
    # starts after the transition committed but before its local receipt did.
    recovered_store = CheckpointStore(run)
    decision = execute_attended_action(
        run,
        request,
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                backend,
                vision=FakeVision(),
                effect_verifier=CurrentRecords(),
                poll_interval_s=0.0,
            )
        ),
    )
    receipt = AttendedActionStore(run).read_reconciliation_receipt(capability.pause_id)
    assert calls == 2
    assert backend.actions == []
    assert [checkpoint.step_id for checkpoint in recovered_store.checkpoints()] == [
        "submit",
        "settle",
    ]
    assert decision.status == "completed"
    assert decision.transition_receipt_digest == receipt.transition_receipt_digest
    assert decision.transition_receipt_digest == _digest(store.checkpoints()[0])

    recovery = BoundAttendedExecutor(
        lambda _manifest: Replayer(
            backend,
            vision=FakeVision(),
            effect_verifier=CurrentRecords(),
            poll_interval_s=0.0,
        )
    )
    with pytest.raises(AttendedActionRefused, match="does not bind"):
        recovery.recover_reconciliation_receipt(
            run, capability, _digest({"request": "no-match"})
        )
    original_checkpoint = store.checkpoints()[0]
    store.write_checkpoint(
        original_checkpoint.model_copy(
            update={
                "step_index": 99,
                "step_id": "duplicate-reconciliation-binding",
                "next_step_index": 100,
            }
        )
    )
    with pytest.raises(AttendedActionRefused, match="does not bind"):
        recovery.recover_reconciliation_receipt(run, capability, _digest(request))
    assert backend.actions == []


def test_program_reconciliation_recovers_receipt_from_history_after_later_checkpoint(
    tmp_path, monkeypatch
):
    """A fresh process selects the one matching program transition, not the last."""

    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"id": "row-1"},
        forbid_collateral_loss=False,
    )
    workflow = Workflow(
        name="program-reconcile-receipt-recovery",
        program=ProgramGraph(
            entry="human",
            states={
                "human": State(
                    id="human",
                    kind=StateKind.ACTION,
                    step=Step(
                        id="submit",
                        intent="submit the reviewed record",
                        action=ActionKind.KEY,
                        key="ENTER",
                        effects=[effect],
                    ),
                    transitions=[Transition(target="settle")],
                ),
                "settle": State(
                    id="settle",
                    kind=StateKind.ACTION,
                    step=Step(
                        id="settle",
                        intent="wait for the saved record",
                        action=ActionKind.WAIT,
                    ),
                    transitions=[Transition(target="done")],
                ),
                "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
            },
        ),
    )
    _bundle, run, _initial, store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    manifest = store.read_manifest()
    assert manifest is not None
    _AUTHORITY_DIGESTS[str(run.resolve())] = (
        DurableAuthority(run, store).validate(manifest).progress_digest
    )
    uncertainty = ActionDeliveryUncertainty(
        operation="key",
        native=True,
        observed_at="2026-07-29T00:00:01+00:00",
        cause_type="TimeoutError",
    )
    pending = store.read_pending()
    assert pending is not None
    pending = pending.model_copy(
        update={
            "category": "delivery_uncertain",
            "reason": "the submit action may already have been delivered",
            "delivery_uncertainty": uncertainty,
        }
    )
    store.write_pending(pending)
    capability = issue_attended_capability(
        run,
        store=store,
        pending=pending,
        workflow=workflow,
        result=StepResult(
            step_id="submit",
            intent="submit the reviewed record",
            ok=False,
            error="the submit action may already have been delivered",
            delivery_attempted=True,
            delivery_uncertainty=uncertainty,
        ),
    )
    _sync_v2_authority(store)

    class CurrentRecords:
        substrate = "fake"

        def capture_pre_state(self, context=None):
            return EffectState(
                substrate="fake", reachable=True, records=[{"id": "row-1"}]
            )

    backend = FakeBackend()
    original = AttendedActionStore.write_reconciliation_receipt
    calls = 0

    def fail_after_resume(self, receipt):
        nonlocal calls
        calls += 1
        raise OSError("receipt persistence interrupted after durable resume")

    request = AttendedActionRequest(
        capability_digest=capability.digest,
        idempotency_key="program-reconcile-crash-after-resume-001",
        action="reconcile",
        disposition="reconciliation_requested",
    )
    monkeypatch.setattr(
        AttendedActionStore, "write_reconciliation_receipt", fail_after_resume
    )
    with pytest.raises(OSError, match="interrupted after durable resume"):
        execute_attended_action(
            run,
            request,
            operator="staff",
            executor=BoundAttendedExecutor(
                lambda _manifest: Replayer(
                    backend,
                    vision=FakeVision(),
                    effect_verifier=CurrentRecords(),
                    poll_interval_s=0.0,
                )
            ),
        )
    monkeypatch.setattr(AttendedActionStore, "write_reconciliation_receipt", original)

    recovered_store = CheckpointStore(run)
    decision = execute_attended_action(
        run,
        request,
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                backend,
                vision=FakeVision(),
                effect_verifier=CurrentRecords(),
                poll_interval_s=0.0,
            )
        ),
    )
    receipt = AttendedActionStore(run).read_reconciliation_receipt(capability.pause_id)
    checkpoints = recovered_store.program_checkpoints()
    assert calls == 2
    assert backend.actions == []
    assert [checkpoint.verified_state_id for checkpoint in checkpoints] == [
        "human",
        "settle",
    ]
    assert decision.status == "completed"
    assert decision.transition_receipt_digest == receipt.transition_receipt_digest
    assert decision.transition_receipt_digest == _digest(
        checkpoints[0].attended_transition
    )


@pytest.mark.parametrize("report_success", [True, False, None])
def test_completed_executor_result_without_receipt_is_refused(
    tmp_path, monkeypatch, report_success
):
    """The public boundary never journals an unbound completed result."""

    _workflow, _bundle, run, _store, capability = _paused(tmp_path)

    class MissingReceiptExecutor:
        def continue_run(self, _run_dir, _capability, _approval):
            return AttendedExecutionResult(
                status="completed",
                message="unsafe custom result",
                report_success=report_success,
            )

        def skip_run(self, _run_dir, _capability, _approval):
            raise AssertionError("not used")

        def reconcile_run(self, _run_dir, _capability, _approval, _request_digest):
            raise AssertionError("not used")

    from openadapt_flow.runtime.durable.continuation import ContinuationCoordinator

    monkeypatch.setattr(
        ContinuationCoordinator,
        "attest_executor_outcome",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(
        AttendedActionRefused, match="completed attended result requires"
    ):
        execute_attended_action(
            run,
            _request(capability),
            operator="staff",
            executor=MissingReceiptExecutor(),
        )
    assert [item.status for item in AttendedActionStore(run)._read_log().decisions] == [
        "prepared",
        "delivery_started",
    ]


def test_attended_skip_uses_canonical_qualified_risk(tmp_path, monkeypatch):
    environment = EnvironmentBoundary(
        target_kind="web",
        application="Reference",
        application_version="1",
        environment_digest="a" * 64,
        runtime_version="1.24.0",
    )
    cases = (
        (
            Step(
                id="continue",
                intent="Continue to the review screen",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/continue.png",
                    region=(10, 10, 40, 20),
                    click_point=(30, 20),
                    ocr_text="Continue",
                ),
                risk="irreversible",
                risk_explanation="control label contains a consequential-write verb",
                risk_review_required=True,
                guard=Guard(
                    predicate=Predicate(
                        kind=PredicateKind.TEXT_PRESENT,
                        text="Optional section",
                    ),
                    on_unmet="skip",
                ),
            ),
            "read_only",
        ),
        (
            Step(
                id="confirm",
                intent="Open review details",
                action=ActionKind.CLICK,
                risk="reversible",
                guard=Guard(
                    predicate=Predicate(
                        kind=PredicateKind.TEXT_PRESENT,
                        text="Optional section",
                    ),
                    on_unmet="skip",
                ),
            ),
            "consequential",
        ),
    )
    for index, (step, classification) in enumerate(cases):
        workflow = Workflow(name=f"qualified-risk-{index}", steps=[step])
        init_project(workflow, environment=environment)
        set_action_classification(
            workflow,
            ActionRiskClassification(
                step_id=step.id,
                classification=classification,
                explanation="Reviewed operator classification",
                operator_confirmed=True,
            ),
        )
        _workflow, _bundle, _run, store, capability = _paused(
            tmp_path / str(index), workflow=workflow
        )
        assert "skip" not in capability.allowed_actions
        if classification == "read_only":
            policy_digest = "c" * 64
            manifest = store.read_manifest()
            pending = store.read_pending()
            assert manifest is not None and pending is not None
            store.write_manifest(
                manifest.model_copy(
                    update={
                        "governed_authorization": GovernedRunAuthorization(
                            bundle_content_digest="a" * 64,
                            runtime_inputs_digest="b" * 64,
                            admitted_policy_name="clinical-write",
                            admitted_policy_contract_sha256=policy_digest,
                        )
                    }
                )
            )
            with monkeypatch.context() as patch:
                patch.setattr(
                    "openadapt_flow.qualification.current_certification_matches",
                    lambda _workflow, *, policy=None, policy_contract_digest=None: (
                        policy is None and policy_contract_digest == policy_digest
                    ),
                )
                admitted = issue_attended_capability(
                    _run,
                    store=store,
                    pending=pending,
                    workflow=workflow,
                    result=StepResult(
                        step_id=step.id,
                        intent=step.intent,
                        ok=False,
                        error="Please verify you are human",
                    ),
                )
            assert admitted.pause_id != capability.pause_id
            assert "skip" in admitted.allowed_actions


def test_api_effect_cannot_make_human_gui_path_skippable(tmp_path):
    api_effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"id": "api-row"},
        risk="irreversible",
    )
    workflow = Workflow(
        name="api-effect-split",
        steps=[
            Step(
                id="write",
                intent="Open the optional record",
                action=ActionKind.CLICK,
                risk="reversible",
                guard=Guard(
                    predicate=Predicate(
                        kind=PredicateKind.TEXT_PRESENT,
                        text="Optional section",
                    ),
                    on_unmet="skip",
                ),
                api_binding=ApiBinding(
                    url_template="/records",
                    effects=[api_effect],
                ),
            )
        ],
    )
    _workflow, _bundle, _run, _store, capability = _paused(tmp_path, workflow=workflow)
    assert "skip" not in capability.allowed_actions
    assert "continue" not in capability.allowed_actions


def test_attended_completion_verifies_only_the_human_gui_effect_path(tmp_path):
    gui_effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"id": "gui-row"},
        risk="irreversible",
        forbid_collateral_loss=False,
    )
    api_effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"id": "api-row"},
        risk="irreversible",
        forbid_collateral_loss=False,
    )
    workflow = Workflow(
        name="attended-effect-path",
        steps=[
            Step(
                id="human",
                intent="human save",
                action=ActionKind.KEY,
                key="A",
                effects=[gui_effect],
                api_binding=ApiBinding(
                    url_template="/records",
                    effects=[api_effect],
                ),
            )
        ],
    )
    _workflow, _bundle, run, store, capability = _paused(tmp_path, workflow=workflow)

    class CurrentRecords:
        substrate = "fake"

        def capture_pre_state(self, context=None):
            return EffectState(
                substrate="fake",
                reachable=True,
                records=[{"id": "gui-row"}],
            )

        def verify(self, expected, before, context=None):
            raise AssertionError("attended readback must not reuse delivery verify")

    decision = execute_attended_action(
        run,
        _request(capability, key="request-key-gui-effect-path"),
        operator="staff",
        executor=BoundAttendedExecutor(
            lambda _manifest: Replayer(
                FakeBackend(),
                vision=FakeVision(),
                effect_verifier=CurrentRecords(),
                poll_interval_s=0.0,
            )
        ),
    )
    checkpoint = store.checkpoints()[0]
    assert decision.status == "completed"
    assert checkpoint.effect_contract_hashes == [gui_effect.contract_hash()]
    assert api_effect.contract_hash() not in checkpoint.effect_contract_hashes


def test_skip_requires_declared_nonconsequential_skip_semantics(tmp_path):
    generic, _bundle, run, store, capability = _paused(tmp_path / "generic")
    executor = BoundAttendedExecutor(
        lambda _manifest: Replayer(
            FakeBackend(), vision=FakeVision(), poll_interval_s=0.0
        )
    )
    assert "skip" not in capability.allowed_actions
    with pytest.raises(AttendedActionRefused, match="does not allow"):
        execute_attended_action(
            run,
            _request(capability, action="skip", key="request-key-skip1"),
            operator="staff",
            executor=executor,
        )
    assert store.read_pending() is not None
    assert generic.steps[0].guard is None

    optional = Workflow(
        name="optional",
        steps=[
            Step(
                id="optional",
                intent="optional dismissal",
                action=ActionKind.KEY,
                key="A",
                guard=Guard(
                    predicate=Predicate(
                        kind=PredicateKind.TEXT_PRESENT, text="OPTIONAL"
                    ),
                    on_unmet="skip",
                ),
            ),
            _step("next", "B"),
        ],
    )
    _workflow, _bundle, run, store, capability = _paused(
        tmp_path / "optional", workflow=optional
    )
    decision = execute_attended_action(
        run,
        _request(capability, action="skip", key="request-key-skip2"),
        operator="staff",
        executor=executor,
    )
    assert decision.status == "completed"
    assert store.checkpoints()[0].skipped is True


def test_teach_and_escalate_are_audited_without_actuation(tmp_path):
    _workflow, _bundle, run, store, capability = _paused(tmp_path)
    teach = execute_attended_action(
        run,
        AttendedActionRequest(
            capability_digest=capability.digest,
            idempotency_key="request-key-teach",
            action="teach",
            disposition="teach_requested",
        ),
        operator="staff",
    )
    assert teach.status == "needs_demonstration"
    assert "identity-evidence" in teach.message
    escalated = execute_attended_action(
        run,
        AttendedActionRequest(
            capability_digest=capability.digest,
            idempotency_key="request-key-escalate",
            action="escalate",
            disposition="needs_assistance",
        ),
        operator="staff",
    )
    assert escalated.status == "escalated"
    assert store.read_pending() is not None


def test_encrypted_pause_uses_environment_key_and_protected_capability_secret(
    tmp_path, monkeypatch
):
    key = "correct horse battery staple"
    workflow = Workflow(
        name="sealed",
        steps=[_step("human", "A", expect="DONE")],
    )
    bundle = tmp_path / "bundle"
    run = tmp_path / "run"
    workflow.save(bundle, encrypt=True, key=key)
    store = CheckpointStore(run, key=key)
    _write_v2_manifest(
        store,
        RunManifest(
            run_id="sealed-run",
            workflow_name=workflow.name,
            bundle_dir=str(bundle),
        ),
    )
    pending = PendingEscalation(
        run_id="sealed-run",
        workflow_name=workflow.name,
        step_index=0,
        step_id="human",
        category="human_required",
        reason="MFA required",
    )
    store.write_pending(pending)
    capability = issue_attended_capability(
        run,
        store=store,
        pending=pending,
        workflow=workflow,
        result=StepResult(
            step_id="human", intent="press A", ok=False, error="MFA required"
        ),
    )
    _sync_v2_authority(store)
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", key)
    decision = execute_attended_action(
        run,
        AttendedActionRequest(
            capability_digest=capability.digest,
            idempotency_key="request-key-sealed",
            action="escalate",
            disposition="needs_assistance",
        ),
        operator="staff",
    )
    assert decision.status == "escalated"
    assert (run / "pending_escalation.json.enc").is_file()
    assert not (run / "pending_escalation.json").exists()
    assert (run / ".attended_capability.key").stat().st_mode & 0o077 == 0


def test_lease_refuses_concurrent_or_crashed_delivery(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    request = _request(capability)
    store = AttendedActionStore(run)
    with store.lease(request):
        with pytest.raises(AttendedActionRefused, match="already in progress"):
            with store.lease(request):
                pass

    pending = CheckpointStore(run).read_pending()
    assert pending is not None
    expired = ContinuationLeaseRecord(
        attempt_id="old-attempt",
        run_id=pending.run_id,
        pause_binding_sha256=approval_pause_digest(pending),
        operation="continue",
        owner_nonce_sha256="sha256:" + "0" * 64,
        acquired_at="2020-01-01T00:00:00+00:00",
        expires_at="2020-01-01T00:01:00+00:00",
    )
    store.lease_path.write_text(expired.model_dump_json())
    with store.lease(
        request,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ):
        pass


def test_attended_http_action_requires_auth_csrf_and_exact_capability(
    tmp_path, monkeypatch
):
    _workflow, bundle, run, _store, capability = _paused(tmp_path)
    monkeypatch.setattr(
        "openadapt_flow.console.app._local_operator_identity", lambda: "staff"
    )
    executor = _ResultExecutor()

    class Service:
        decided_by: list[str] = []

        def execute(self, run_dir, request, *, operator, decided_by="unknown"):
            self.decided_by.append(decided_by)
            return execute_attended_action(
                run_dir,
                request,
                operator=operator,
                decided_by=decided_by,
                executor=executor,
            )

    app = create_app(
        bundle.parent,
        run.parent,
        allow_actions=True,
        attend=True,
        attended_service=Service(),
    )
    unauthenticated = TestClient(app, base_url="http://127.0.0.1")
    assert unauthenticated.get("/api/attention").status_code == 401
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={
            "Authorization": f"Bearer {app.state.console_access_token}",
            "Origin": "http://127.0.0.1",
            "X-OpenAdapt-CSRF": app.state.console_csrf_token,
        },
    )
    item = client.get("/api/attention").json()[0]
    health = client.get("/api/health").json()
    assert health["attended_decisions_ready"] is True
    assert health["attended_actions_ready"] is True
    assert item["capability"]["digest"] == capability.digest
    assert "expected_next_transition" not in item["capability"]
    detail = client.get(f"/api/attention/{item['id']}").json()
    task = detail["task"]
    assert task["schema_version"] == "openadapt.human-decision-task/v1"
    assert task["capability_digest"] == capability.digest
    assert task["run_id"].startswith("run_")
    assert capability.run_id not in json.dumps(detail)
    assert task["question"]["template"] == "review_uncertain_delivery"
    assert "Please verify you are human" not in json.dumps(detail)
    payload = {
        **_request(capability, key="request-key-http1").model_dump(),
        "task_digest": detail["task_digest"],
        "task_signature": task["signature"],
    }
    tampered = client.post(
        f"/api/attention/{item['id']}/actions/continue",
        json={
            **payload,
            "idempotency_key": "request-key-http-tampered",
            "task_signature": "hmac-sha256:" + ("0" * 64),
        },
    )
    assert tampered.status_code == 409
    assert executor.calls == 0
    response = client.post(
        f"/api/attention/{item['id']}/actions/continue",
        json=payload,
    )
    assert response.status_code == 200
    # The browser boundary returns the closed, PHI-free receipt: the engine's
    # free-text message and operator principal stay in the durable audit.
    receipt = response.json()
    assert receipt["state"] == "completed"
    assert receipt["action"] == "verify_and_resume"
    assert "message" not in receipt and "operator" not in receipt
    assert executor.calls == 1
    assert Service.decided_by == ["human"]

    wrong_path = client.post(
        f"/api/attention/{item['id']}/actions/skip",
        json={**payload, "idempotency_key": "request-key-http2"},
    )
    assert wrong_path.status_code == 400


def test_remote_projection_is_explicit_aal2_phi_free_and_exactly_bound(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    item = attention_item(run.parent, run)
    assert item is not None

    with pytest.raises(AttendedActionRefused, match="not explicitly enabled"):
        portable_remote_decision_task(run, item, deployment=DeploymentConfig())

    projection = portable_remote_decision_task(
        run, item, deployment=_remote_deployment()
    )
    task = projection.task
    assert task.required_authn.value == "aal2"
    assert task.tenant_id == "tenant_exact_01"
    assert task.runner_id == "runner_exact_01"
    assert task.capability_digest == capability.digest
    assert projection.expected_transition_digest == (
        capability.expected_transition_digest
    )
    assert projection.event_sequence == capability.event_sequence == 1
    serialized = projection.model_dump_json()
    for protected in (
        "Please verify you are human",
        "press A",
        str(run),
        str(run.parent / "bundle"),
    ):
        assert protected not in serialized


def test_remote_response_refuses_scope_or_binding_drift(tmp_path):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    item = attention_item(run.parent, run)
    assert item is not None
    deployment = _remote_deployment()
    projection = portable_remote_decision_task(run, item, deployment=deployment)
    request = _remote_request(projection, capability)
    principal = _remote_principal()

    # Exercise representative dimensions of the one signed binding without
    # proliferating one source-text test per field.
    mutations = (
        {"tenant_id": "tenant_other_01"},
        {"runner_id": "runner_other_01"},
        {"event_sequence": projection.event_sequence + 1},
        {"binding_digest": "sha256:" + ("0" * 64)},
    )
    for update in mutations:
        with pytest.raises(AttendedActionRefused):
            execute_remote_attended_action(
                run,
                item,
                request.model_copy(update=update),
                deployment=deployment,
                principal=principal,
                executor=_ResultExecutor(),
            )


def test_remote_decision_is_idempotent_and_resumes_through_fresh_remote_actuation(
    tmp_path,
):
    workflow = Workflow(
        name="remote-attended",
        steps=[
            _step("human", "A", expect="DONE"),
            click_step("save", risk="irreversible", ocr_text="Save"),
        ],
    )
    _workflow, bundle, run, _store, capability = _paused(tmp_path, workflow=workflow)
    template = bundle / "templates" / "btn.png"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_bytes(make_png())
    item = attention_item(run.parent, run)
    assert item is not None
    deployment = _remote_deployment()
    projection = portable_remote_decision_task(run, item, deployment=deployment)
    request = _remote_request(projection, capability)

    frame = make_png()
    backend = RemoteLeaseBackend(initial_frame=frame, fresh_frame=frame)
    vision = FakeVision()
    vision.text_results = {
        "DONE": Match(point=(10, 10), region=(0, 0, 20, 20), confidence=1.0)
    }
    # Prove the successor at attended revalidation, resolve again during
    # resume, then re-resolve after the remote backend reacquires focus/frame.
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.99),
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.99),
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.99),
    ]
    executor = BoundAttendedExecutor(
        lambda _manifest: Replayer(backend, vision=vision, poll_interval_s=0.0)
    )
    first = execute_remote_attended_action(
        run,
        item,
        request,
        deployment=deployment,
        principal=_remote_principal(),
        executor=executor,
    )
    second = execute_remote_attended_action(
        run,
        item,
        request,
        deployment=deployment,
        principal=_remote_principal(),
        executor=executor,
    )

    assert first == second
    assert first.decided_by == "human"
    assert first.status == "completed"
    assert backend.acquire_count == 1
    assert ("press", "A") not in backend.actions
    assert backend.actions == [("click", 110, 105, False)]


def test_public_attended_service_executes_exact_request_on_owner(tmp_path, monkeypatch):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    executor = _ResultExecutor()
    monkeypatch.setattr(
        "openadapt_flow.runtime.durable.attended_service._deployment_executor",
        lambda _deployment, *, key: nullcontext(executor),
    )

    with AttendedActionService(DeploymentConfig()) as service:
        decision = service.execute(
            run,
            _request(capability, key="request-key-public-service"),
            operator="staff",
            decided_by="automation",
        )
        owner_thread = service._owner.owner_thread_id

    assert decision.status == "completed"
    assert decision.decided_by == "automation"
    assert executor.calls == 1
    assert owner_thread is not None


def test_attended_http_can_teach_or_escalate_without_live_executor(
    tmp_path, monkeypatch
):
    _workflow, bundle, run, _store, capability = _paused(tmp_path)
    monkeypatch.setattr(
        "openadapt_flow.console.app._local_operator_identity", lambda: "staff"
    )
    app = create_app(
        bundle.parent,
        run.parent,
        allow_actions=True,
        attend=True,
    )
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={
            "Authorization": f"Bearer {app.state.console_access_token}",
            "Origin": "http://127.0.0.1",
            "X-OpenAdapt-CSRF": app.state.console_csrf_token,
        },
    )
    health = client.get("/api/health").json()
    assert health["attended_decisions_ready"] is True
    assert health["attended_actions_ready"] is False
    item = client.get("/api/attention").json()[0]
    detail = client.get(f"/api/attention/{item['id']}").json()
    response = client.post(
        f"/api/attention/{item['id']}/actions/teach",
        json={
            "capability_digest": capability.digest,
            "task_digest": detail["task_digest"],
            "task_signature": detail["task"]["signature"],
            "idempotency_key": "request-key-http-teach",
            "action": "teach",
            "disposition": "teach_requested",
        },
    )
    assert response.status_code == 200
    assert response.json()["state"] == "demonstration_requested"


def test_reject_ends_the_run_where_teach_and_escalate_leave_it_resumable(tmp_path):
    """The three non-actuating answers must not converge on one outcome.

    ``teach`` and ``escalate`` both leave the durable pause ``pending``; the
    run can still be approved and resumed. ``reject`` marks it ``rejected``,
    which no approval overrides. If these collapsed into one recorded state,
    the answer distribution could not tell "someone will pick this up" from
    "this run is over" -- which is the entire reason reject exists as its own
    member rather than as a second label on escalate.
    """
    from openadapt_flow.runtime.durable.approval import RunRejected

    for action, disposition, status in (
        ("teach", "teach_requested", "needs_demonstration"),
        ("escalate", "needs_assistance", "escalated"),
    ):
        _wf, _bundle, run, store, capability = _paused(tmp_path / action)
        decision = execute_attended_action(
            run,
            AttendedActionRequest(
                capability_digest=capability.digest,
                idempotency_key=f"parks-the-run-{action}-01",
                action=action,
                disposition=disposition,
            ),
            operator="staff",
        )
        assert decision.status == status
        assert store.read_pending().status == "pending"

    _wf, _bundle, run, store, capability = _paused(tmp_path / "reject")
    decision = execute_attended_action(
        run,
        AttendedActionRequest(
            capability_digest=capability.digest,
            idempotency_key="ends-the-run-reject-01",
            action="reject",
            disposition="rejected_by_operator",
        ),
        operator="staff",
    )
    assert decision.status == "rejected"
    assert decision.disposition == "rejected_by_operator"
    assert store.read_pending().status == "rejected"
    with pytest.raises(RunRejected):
        enforce_resume_authorization(
            store.read_pending(),
            ApprovalRecord(approver="supervisor", resolution="resume anyway"),
            bundle_version="",
        )


def test_reject_admission_refuses_every_mutation_of_its_preconditions(tmp_path):
    """Each precondition, removed one at a time, must refuse on its own.

    A rejection is terminal and unactuated, which makes it tempting to admit
    cheaply. It is admitted through the same authority as every other attended
    action: an authenticated operator, a matching closed disposition, the exact
    signed capability, and that capability's own action set.
    """
    _wf, _bundle, run, _store, capability = _paused(tmp_path / "mutations")

    # (1) A disposition that does not belong to `reject`.
    for wrong in ("completed_by_operator", "needs_assistance", "teach_requested"):
        with pytest.raises(AttendedActionRefused, match="disposition"):
            execute_attended_action(
                run,
                AttendedActionRequest(
                    capability_digest=capability.digest,
                    idempotency_key=f"reject-wrong-disposition-{wrong}",
                    action="reject",
                    disposition=wrong,
                ),
                operator="staff",
            )

    # (2) No authenticated operator.
    with pytest.raises(ApprovalRequired):
        execute_attended_action(
            run,
            AttendedActionRequest(
                capability_digest=capability.digest,
                idempotency_key="reject-without-operator-1",
                action="reject",
                disposition="rejected_by_operator",
            ),
            operator="   ",
        )

    # (3) A capability digest that is not this pause's.
    with pytest.raises(AttendedActionRefused):
        execute_attended_action(
            run,
            AttendedActionRequest(
                capability_digest="sha256:" + "f" * 64,
                idempotency_key="reject-wrong-capability-1",
                action="reject",
                disposition="rejected_by_operator",
            ),
            operator="staff",
        )

    # (4) A pause whose signed capability never carried `reject`, because the
    # runtime positively recorded that this step may already have actuated.
    uncertain_run = tmp_path / "uncertain" / "run"
    workflow = Workflow(name="uncertain", steps=[_step("human", "A", expect="DONE")])
    bundle = tmp_path / "uncertain" / "bundle"
    workflow.save(bundle)
    store = CheckpointStore(uncertain_run)
    _write_v2_manifest(
        store,
        RunManifest(
            run_id="run-uncertain-a",
            workflow_name=workflow.name,
            bundle_dir=str(bundle),
            params={},
        ),
    )
    uncertainty = ActionDeliveryUncertainty(
        operation="click",
        native=True,
        observed_at="2026-07-18T12:00:01+00:00",
        cause_type="TimeoutError",
    )
    pending = PendingEscalation(
        run_id="run-uncertain-a",
        workflow_name=workflow.name,
        step_index=0,
        step_id="human",
        intent=workflow.steps[0].intent,
        category="delivery_uncertain",
        reason="the action may already have been delivered",
        resume_from_index=0,
        delivery_uncertainty=uncertainty,
    )
    store.write_pending(pending)
    uncertain_result = StepResult(
        step_id="human",
        intent=workflow.steps[0].intent,
        ok=False,
        error="the action may already have been delivered",
        delivery_attempted=True,
        delivery_uncertainty=uncertainty,
    )
    RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-18T12:00:00+00:00",
        success=False,
        results=[uncertain_result],
    ).save(uncertain_run)
    uncertain = issue_attended_capability(
        uncertain_run,
        store=store,
        pending=pending,
        workflow=workflow,
        result=uncertain_result,
    )
    _sync_v2_authority(store)
    assert uncertain.delivery_state == "unknown"
    assert "reject" not in uncertain.allowed_actions
    # And escalate survives: handing a possibly-landed write to someone who can
    # reconcile is the correct answer there, not throwing the pause away.
    assert "escalate" in uncertain.allowed_actions
    with pytest.raises(AttendedActionRefused, match="does not allow this action"):
        execute_attended_action(
            uncertain_run,
            AttendedActionRequest(
                capability_digest=uncertain.digest,
                idempotency_key="reject-uncertain-pause-01",
                action="reject",
                disposition="rejected_by_operator",
            ),
            operator="staff",
        )
    assert store.read_pending().status == "pending"

    # Nothing above may have ended the original run.
    assert CheckpointStore(run).read_pending().status == "pending"


def test_a_rejection_still_ends_the_run_when_the_report_is_unreadable(tmp_path):
    """An unreadable report must not prevent an operator from stopping a run.

    The report is where the terminal transaction outcome is recorded, so a
    rejection over a corrupt one loses that record. It does not lose the
    TERMINATION: the pause status is the enforcement point, and refusing to let
    an operator stop a run because a JSON file will not parse would be the
    wrong failure to choose. The decision message says the outcome is
    unrecorded rather than implying one was written.
    """
    _wf, _bundle, run, store, capability = _paused(tmp_path / "corrupt")
    (run / "report.json").write_text("{not json", encoding="utf-8")

    decision = execute_attended_action(
        run,
        AttendedActionRequest(
            capability_digest=capability.digest,
            idempotency_key="reject-corrupt-report-01",
            action="reject",
            disposition="rejected_by_operator",
        ),
        operator="staff",
    )
    assert decision.status == "rejected"
    assert "unrecorded" in decision.message
    assert store.read_pending().status == "rejected"
    # And the corrupt file was not overwritten with a partial report.
    assert (run / "report.json").read_text(encoding="utf-8") == "{not json"
    assert not list(run.glob("*.rejecting"))
