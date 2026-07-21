"""Adversarial tests for exact, single-execution governed authorizations."""

from __future__ import annotations

from openadapt_flow.ir import (
    ActionKind,
    Guard,
    Postcondition,
    PostconditionKind,
    Predicate,
    PredicateKind,
    ProgramGraph,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
)
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    UnverifiedWriteApproval,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.durable import (
    ApprovalRecord,
    CheckpointStore,
    bundle_version,
    resume,
)
from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import (
    FakeBackend,
    FakeVision,
    Match,
    OcrLine,
    context_click_step,
    make_png,
    resolving_vision,
)


def _seal(tmp_path, workflow: Workflow) -> tuple[Workflow, object]:
    bundle = tmp_path / workflow.name
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    (bundle / "templates" / "identity.png").write_bytes(make_png((80, 20)))
    workflow.save(bundle)
    return Workflow.load(bundle), bundle


class _MutatingVision(FakeVision):
    def __init__(self, path, *, mutate_on=1):
        super().__init__()
        self.path = path
        self.mutate_on = mutate_on
        self.template_results = [
            Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.99)
        ]

    def wait_settled(self, backend, **kwargs):
        frame = super().wait_settled(backend, **kwargs)
        if self.settle_count == self.mutate_on:
            self.path.write_bytes(b"concurrent substitution")
        return frame


def _authorization(
    workflow: Workflow,
    *,
    params: dict[str, str] | None = None,
    worklists: dict[str, list[dict[str, str]]] | None = None,
    required: tuple[str, ...] = (),
) -> GovernedRunAuthorization:
    assert workflow.manifest is not None
    return GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(workflow, params, worklists),
        admitted_policy_name="test-governed",
        required_identity_step_ids=required,
    )


def test_in_memory_semantic_mutation_halts_before_action(tmp_path):
    step = context_click_step("Jane Sample 1980-01-15 MRN 123")
    workflow, bundle = _seal(tmp_path, Workflow(name="semantic", steps=[step]))
    authorization = _authorization(workflow, required=(step.id,))
    workflow.steps[0].identity_armed = False
    workflow.steps[0].anchor.context_text = None
    workflow.steps[0].anchor.structured_identity = None
    backend = FakeBackend()

    report = Replayer(
        backend, vision=resolving_vision(), governed_authorization=authorization
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run")

    assert report.success is False
    assert backend.actions == []
    assert report.results[0].step_id == "<authorization>"
    assert "in-memory workflow semantics" in (report.results[0].error or "")


def test_bundle_asset_mismatch_halts_before_action(tmp_path):
    step = context_click_step("Jane Sample 1980-01-15 MRN 123")
    workflow, bundle = _seal(tmp_path, Workflow(name="assets", steps=[step]))
    authorization = _authorization(workflow, required=(step.id,))
    (bundle / "templates" / "btn.png").write_bytes(b"tampered")
    backend = FakeBackend()

    report = Replayer(
        backend, vision=resolving_vision(), governed_authorization=authorization
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run")

    assert report.success is False
    assert backend.actions == []
    assert "bundle integrity failed" in (report.results[0].error or "")


def test_target_asset_mutation_after_validation_halts_before_action(tmp_path):
    step = context_click_step("Jane Sample 1980-01-15 MRN 123")
    workflow, bundle = _seal(tmp_path, Workflow(name="target_race", steps=[step]))
    authorization = _authorization(workflow, required=(step.id,))
    backend = FakeBackend()
    vision = _MutatingVision(bundle / "templates" / "btn.png")
    vision.ocr_lines = [OcrLine("Jane Sample 1980-01-15 MRN 123")]

    report = Replayer(backend, vision=vision, governed_authorization=authorization).run(
        workflow, bundle_dir=bundle, run_dir=tmp_path / "run"
    )

    assert report.success is False
    assert backend.actions == []
    assert "changed after admission" in (report.results[0].error or "")
    assert vision.template_png_calls == [make_png((50, 20))]


def test_identity_asset_uses_same_mutation_guard(tmp_path):
    step = context_click_step("Jane Sample 1980-01-15 MRN 123")
    step.anchor.identifier_crop = "templates/identity.png"
    step.anchor.identifier_region = (160, 95, 80, 20)
    workflow, bundle = _seal(tmp_path, Workflow(name="identity_race", steps=[step]))
    authorization = _authorization(workflow, required=(step.id,))
    backend = FakeBackend()
    vision = _MutatingVision(bundle / "templates" / "identity.png")
    vision.ocr_lines = []

    report = Replayer(backend, vision=vision, governed_authorization=authorization).run(
        workflow, bundle_dir=bundle, run_dir=tmp_path / "run"
    )

    assert report.success is False
    assert backend.actions == []
    assert "changed after admission" in (report.results[0].error or "")


def test_guard_skip_cannot_hide_governed_asset_mutation(tmp_path):
    step = context_click_step("Jane Sample 1980-01-15 MRN 123")
    step.guard = Guard(
        predicate=Predicate(
            kind=PredicateKind.ANCHOR_RESOLVES,
            anchor=step.anchor.model_copy(deep=True),
        ),
        on_unmet="skip",
    )
    workflow, bundle = _seal(tmp_path, Workflow(name="guard_race", steps=[step]))
    authorization = _authorization(workflow)
    backend = FakeBackend()
    vision = _MutatingVision(bundle / "templates" / "btn.png")

    report = Replayer(backend, vision=vision, governed_authorization=authorization).run(
        workflow, bundle_dir=bundle, run_dir=tmp_path / "run"
    )

    assert report.success is False
    assert backend.actions == []
    assert report.results[0].safety_halt is True
    assert report.results[0].skipped is False
    assert "changed after admission" in (report.results[0].error or "")


def test_program_transition_cannot_hide_governed_asset_mutation(tmp_path):
    anchor = context_click_step("Jane Sample 1980-01-15 MRN 123").anchor
    program = ProgramGraph(
        entry="branch",
        states={
            "branch": State(
                id="branch",
                kind=StateKind.BRANCH,
                transitions=[
                    Transition(
                        target="done",
                        guard=Predicate(
                            kind=PredicateKind.ANCHOR_RESOLVES,
                            anchor=anchor,
                        ),
                    )
                ],
            ),
            "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
        },
    )
    workflow, bundle = _seal(
        tmp_path, Workflow(name="transition_race", program=program)
    )
    authorization = _authorization(workflow)
    backend = FakeBackend()
    vision = _MutatingVision(bundle / "templates" / "btn.png")

    report = Replayer(backend, vision=vision, governed_authorization=authorization).run(
        workflow, bundle_dir=bundle, run_dir=tmp_path / "run"
    )

    assert report.success is False
    assert report.terminal_outcome == "halt"
    assert backend.actions == []
    assert report.results[-1].safety_halt is True
    assert "changed after admission" in (report.results[-1].error or "")


def test_scroll_stop_predicate_mutation_halts_before_scroll(tmp_path):
    anchor = context_click_step("Jane Sample 1980-01-15 MRN 123").anchor
    scroll = Step(
        id="scroll",
        intent="scroll until target appears",
        action=ActionKind.SCROLL,
        scroll_dx=0,
        scroll_dy=300,
        wait_until=Predicate(
            kind=PredicateKind.ANCHOR_RESOLVES,
            anchor=anchor,
        ),
    )
    workflow, bundle = _seal(tmp_path, Workflow(name="scroll_race", steps=[scroll]))
    authorization = _authorization(workflow)
    backend = FakeBackend()
    vision = _MutatingVision(bundle / "templates" / "btn.png")

    report = Replayer(backend, vision=vision, governed_authorization=authorization).run(
        workflow, bundle_dir=bundle, run_dir=tmp_path / "run"
    )

    assert report.success is False
    assert backend.actions == []
    assert report.results[0].safety_halt is True
    assert "changed after admission" in (report.results[0].error or "")


def test_transition_halt_checkpoints_already_performed_write(tmp_path):
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient": "A"},
        idempotency_key="transition-write",
    )
    write = Step(
        id="write",
        intent="save record",
        action=ActionKind.KEY,
        key="Enter",
        effects=[effect],
        expect=[Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved")],
    )
    anchor = context_click_step("Jane Sample 1980-01-15 MRN 123").anchor
    program = ProgramGraph(
        entry="write-state",
        states={
            "write-state": State(
                id="write-state",
                kind=StateKind.ACTION,
                step=write,
                transitions=[
                    Transition(
                        target="done",
                        guard=Predicate(
                            kind=PredicateKind.ANCHOR_RESOLVES,
                            anchor=anchor,
                        ),
                    )
                ],
            ),
            "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
        },
    )
    workflow, bundle = _seal(
        tmp_path, Workflow(name="transition_checkpoint", program=program)
    )
    assert workflow.manifest is not None
    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(workflow, None, None),
        admitted_policy_name="test-governed",
        unverified_write_approvals=(
            UnverifiedWriteApproval(
                step_id="write", effect_contract_hashes=(effect.contract_hash(),)
            ),
        ),
    )
    backend = FakeBackend()
    target_path = bundle / "templates" / "btn.png"
    original_target = target_path.read_bytes()
    vision = _MutatingVision(target_path, mutate_on=3)
    vision.text_results = {
        "Saved": Match(point=(1, 1), region=(0, 0, 2, 2), confidence=0.99)
    }
    run_dir = tmp_path / "run"

    report = Replayer(
        backend,
        vision=vision,
        governed_authorization=authorization,
        durable=True,
        poll_interval_s=0.0,
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is False
    assert backend.actions == [("press", "Enter")]
    checkpoint = CheckpointStore(run_dir).last_program_checkpoint()
    assert checkpoint is not None
    assert checkpoint.verified_state_id == "write-state"
    assert checkpoint.new_effect_keys == []
    assert checkpoint.new_unverified_effect_keys == [effect.contract_hash()]

    target_path.write_bytes(original_target)
    resumed_backend = FakeBackend()
    resumed_vision = resolving_vision()
    resumed_vision.text_results = {
        "Saved": Match(point=(1, 1), region=(0, 0, 2, 2), confidence=0.99)
    }
    resumed = resume(
        run_dir,
        Replayer(resumed_backend, vision=resumed_vision, poll_interval_s=0.0),
        approval=ApprovalRecord(
            approver="operator@example.com",
            resolution="continue after guarded transition halt",
            bundle_version=bundle_version(bundle),
        ),
    )
    assert resumed.success is True
    assert resumed_backend.actions == []


def test_authorization_is_single_use(tmp_path):
    workflow, bundle = _seal(
        tmp_path,
        Workflow(
            name="single_use",
            steps=[
                context_click_step("Jane Sample 1980-01-15 MRN 123", risk="reversible")
            ],
        ),
    )
    step = workflow.steps[0]
    authorization = _authorization(workflow, required=(step.id,))
    first_backend = FakeBackend()
    vision = resolving_vision()
    vision.ocr_lines = []

    Replayer(first_backend, vision=vision, governed_authorization=authorization).run(
        workflow, bundle_dir=bundle, run_dir=tmp_path / "run1"
    )
    second_backend = FakeBackend()
    report = Replayer(
        second_backend,
        vision=resolving_vision(),
        governed_authorization=authorization,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run2")

    assert report.success is False
    assert second_backend.actions == []
    assert "already consumed" in (report.results[0].error or "")


def test_parameter_and_worklist_changes_are_refused(tmp_path):
    workflow, bundle = _seal(
        tmp_path,
        Workflow(
            name="inputs",
            steps=[],
            params={"tenant": "alpha"},
        ),
    )
    authorization = _authorization(
        workflow,
        params={"tenant": "alpha"},
        worklists={"queue": [{"patient": "A"}]},
    )
    report = Replayer(
        FakeBackend(), vision=FakeVision(), governed_authorization=authorization
    ).run(
        workflow,
        params={"tenant": "beta"},
        worklists={"queue": [{"patient": "B"}]},
        bundle_dir=bundle,
        run_dir=tmp_path / "run",
    )

    assert report.success is False
    assert "different runtime parameters or worklists" in (
        report.results[0].error or ""
    )


def test_program_exception_handler_cannot_catch_governed_identity_halt(tmp_path):
    step = context_click_step("Jane Sample 1980-01-15 MRN 123")
    step.timeout_s = 0.1
    program = ProgramGraph(
        entry="open",
        states={
            "open": State(
                id="open",
                kind=StateKind.ACTION,
                step=step,
                transitions=[Transition(target="done")],
                on_exception="recover",
            ),
            "recover": State(
                id="recover",
                kind=StateKind.ACTION,
                step=step.model_copy(
                    update={
                        "id": "recover",
                        "action": ActionKind.KEY,
                        "key": "R",
                        "anchor": None,
                    }
                ),
                transitions=[Transition(target="done")],
            ),
            "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
        },
    )
    workflow, bundle = _seal(tmp_path, Workflow(name="program_safety", program=program))
    authorization = _authorization(workflow, required=(step.id,))
    backend = FakeBackend()
    vision = resolving_vision()
    vision.ocr_lines = []

    report = Replayer(backend, vision=vision, governed_authorization=authorization).run(
        workflow, bundle_dir=bundle, run_dir=tmp_path / "run"
    )

    assert report.success is False
    assert report.terminal_outcome == "halt"
    assert "recover" not in report.visited_states
    assert backend.actions == []
    action_result = next(
        result for result in report.results if result.step_id == step.id
    )
    assert action_result.safety_halt is True
    assert action_result.exception_handled is False


def test_program_ledger_never_promotes_approved_write_to_confirmed(tmp_path):
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient": "A"},
        idempotency_key="one-run",
    )
    write = context_click_step("Jane Sample 1980-01-15 MRN 123")
    write.id = "write"
    write.effects = [effect]
    write.expect = [Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved")]
    repeated = write.model_copy(deep=True)
    repeated.id = "write-repeat"
    program = ProgramGraph(
        entry="first",
        states={
            "first": State(
                id="first",
                kind=StateKind.ACTION,
                step=write,
                transitions=[Transition(target="second")],
            ),
            "second": State(
                id="second",
                kind=StateKind.ACTION,
                step=repeated,
                transitions=[Transition(target="done")],
            ),
            "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
        },
    )
    workflow, bundle = _seal(
        tmp_path, Workflow(name="unverified_ledger", program=program)
    )
    assert workflow.manifest is not None
    approvals = tuple(
        UnverifiedWriteApproval(
            step_id=step_id, effect_contract_hashes=(effect.contract_hash(),)
        )
        for step_id in ("write", "write-repeat")
    )
    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(workflow, None, None),
        admitted_policy_name="test-governed",
        unverified_write_approvals=approvals,
    )
    backend = FakeBackend()
    vision = resolving_vision()
    vision.ocr_lines = [OcrLine("Jane Sample 1980-01-15 MRN 123")]
    vision.text_results = {
        "Saved": Match(point=(1, 1), region=(0, 0, 2, 2), confidence=0.99)
    }

    report = Replayer(
        backend,
        vision=vision,
        governed_authorization=authorization,
        durable=True,
        poll_interval_s=0.0,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run")

    assert report.success is True
    writes = [
        result
        for result in report.results
        if result.step_id in {"write", "write-repeat"}
    ]
    assert len(writes) == 2
    assert all(result.effect_verified is None for result in writes)
    assert all(result.effect_approved_unverified for result in writes)
    assert len([action for action in backend.actions if action[0] == "click"]) == 1
    checkpoints = CheckpointStore(tmp_path / "run").program_checkpoints()
    assert checkpoints[0].new_effect_keys == []
    assert checkpoints[0].new_unverified_effect_keys == [effect.contract_hash()]
    assert all(checkpoint.new_effect_keys == [] for checkpoint in checkpoints)


def test_durable_resume_restores_governed_authorization(tmp_path):
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient": "A"},
        idempotency_key="durable-run",
    )
    write = context_click_step("Jane Sample 1980-01-15 MRN 123")
    write.id = "write"
    write.effects = [effect]
    write.expect = [Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved")]
    guarded = context_click_step("Jane Sample 1980-01-15 MRN 123")
    guarded.id = "guarded"
    workflow, bundle = _seal(
        tmp_path, Workflow(name="durable_governed", steps=[write, guarded])
    )
    assert workflow.manifest is not None
    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(workflow, None, None),
        admitted_policy_name="test-governed",
        required_identity_step_ids=("guarded",),
        unverified_write_approvals=(
            UnverifiedWriteApproval(
                step_id="write", effect_contract_hashes=(effect.contract_hash(),)
            ),
        ),
    )
    run_dir = tmp_path / "run"
    first_vision = resolving_vision()
    first_vision.ocr_lines = [
        OcrLine("Jane Sample 1980-01-15 MRN 123"),
        OcrLine("Jane Sample 1980-01-15 MRN 123"),
    ]
    first_vision.ocr_results = [
        [OcrLine("Jane Sample 1980-01-15 MRN 123")],
        [],
    ]
    first_vision.text_results = {
        "Saved": Match(point=(1, 1), region=(0, 0, 2, 2), confidence=0.99)
    }
    initial_backend = FakeBackend()
    initial = Replayer(
        initial_backend,
        vision=first_vision,
        governed_authorization=authorization,
        durable=True,
        poll_interval_s=0.0,
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert initial.success is False
    store = CheckpointStore(run_dir)
    manifest = store.read_manifest()
    assert manifest is not None
    assert manifest.governed_authorization == authorization
    assert store.checkpoints()[0].effect_approved_unverified is True

    resumed_backend = FakeBackend()
    resumed_vision = resolving_vision()
    resumed_vision.ocr_lines = []
    resumed = resume(
        run_dir,
        Replayer(resumed_backend, vision=resumed_vision, poll_interval_s=0.0),
        approval=ApprovalRecord(
            approver="operator@example.com",
            resolution="continue exact governed run",
            bundle_version=bundle_version(bundle),
        ),
    )

    assert resumed.success is False
    assert resumed.governed_authorization_id == authorization.authorization_id
    assert resumed_backend.actions == []
    assert resumed.results[-1].safety_halt is True
