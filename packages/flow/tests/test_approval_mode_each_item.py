"""Per-item approval (``each_item``) end to end through the real Replayer.

Phase 0 of the approval design: a person approves every save. The run waits
before the save step resolves, shows the filled form, and saves only after a
person's approval from Desktop's Run it. After the approval the step takes a
fresh frame and resolves its target and right-record check again, and the
last-point gate re-reads the form: a field that changed while waiting stops
the item before the save.

The backend and vision are fakes; the system of record is a scripted
verifier. ``Person`` plays the person in Desktop's Run it by answering the
run's request files the way ``openadapt-flow item-approval`` does. All values
are made up.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from openadapt_flow.approval_mode.gate import ApprovalGate, SaveContext, StepContext
from openadapt_flow.approval_mode.item_approval import (
    ItemApprovalStore,
    make_answer,
)
from openadapt_flow.approval_mode.model import ItemDecisionKind
from openadapt_flow.execution_profiles import ExecutionProfile
from openadapt_flow.ir import ActionKind, Step, StepResult, Workflow
from openadapt_flow.run_gate import build_runtime_authorization
from openadapt_flow.runtime.durable import resume
from openadapt_flow.runtime.durable.approval import ApprovalRequired
from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr
from tests.test_approval_mode_replayer import RecordSystem, _create_workflow
from tests.test_execution_profiles import (
    _KEY,
    Replayer,
    _gate,
    _ReadyVision,
    _sealed,
)
from tests.test_replayer import FakeBackend

APPROVER = "cloud-user-0001"
APPROVE: dict[str, Any] = dict(
    decision="approve",
    decided_by="human",
    route="desktop_run_it",
    approver_id=APPROVER,
    auth_method="cloud_aal2",
)


class FormBackend(FakeBackend):
    """A fake application whose form fields the engine can re-read."""

    def __init__(self) -> None:
        super().__init__()
        self.fields = {"last_name": "Testperson", "first_name": "Ana"}

    def form_state(self) -> dict[str, str]:
        return dict(self.fields)


class Person:
    """Answers each waiting request with the next scripted decision.

    ``None`` means nobody answers that request. ``on_wait`` runs while the
    engine waits, before the person looks, to change the application.
    """

    def __init__(self, decisions: list[Optional[dict[str, Any]]]) -> None:
        self.decisions = list(decisions)
        self.clock = 0.0
        self.store: Optional[ItemApprovalStore] = None
        self.on_wait = None
        self.seen: list[str] = []

    def monotonic(self) -> float:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.clock += seconds
        if self.on_wait is not None:
            self.on_wait()
        assert self.store is not None
        for request in self.store.requests():
            if request.request_id in self.seen:
                continue
            if self.store.status(request) != "waiting":
                continue
            self.seen.append(request.request_id)
            decision = self.decisions.pop(0) if self.decisions else None
            if decision is None:
                continue
            self.store.write_answer(make_answer(request, **decision))


def _save_delivered(backend: FakeBackend) -> bool:
    return any(action[0] in {"press", "key"} for action in backend.actions)


class Item:
    """One made-up item under ``each_item`` (or another mode)."""

    def __init__(
        self,
        tmp_path,
        *,
        person: Optional[Person] = None,
        mode: str = "each_item",
        workflow: Optional[Workflow] = None,
        wait_s: float = 30.0,
        backend: Optional[FakeBackend] = None,
        authorization_mode: Optional[str] = None,
        store: bool = True,
        vision: Any = None,
    ) -> None:
        self.vision = vision
        self.tmp_path = tmp_path
        self.workflow, self.bundle = _sealed(
            tmp_path, workflow or _create_workflow(), encrypted=True
        )
        self.verifier = RecordSystem()
        gate_result = _gate(
            self.workflow,
            self.bundle,
            ExecutionProfile.REGULATED,
            verifier=self.verifier,
            durable=True,
        )
        assert gate_result.passed, gate_result.render()
        self.authorization = build_runtime_authorization(self.workflow, gate_result)
        if authorization_mode is not None:
            self.authorization = self.authorization.model_copy(
                update={"approval_mode": authorization_mode}
            )
        self.run_dir = tmp_path / "run"
        self.person = person or Person([])
        self.store = ItemApprovalStore(self.run_dir) if store else None
        self.person.store = ItemApprovalStore(self.run_dir)
        self.mode = mode
        self.wait_s = wait_s
        self.backend = backend or FormBackend()

    def gate(self) -> ApprovalGate:
        return ApprovalGate(
            self.mode,
            approval_store=self.store,
            approval_wait_s=self.wait_s,
            approval_poll_s=1.0,
            sleep=self.person.sleep,
            monotonic=self.person.monotonic,
        )

    def replayer(self, *, gate: Any = "default", backend=None) -> Replayer:
        return Replayer(
            backend or self.backend,
            vision=self.vision or _ReadyVision(),
            effect_verifier=self.verifier,
            governed_authorization=self.authorization,
            durable=True,
            checkpoint_key=_KEY,
            require_settled=True,
            approval_gate=self.gate() if gate == "default" else gate,
        )

    def run(self, **kwargs):
        return self.replayer(**kwargs).run(
            self.workflow, bundle_dir=self.bundle, run_dir=self.run_dir
        )

    def pending(self):
        return CheckpointStore(self.run_dir, key=_KEY).read_pending()


# -- the person approves --------------------------------------------------------------


def test_the_run_waits_and_saves_only_after_a_persons_approval(tmp_path):
    person = Person([APPROVE])
    item = Item(tmp_path, person=person)
    report = item.run()

    assert _save_delivered(item.backend)
    assert report.transaction_outcome == "VERIFIED"
    approval = report.approval
    assert approval is not None
    assert approval.decision is ItemDecisionKind.APPROVED
    assert approval.needs_person is False
    assert approval.saves_without_person == 0
    assert [row.decision for row in approval.approvals] == ["approved"]
    row = approval.approvals[0]
    assert (row.decided_by, row.route, row.approver_id) == (
        "human",
        "desktop_run_it",
        APPROVER,
    )
    assert any(
        check.code == "each_item" and check.detail == "approved_by_person"
        for check in approval.pre_checks
    )
    # The person saw a request, and the run used the approval exactly once.
    requests = item.store.requests()
    assert len(requests) == 1
    assert item.store.status(requests[0]) == "used"
    assert requests[0].step_id == "submit"
    assert (item.run_dir / requests[0].screenshot).is_file()


def test_the_report_keeps_codes_and_digests_not_values_or_form_state(tmp_path):
    item = Item(tmp_path, person=Person([APPROVE]))
    item.run()
    saved = json.loads((item.run_dir / "report.json").read_text())
    block = json.dumps(saved["approval"])
    request = item.store.requests()[0]
    assert request.form_state_sha256 not in block
    assert request.screenshot_sha256 not in block
    assert "Testperson" not in block
    assert saved["approval"]["decision"] == "approved"


# -- the person says "Don't save" ------------------------------------------------------


def test_dont_save_stops_before_saving_and_ends_the_item(tmp_path):
    decline = dict(APPROVE, decision="decline", decline_reason="wrong_record")
    item = Item(tmp_path, person=Person([decline]))
    report = item.run()

    assert not _save_delivered(item.backend)
    assert item.verifier.reads == 0
    assert report.transaction_outcome == "HALTED_BEFORE_EFFECT"
    approval = report.approval
    assert approval.decision is ItemDecisionKind.DECLINED
    assert approval.decision_reason == "wrong_record"
    assert approval.approvals[-1].decline_reason == "wrong_record"
    pending = item.pending()
    assert pending is not None
    assert pending.category == "item_declined"
    assert pending.status == "rejected"

    # A declined item can never be resumed into a save.
    from openadapt_flow.runtime.durable.approval import ResumeRefused

    with pytest.raises(ResumeRefused):
        resume(item.run_dir, item.replayer(backend=FormBackend()), key=_KEY)


# -- nobody answers in time --------------------------------------------------------------


def test_no_answer_in_time_stops_before_the_save_and_a_later_approval_resumes(
    tmp_path, monkeypatch
):
    from openadapt_flow.__main__ import _answer_durable_item_pause

    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    item = Item(tmp_path, person=Person([None]), wait_s=5)
    report = item.run()

    assert not _save_delivered(item.backend)
    assert report.transaction_outcome == "HALTED_BEFORE_EFFECT"
    assert report.approval.decision is ItemDecisionKind.APPROVAL_REQUIRED
    assert report.approval.needs_person
    pending = item.pending()
    assert pending is not None and pending.category == "item_approval"
    assert pending.status == "pending"

    # Later, the person approves the request in Desktop's Run it.
    request = item.store.requests()[0]
    answer = make_answer(request, **APPROVE)
    item.store.write_answer(answer)
    assert _answer_durable_item_pause(item.run_dir, request, answer) is True
    assert item.pending().status == "approved"

    resumed_backend = FormBackend()
    item.person = Person([])  # nobody needs to be asked again
    item.person.store = ItemApprovalStore(item.run_dir)
    resumed = resume(item.run_dir, item.replayer(backend=resumed_backend), key=_KEY)

    assert _save_delivered(resumed_backend)
    assert resumed.transaction_outcome == "VERIFIED"
    assert resumed.approval.decision is ItemDecisionKind.APPROVED
    assert [row.answer_sha256 for row in resumed.approval.approvals] == [
        answer.digest()
    ]
    assert item.store.status(request) == "used"
    assert len(item.store.requests()) == 1


def test_an_approval_that_raced_the_timeout_is_carried_to_the_pause_by_resume(
    tmp_path, monkeypatch
):
    """The person approved while the run was giving up waiting.

    ``item-approval approve`` found no pause yet, so it couldn't answer it
    (``resume_required`` false). ``resume`` carries that approval to the
    pause itself, then makes the save exactly once.
    """

    from openadapt_flow.__main__ import _carry_item_approval_to_pause

    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    item = Item(tmp_path, person=Person([None]), wait_s=5)
    item.run()
    assert item.pending().status == "pending"
    request = item.store.requests()[0]
    answer = make_answer(request, **APPROVE)
    item.store.write_answer(answer)

    store = CheckpointStore(item.run_dir, key=_KEY)
    carried = _carry_item_approval_to_pause(item.run_dir, store, store.read_pending())
    assert carried.status == "approved"

    resumed_backend = FormBackend()
    item.person = Person([])
    item.person.store = ItemApprovalStore(item.run_dir)
    resumed = resume(item.run_dir, item.replayer(backend=resumed_backend), key=_KEY)
    assert _save_delivered(resumed_backend)
    assert resumed.transaction_outcome == "VERIFIED"
    assert item.store.status(request) == "used"


@pytest.mark.parametrize("answer_kind", ["none", "decline", "not_a_person"])
def test_resume_carries_only_a_persons_approval(tmp_path, monkeypatch, answer_kind):
    from openadapt_flow.__main__ import _carry_item_approval_to_pause
    from openadapt_flow.approval_mode.item_approval import ItemApprovalAnswer

    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    item = Item(tmp_path, person=Person([None]), wait_s=5)
    item.run()
    request = item.store.requests()[0]
    if answer_kind == "decline":
        item.store.write_answer(
            make_answer(
                request,
                decision="decline",
                decided_by="human",
                route="desktop_run_it",
                approver_id=APPROVER,
                auth_method="cloud_aal2",
                decline_reason="wrong_values",
            )
        )
    elif answer_kind == "not_a_person":
        # Written around the command, so nothing checked it on the way in.
        good = make_answer(request, **APPROVE)
        item.store.write_answer(
            ItemApprovalAnswer.model_validate(
                {**good.model_dump(mode="json"), "decided_by": "automation"}
            )
        )
    store = CheckpointStore(item.run_dir, key=_KEY)
    carried = _carry_item_approval_to_pause(item.run_dir, store, store.read_pending())
    assert carried.status == "pending"
    backend = FormBackend()
    with pytest.raises(ApprovalRequired):
        resume(item.run_dir, item.replayer(backend=backend), key=_KEY)
    assert not _save_delivered(backend)


# -- what the run refuses on its own --------------------------------------------------------


def test_a_field_changed_while_waiting_stops_before_the_save(tmp_path):
    person = Person([APPROVE])
    item = Item(tmp_path, person=person)

    def someone_edits_the_form():
        # The request was written with the form as it was; someone edits a
        # field while the person is looking at the screenshot.
        if item.store.requests():
            item.backend.fields["last_name"] = "Testperzon"

    person.on_wait = someone_edits_the_form
    report = item.run()

    assert not _save_delivered(item.backend)
    assert report.transaction_outcome == "HALTED_BEFORE_EFFECT"
    approval = report.approval
    assert approval.decision is ItemDecisionKind.STOP_BEFORE_SAVING
    failed = [check for check in approval.pre_checks if not check.passed]
    assert failed[-1].detail == "form_changed_after_approval"


class _BlankScreenVision(_ReadyVision):
    def ocr(self, screen_png, *, region=None):
        return []


def test_without_form_controls_the_screen_text_is_the_form(tmp_path):
    item = Item(tmp_path, person=Person([APPROVE]), backend=FakeBackend())
    report = item.run()
    assert _save_delivered(item.backend)
    assert report.approval.decision is ItemDecisionKind.APPROVED
    assert item.store.requests()[0].form_state_kind == "screen_text"


def test_a_form_the_engine_cant_read_never_asks_and_never_saves(tmp_path):
    item = Item(
        tmp_path,
        person=Person([APPROVE]),
        backend=FakeBackend(),
        vision=_BlankScreenVision(),
    )
    report = item.run()

    assert not _save_delivered(item.backend)
    assert item.store.requests() == []
    assert report.approval.decision is ItemDecisionKind.APPROVAL_REQUIRED
    failed = [check for check in report.approval.pre_checks if not check.passed]
    assert failed[-1].detail == "form_state_unreadable"


@pytest.mark.parametrize(
    ("tamper", "code"),
    [
        ({"decided_by": "automation"}, "not_decided_by_a_person"),
        ({"route": "phone_portal"}, "route_not_allowed"),
        ({"request_sha256": "sha256:" + "0" * 64}, "request_digest_mismatch"),
    ],
)
def test_an_answer_written_around_the_cli_is_still_checked_by_the_run(
    tmp_path, tamper, code
):
    person = Person([])
    item = Item(tmp_path, person=person)

    def forge():
        for request in item.store.requests():
            if item.store.status(request) == "waiting":
                payload = make_answer(request, **APPROVE).model_dump(mode="json")
                payload.update(tamper)
                path = item.store.dir / f"answer-{request.request_id}.json"
                path.write_text(json.dumps(payload))

    person.on_wait = forge
    report = item.run()

    assert not _save_delivered(item.backend)
    assert report.approval.decision is ItemDecisionKind.APPROVAL_REQUIRED
    failed = [check for check in report.approval.pre_checks if not check.passed]
    assert failed[-1].detail == code


def test_one_approval_covers_one_save_only(tmp_path):
    workflow = _create_workflow("two-saves")
    second = workflow.steps[0].model_copy(deep=True)
    second.id = "submit_again"
    second.intent = "submit again"
    second.effects = [
        Effect(
            kind=EffectKind.EXACT_NEW_SET,
            new_records=[{"record_id": ValueExpr(literal="synthetic-2")}],
            idempotency_key="profile-test-run-2",
            risk="irreversible",
        )
    ]
    workflow.steps.append(second)
    item = Item(tmp_path, person=Person([APPROVE, None]), workflow=workflow, wait_s=5)
    report = item.run()

    presses = [action for action in item.backend.actions if action[0] == "press"]
    assert len(presses) == 1, "only the approved save was delivered"
    approval = report.approval
    assert approval.decision is ItemDecisionKind.APPROVAL_REQUIRED
    assert approval.gated_step_id == "submit_again"
    assert [row.step_id for row in approval.approvals] == ["submit"]
    assert report.transaction_outcome != "VERIFIED"


def test_the_authorization_names_the_mode_and_a_different_gate_is_refused(tmp_path):
    item = Item(tmp_path, mode="not_gated", authorization_mode="each_item")
    report = item.run()
    assert item.backend.actions == []
    assert report.transaction_outcome == "REJECTED_POLICY"
    assert "requires the each_item approval mode" in (report.results[-1].error or "")

    bare = Item(tmp_path / "bare", authorization_mode="each_item")
    report = bare.run(gate=None)
    assert bare.backend.actions == []
    assert report.transaction_outcome == "REJECTED_POLICY"


def test_without_an_approval_store_each_item_never_saves(tmp_path):
    item = Item(tmp_path, person=Person([APPROVE]), store=False)
    report = item.run()
    assert not _save_delivered(item.backend)
    assert report.approval.decision is ItemDecisionKind.APPROVAL_REQUIRED


def test_not_gated_asks_nobody_and_says_so(tmp_path):
    item = Item(tmp_path, mode="not_gated")
    report = item.run()
    assert _save_delivered(item.backend)
    assert item.store.requests() == []
    assert report.approval.decision is ItemDecisionKind.NOT_GATED


def test_problems_only_without_a_switch_asks_a_person_and_never_counts_no_person(
    tmp_path,
):
    item = Item(tmp_path, mode="problems_only", person=Person([APPROVE]))
    report = item.run()

    assert _save_delivered(item.backend)
    assert report.transaction_outcome == "VERIFIED"
    approval = report.approval
    assert approval.decision is ItemDecisionKind.APPROVED
    assert approval.saves_without_person == 0
    assert not approval.counts_toward_problems


# -- what nothing but Desktop's Run it can answer ---------------------------------------


def test_continue_and_a_generic_approve_cant_answer_an_item_approval(
    tmp_path, monkeypatch
):
    import openadapt_flow.__main__ as cli
    from openadapt_flow.runtime.durable.attended import (
        AttendedActionRefused,
        AttendedActionRequest,
        execute_attended_action,
    )

    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    item = Item(tmp_path, person=Person([None]), wait_s=1)
    item.run()
    pending = item.pending()
    assert pending.category == "item_approval"

    with pytest.raises(AttendedActionRefused):
        execute_attended_action(
            item.run_dir,
            AttendedActionRequest(
                capability_digest="sha256:" + "0" * 64,
                idempotency_key="continue-request-0001",
                action="continue",
            ),
            operator="operator@example.test",
            decided_by="human",
            key=_KEY,
        )
    assert cli.main(["approve", str(item.run_dir)]) == 1
    assert item.pending().status == "pending"
    # Resume without the person's approval refuses before anything runs.
    assert cli.main(["resume", str(item.run_dir)]) == 3


# -- the gate itself ----------------------------------------------------------------------


def _bare_step(step_id: str = "save") -> Step:
    return Step(
        id=step_id,
        intent="Click 'Save'",
        action=ActionKind.KEY,
        key="Enter",
        risk="irreversible",
    )


def test_a_drill_fault_step_never_asks_and_still_cant_save(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    gate = ApprovalGate("each_item", approval_store=store, approval_wait_s=0)
    gate.begin_item(run_id="run-1", bundle_content_digest="a" * 64, profile="standard")
    step = _bare_step()
    workflow = Workflow(name="drill", steps=[step])
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)
    verdict = gate.before_step(
        StepContext(
            step=step,
            workflow=workflow,
            result=result,
            step_index=0,
            run_id="run-1",
            frame=lambda: b"frame",
            form_state=lambda _frame: ("dom", "sha256:" + "2" * 64),
            drill_fault_step=True,
            qualification_case_kind="wrong_identity",
        )
    )
    assert verdict.proceed
    assert store.requests() == []
    edge = gate.before_save(
        SaveContext(
            step=step,
            workflow=workflow,
            result=result,
            prior_results=[],
            effect_verifier=None,
            governed_authorization=None,
            model_calls=0,
            frame=lambda: b"frame",
        )
    )
    assert not edge.proceed


def test_an_approval_never_carries_over_to_another_execution_of_the_step(tmp_path):
    store = ItemApprovalStore(tmp_path / "run")
    person = Person([APPROVE])
    person.store = store
    gate = ApprovalGate(
        "each_item",
        approval_store=store,
        approval_wait_s=5,
        approval_poll_s=1,
        sleep=person.sleep,
        monotonic=person.monotonic,
    )
    gate.begin_item(run_id="run-1", bundle_content_digest="a" * 64, profile="standard")
    step = _bare_step()
    workflow = Workflow(name="loop", steps=[step])
    form = ("dom", "sha256:" + "3" * 64)

    def contexts(result):
        return (
            StepContext(
                step=step,
                workflow=workflow,
                result=result,
                step_index=0,
                run_id="run-1",
                frame=lambda: b"frame",
                form_state=lambda _frame: form,
            ),
            SaveContext(
                step=step,
                workflow=workflow,
                result=result,
                prior_results=[],
                effect_verifier=None,
                governed_authorization=None,
                model_calls=0,
                frame=lambda: b"frame",
                form_state=lambda _frame: form,
            ),
        )

    first = StepResult(step_id=step.id, intent=step.intent, ok=False)
    start, edge = contexts(first)
    assert gate.before_step(start).proceed
    assert gate.before_save(edge).proceed

    # The same step again (a loop row): a new result, so a new approval.
    again = StepResult(step_id=step.id, intent=step.intent, ok=False)
    start, edge = contexts(again)
    assert not gate.before_step(start).proceed  # nobody answers this time
    assert not gate.before_save(edge).proceed
    assert len(store.requests()) == 2
