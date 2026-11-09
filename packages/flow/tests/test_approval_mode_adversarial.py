"""Adversarial runs against "Only ask a person about problems".

Each test tries to build a run in which an entry is saved without a matching
read-back and without a person seeing it: a run whose approval decision is
``no_person`` while something was written that the record check didn't
confirm. Every test asserts the safe result: the save never happens (stopped
before saving), or the item goes to a person.

The records system here is an in-memory table owned by the test setup. A
made-up form application (a fake backend) writes to it, and the read-back
judges it with the engine's own effect judge
(``runtime.effects._common.judge_records``), so the state before the save is
handled exactly as the engine handles it. Every read-back in this file is
done by the test setup's table, not by a deployment's verifier. All people,
patients and values are made up.

Before this file's fixes, attempts 1, 2, 3 and 5 got through end to end: the
item ended ``no_person`` with a write the read-back didn't cover. Attempt 8
got through the gate itself, and attempt 9 got past coverage and E2 (P2
happens to stop an API-only save today). Attempts 4 and 10 already reached a
person, but after the write: 4 without switching the mode off, 10 with the row
written. Attempt 7 is a synthetic report, as defense in depth. The
``test_remaining_*`` tests are strict expected failures for gaps that stay
open.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Optional

import pytest

from openadapt_flow.approval_mode.coverage import (
    save_contract_problems,
    workflow_coverage,
)
from openadapt_flow.approval_mode.gate import ApprovalGate
from openadapt_flow.approval_mode.model import ItemDecisionKind, OffReason
from openadapt_flow.approval_mode.post import check_verified
from openadapt_flow.execution_profiles import ExecutionProfile
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    IdentityCheck,
    Postcondition,
    PostconditionKind,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.run_gate import build_runtime_authorization
from openadapt_flow.runtime.effects import Effect, EffectKind, EffectState, ValueExpr
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.adapter import (
    ConnectionProbe,
    set_verifier_identity,
)
from openadapt_flow.verification import VerificationTier
from tests._approval_mode_support import (
    ENVIRONMENT_DIGEST,
    RUNNER_ID,
    USER_KEY,
    VERIFIER_IDENTITY,
    FakeCloud,
    FakeSignedInUser,
    good_item,
    make_record,
    open_log,
    status_keypair,
)
from tests.test_approval_mode_post import _save as _saved_result
from tests.test_approval_mode_post import _verified_report
from tests.test_execution_profiles import (
    _KEY,
    Replayer,
    _gate,
    _ReadyVision,
    _sealed,
)
from tests.test_replayer import FakeBackend

# -- a made-up records system and form application ------------------------------


class RecordsTable:
    """The test setup's in-memory records system (one table of dict rows)."""

    def __init__(self, rows: Optional[list[dict[str, Any]]] = None) -> None:
        self.rows: list[dict[str, Any]] = [dict(row) for row in rows or []]
        self._next = 100

    def new_id(self) -> str:
        self._next += 1
        return f"R-{self._next}"


class TableReadBack:
    """A system-of-record verifier over :class:`RecordsTable`.

    It reads the rows the application wrote and judges them with the engine's
    own ``judge_records``.
    """

    substrate = "sql"

    def __init__(self, table: RecordsTable) -> None:
        self.table = table
        self.verification_tier = VerificationTier.INDEPENDENT_SYSTEM
        self.reads = 0
        set_verifier_identity(self, VERIFIER_IDENTITY)

    def test_connection(self, context=None):
        return ConnectionProbe(ok=True, substrate=self.substrate)

    def capture_pre_state(self, context=None):
        return EffectState(
            substrate=self.substrate,
            reachable=True,
            records=[dict(row) for row in self.table.rows],
        )

    def verify(self, effect, before, context=None):
        self.reads += 1
        current = [dict(row) for row in self.table.rows]
        return judge_records(effect, before, current, substrate=self.substrate)


class FormApp(FakeBackend):
    """A made-up referral form.

    Each typed value goes into the next field of ``tab_order`` (focus moves on
    after each entry). Enter saves the form: it creates a new row, or, with
    ``update_row`` set, writes the form's fields into that existing row.

    - ``autosave``: a value typed after the save is written straight into the
      saved row, as a chart that saves each field on entry does.
    - ``draft``: the first keystroke creates the row (a draft the application
      saves as you type); Enter only marks it done.
    - ``also_on_save``: anything else the save writes (a stray edit).
    """

    def __init__(
        self,
        table: RecordsTable,
        tab_order: list[str],
        *,
        update_row: Optional[str] = None,
        autosave: bool = False,
        draft: bool = False,
        also_on_save: Optional[Callable[[RecordsTable, dict[str, Any]], None]] = None,
    ) -> None:
        super().__init__()
        self.also_on_save = also_on_save
        self.table = table
        self.tab_order = list(tab_order)
        self.update_row = update_row
        self.autosave = autosave
        self.draft = draft
        self.form: dict[str, str] = {}
        self._cursor = 0
        self.saved_row: Optional[dict[str, Any]] = None
        self.saves = 0
        self._last_typed = ""

    def type_text(self, text):
        super().type_text(text)
        self._last_typed = text
        if self._cursor >= len(self.tab_order):
            return
        name = self.tab_order[self._cursor]
        self._cursor += 1
        if self.saved_row is not None and self.autosave:
            self.saved_row[name] = text
        elif self.draft:
            if self.saved_row is None:
                self.saved_row = {"id": self.table.new_id()}
                self.table.rows.append(self.saved_row)
            self.saved_row[name] = text
        else:
            self.form[name] = text

    def press(self, key):
        super().press(key)
        if key != "Enter":
            return
        self.saves += 1
        if self.draft and self.saved_row is not None:
            self.saved_row["status"] = "done"
            return
        if self.update_row is not None:
            row = next(r for r in self.table.rows if r["id"] == self.update_row)
            row.update(self.form)
        else:
            row = {"id": self.table.new_id(), **self.form}
            self.table.rows.append(row)
        if self.also_on_save is not None:
            self.also_on_save(self.table, row)
        self.saved_row = row
        self.form = {}

    def focused_text_value(self):
        # The field just typed into reads back once (the replayer's check that
        # the keystrokes landed); then focus has moved to the next, empty field.
        value, self._last_typed = self._last_typed, ""
        return value

    def text_value_at(self, x, y):
        return None


# -- workflows -------------------------------------------------------------------


def _anchor(text: str) -> Anchor:
    return Anchor(
        template="templates/submit.png",
        region=(0, 0, 10, 10),
        click_point=(5, 5),
        ocr_text=text,
        context_text="Synthetic record",
    )


def _type(step_id: str, param: Optional[str], *, text: Optional[str] = None) -> Step:
    return Step(
        id=step_id,
        intent=f"enter the {param or 'value'}",
        action=ActionKind.TYPE,
        param=param,
        text=text,
    )


def _open_chart() -> Step:
    """Click the patient's row: an identity-checked navigation click."""

    return Step(
        id="open-chart",
        intent="open the chart",
        action=ActionKind.CLICK,
        anchor=_anchor("Rivera chart"),
        identity_armed=True,
        expect=[
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT,
                text="Profile write completed",
            )
        ],
    )


def _save(effects: list[Effect]) -> Step:
    return Step(
        id="submit",
        intent="submit",
        action=ActionKind.KEY,
        key="Enter",
        anchor=_anchor("Submit target"),
        identity_armed=True,
        effects=effects,
        expect=[
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT,
                text="Profile write completed",
            )
        ],
    )


def _new_referral(*params: str) -> Effect:
    """Exactly one new record carrying every named input."""

    return Effect(
        kind=EffectKind.EXACT_NEW_SET,
        new_records=[{name: ValueExpr(param=name) for name in params}],
        expected_count=1,
        idempotency_key="adversarial-item",
        risk="irreversible",
    )


def _workflow(name: str, steps: list[Step], **fields: Any) -> Workflow:
    return Workflow(name=name, steps=steps, **fields)


def _create_workflow() -> Workflow:
    return _workflow(
        "referral-create",
        [
            _type("enter-last", "last_name"),
            _type("enter-first", "first_name"),
            _save([_new_referral("last_name", "first_name")]),
        ],
    )


# -- one item through the real Replayer ---------------------------------------------


class Item:
    """One made-up item through the real Replayer under problems_only."""

    def __init__(self, tmp_path, workflow: Workflow, app: FormApp, verifier) -> None:
        self.tmp_path = tmp_path
        self.workflow, self.bundle = _sealed(tmp_path, workflow, encrypted=True)
        gate_result = _gate(
            self.workflow,
            self.bundle,
            ExecutionProfile.REGULATED,
            verifier=verifier,
            durable=True,
        )
        assert gate_result.passed, gate_result.render()
        self.gate_result = gate_result
        assert self.workflow.manifest is not None
        floor = {step.id: "template" for step in self.workflow.steps if step.anchor}
        self.record = make_record(
            bundle_content_digest=self.workflow.manifest.content_digest,
            rung_floor=floor,
        )
        self.log = open_log(tmp_path, self.record)
        private, keys = status_keypair()
        self.gate = ApprovalGate(
            "problems_only",
            record=self.record,
            log=self.log,
            pinned_status_keys=keys,
            fetch_status=FakeCloud(private),
            environment_digest=lambda: ENVIRONMENT_DIGEST,
            runner_id=RUNNER_ID,
            signed_in_user=FakeSignedInUser(),
            dedicated_user_key=USER_KEY,
            item=good_item(),
        )
        self.app = app
        self.verifier = verifier

    def authorization(self, params: dict[str, str]):
        return build_runtime_authorization(
            self.workflow, self.gate_result, params=params
        )

    def run(self, params: dict[str, str], name: str = "run", *, authorization=None):
        self.gate.next_item(good_item())
        replayer = Replayer(
            self.app,
            vision=_ReadyVision(),
            effect_verifier=self.verifier,
            governed_authorization=authorization or self.authorization(params),
            durable=True,
            checkpoint_key=_KEY,
            require_settled=True,
            approval_gate=self.gate,
        )
        return replayer.run(
            self.workflow,
            params=params,
            bundle_dir=self.bundle,
            run_dir=self.tmp_path / name,
        )


def _assert_goes_to_a_person(report) -> None:
    approval = report.approval
    assert approval is not None
    assert approval.decision is not ItemDecisionKind.NO_PERSON
    assert approval.needs_person


def _failed_pre_check(report) -> str:
    failed = [c for c in report.approval.pre_checks if not c.passed]
    return failed[-1].detail


# -- the baseline: a covered create is the one way through -------------------------


def test_baseline_covered_create_finishes_without_a_person(tmp_path):
    table = RecordsTable()
    app = FormApp(table, ["last_name", "first_name"])
    item = Item(tmp_path, _create_workflow(), app, TableReadBack(table))
    report = item.run({"last_name": "Rivera", "first_name": "Ana"})

    assert report.transaction_outcome == "VERIFIED", report.results[-1].error
    assert report.approval.decision is ItemDecisionKind.NO_PERSON
    assert table.rows == [{"id": "R-101", "last_name": "Rivera", "first_name": "Ana"}]


def test_baseline_swapped_fields_are_caught_by_the_read_back(tmp_path):
    # The form's tab order changed: the last name lands in the first-name box.
    table = RecordsTable()
    app = FormApp(table, ["first_name", "last_name"])
    item = Item(tmp_path, _create_workflow(), app, TableReadBack(table))
    report = item.run({"last_name": "Rivera", "first_name": "Ana"})

    assert app.saves == 1  # the wrong entry was saved, then found
    assert report.transaction_outcome != "VERIFIED"
    _assert_goes_to_a_person(report)
    assert report.approval.switch_off_reason is OffReason.UNCERTAIN_RESULT


def test_baseline_identity_checked_navigation_is_not_treated_as_a_save(tmp_path):
    # Before the fix, the gate ran P1 to P5 on the navigation click, found no
    # record check for it, stopped the item and switched the mode off with
    # record_check_lost on the first item of any workflow that opens a chart.
    table = RecordsTable()
    app = FormApp(table, ["last_name"])
    workflow = _workflow(
        "referral-from-chart",
        [
            _open_chart(),
            _type("enter-last", "last_name"),
            _save([_new_referral("last_name")]),
        ],
    )
    item = Item(tmp_path, workflow, app, TableReadBack(table))
    report = item.run({"last_name": "Rivera"})

    assert report.transaction_outcome == "VERIFIED", report.results[-1].error
    assert report.approval.decision is ItemDecisionKind.NO_PERSON
    assert report.approval.saves_without_person == 1
    assert {c.step_id for c in report.approval.pre_checks} == {"submit"}
    assert not item.log.state().is_off


def test_baseline_each_item_pauses_at_the_save_not_at_navigation(tmp_path):
    table = RecordsTable()
    app = FormApp(table, ["last_name"])
    workflow = _workflow(
        "referral-from-chart",
        [
            _open_chart(),
            _type("enter-last", "last_name"),
            _save([_new_referral("last_name")]),
        ],
    )
    item = Item(tmp_path, workflow, app, TableReadBack(table))
    item.gate = ApprovalGate("each_item", item=good_item())
    report = item.run({"last_name": "Rivera"})

    assert app.saves == 0
    assert report.approval.decision is ItemDecisionKind.APPROVAL_REQUIRED
    assert report.approval.gated_step_id == "submit"  # the filled form


# -- attempt 1: an entry typed after the last save -----------------------------------


def _trailing_entry_workflow() -> Workflow:
    return _workflow(
        "referral-with-note",
        [
            _type("enter-last", "last_name"),
            _save([_new_referral("last_name")]),
            # Typed after the save: the chart saves each field on entry, and no
            # record check names the note.
            _type("enter-note", "note"),
        ],
    )


def test_attempt_1_entry_after_the_last_save_is_refused_statically():
    coverage = workflow_coverage(_trailing_entry_workflow())
    assert [row.covered for row in coverage] == [False]
    assert coverage[0].problems == ("entry_after_last_save",)
    assert coverage[0].uncovered_inputs == ("note",)


def test_attempt_1_the_requirements_report_names_the_entry_after_the_last_save():
    from openadapt_flow.approval_mode.requirements import (
        DeploymentEvidence,
        RequirementCode,
        RequirementsEvidence,
        VerifierEvidence,
        evaluate_requirements,
    )

    evidence = RequirementsEvidence(
        deployment=DeploymentEvidence(
            policy_pack="regulated",
            minimum_effect_tier=1,
            policy_name="clinical-write",
        ),
        verifiers=(
            VerifierEvidence(
                kind="sql",
                verifier_identity=VERIFIER_IDENTITY,
                connection_ok=True,
            ),
        ),
    )
    report = evaluate_requirements(_trailing_entry_workflow(), evidence)
    e2 = next(
        r for r in report.requirements if r.code is RequirementCode.E2_RECORD_CHECK
    )
    assert not e2.met
    assert e2.reason == "entry_after_last_save"


def test_attempt_1_entry_after_the_last_save_never_saves_without_a_person(tmp_path):
    # Before the fix: VERIFIED, no_person, and the table held a note that no
    # read-back looked at.
    table = RecordsTable()
    app = FormApp(table, ["last_name", "note"], autosave=True)
    item = Item(tmp_path, _trailing_entry_workflow(), app, TableReadBack(table))
    report = item.run({"last_name": "Rivera", "note": "Wrong clinic, see fax"})

    _assert_goes_to_a_person(report)
    assert app.saves == 0
    assert table.rows == []
    assert report.transaction_outcome == "HALTED_BEFORE_EFFECT"
    assert report.approval.decision is ItemDecisionKind.STOP_BEFORE_SAVING
    assert _failed_pre_check(report) == "entry_after_last_save"


# -- attempt 2: an update whose read-back can't see the state before ----------------------


def _phone_equals() -> Effect:
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"id": ValueExpr(literal="R-7")},
        field="phone",
        value=ValueExpr(param="phone"),
        idempotency_key="adversarial-item",
        risk="irreversible",
    )


def _update_workflow() -> Workflow:
    return _workflow(
        "referral-update",
        [_type("enter-phone", "phone"), _save([_phone_equals()])],
    )


def test_attempt_2_update_typed_into_the_wrong_field_never_saves_without_a_person(
    tmp_path,
):
    # The patient's phone already holds the value. The form's layout changed
    # and the value lands in the fax field, overwriting it. field_equals reads
    # only the record after the save, so "phone equals the value" confirms.
    # Before the fix: VERIFIED, no_person, and the fax was overwritten.
    table = RecordsTable(
        [
            {
                "id": "R-7",
                "phone": "555-0142",
                "fax": "555-0199",
                "key": "adversarial-item",
            }
        ]
    )
    app = FormApp(table, ["fax"], update_row="R-7")
    item = Item(tmp_path, _update_workflow(), app, TableReadBack(table))
    report = item.run({"phone": "555-0142"})

    _assert_goes_to_a_person(report)
    assert app.saves == 0
    assert table.rows[0]["fax"] == "555-0199"
    assert _failed_pre_check(report) == "update_needs_field_delta_check"


def test_attempt_2_the_requirements_and_the_gate_apply_the_same_contract_rules():
    save = _update_workflow().steps[-1]
    assert save_contract_problems(save) == ("update_needs_field_delta_check",)
    create = _create_workflow().steps[-1]
    assert save_contract_problems(create) == ()


# -- attempt 3: "a record with this value exists" over an existing record --------------


def test_attempt_3_record_written_over_an_existing_record_never_saves_without_a_person(
    tmp_path,
):
    # A record with this name already exists. The save writes the value into
    # a misnamed field of a new record, but "exactly one record with this name
    # exists" still confirms. Before the fix: VERIFIED and no_person.
    table = RecordsTable([{"id": "R-3", "last_name": "Rivera"}])
    app = FormApp(table, ["last_nme"])
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"last_name": ValueExpr(param="last_name")},
        risk="irreversible",
    )
    workflow = _workflow(
        "referral-record-written",
        [_type("enter-last", "last_name"), _save([effect])],
    )
    item = Item(tmp_path, workflow, app, TableReadBack(table))
    report = item.run({"last_name": "Rivera"})

    _assert_goes_to_a_person(report)
    assert app.saves == 0
    assert len(table.rows) == 1
    assert _failed_pre_check(report) == "update_needs_field_delta_check"


# -- attempt 4: a draft the application saved before the save step ----------------------


def test_attempt_4_a_record_written_before_the_gate_is_caught_by_the_state_before(
    tmp_path,
):
    # The application saves a draft row on the first keystroke, before the
    # gate is asked. exact_new_set compares with the state before the save,
    # where the draft already exists, so it can't confirm.
    table = RecordsTable()
    app = FormApp(table, ["last_name", "first_name"], draft=True)
    item = Item(tmp_path, _create_workflow(), app, TableReadBack(table))
    report = item.run({"last_name": "Rivera", "first_name": "Ana"})

    assert app.saves == 1
    _assert_goes_to_a_person(report)
    # The engine may call this "Stopped before saving": the read-back found no
    # new record. The save was delivered, so the mode switches off anyway.
    assert report.approval.switch_off_reason is OffReason.UNCERTAIN_RESULT
    assert item.log.state().off_reason is OffReason.UNCERTAIN_RESULT


# -- attempt 5: a run that reuses an interrupted run's id -----------------------------


def test_attempt_5_a_reused_run_id_does_not_hide_an_interrupted_item(tmp_path):
    # An item passed the gate and was cut off before its result was logged.
    # The next run reuses the same authorization, so the same run id. Before
    # the fix, the gate took the open item for its own: the second item saved
    # without a person and closed the first one's marker.
    table = RecordsTable()
    app = FormApp(table, ["last_name", "first_name"])
    item = Item(tmp_path, _create_workflow(), app, TableReadBack(table))
    params = {"last_name": "Rivera", "first_name": "Ana"}
    authorization = item.authorization(params)
    run_ref = hashlib.sha256(authorization.authorization_id.encode()).hexdigest()
    item.log.append("item_started", run_ref=run_ref)

    report = item.run(params, authorization=authorization)

    assert app.saves == 0
    _assert_goes_to_a_person(report)
    assert _failed_pre_check(report) == "interrupted_item"
    assert item.log.state().off_reason is OffReason.UNCERTAIN_RESULT


# -- attempt 6: values that come from the recording ----------------------------------


def test_attempt_6_a_literal_typed_from_the_recording_stops_before_saving(tmp_path):
    table = RecordsTable()
    app = FormApp(table, ["last_name", "first_name"])
    workflow = _workflow(
        "referral-literal",
        [
            _type("enter-last", "last_name"),
            _type("enter-first", None, text="Ana"),
            _save([_new_referral("last_name")]),
        ],
    )
    item = Item(tmp_path, workflow, app, TableReadBack(table))
    report = item.run({"last_name": "Rivera"})

    _assert_goes_to_a_person(report)
    assert app.saves == 0
    assert _failed_pre_check(report) == "recorded_value_entry"


def test_attempt_6_a_recorded_default_in_workflow_params_is_refused(tmp_path):
    table = RecordsTable()
    app = FormApp(table, ["last_name", "first_name"])
    workflow = _create_workflow()
    workflow.params = {"first_name": "Ana"}
    item = Item(tmp_path, workflow, app, TableReadBack(table))
    report = item.run({"last_name": "Rivera"})

    assert app.actions == []
    _assert_goes_to_a_person(report)
    assert report.approval.decision_reason == "inputs_supplied"


def test_attempt_6_an_input_the_read_back_does_not_name_stops_before_saving(tmp_path):
    table = RecordsTable()
    app = FormApp(table, ["last_name", "first_name"])
    workflow = _workflow(
        "referral-partial",
        [
            _type("enter-last", "last_name"),
            _type("enter-first", "first_name"),
            _save([_new_referral("last_name")]),
        ],
    )
    item = Item(tmp_path, workflow, app, TableReadBack(table))
    report = item.run({"last_name": "Rivera", "first_name": "Ana"})

    _assert_goes_to_a_person(report)
    assert app.saves == 0
    assert _failed_pre_check(report) == "input_not_read_back"


# -- attempt 7: a save that never went through the gate ---------------------------------


def test_attempt_7_a_confirmed_save_the_gate_never_saw_is_not_checked():
    # Defense in depth, built from a synthetic report: no runtime path found
    # here delivers a save without asking the gate. If one ever does, its own
    # confirmed read-back must not stand in for the gate's coverage check.
    first = _saved_result(step_id="save")
    second = _saved_result(step_id="save-2")
    report = _verified_report(first, second)
    record = make_record(rung_floor={"save": "template", "save-2": "template"})

    passed = check_verified(
        report, record, saves_without_person=1, gated_results=[first]
    )
    assert not passed.passed
    assert passed.detail == "save_not_gated"
    both = check_verified(
        report, record, saves_without_person=2, gated_results=[first, second]
    )
    assert both.passed, both


# -- attempt 8: a different save inheriting an earlier save's pass ------------------------


def test_attempt_8_a_result_for_another_step_never_inherits_an_earlier_pass(tmp_path):
    # The gate remembers a passed save by its result object, so a later input
    # edge of the same save skips the full checks. A context for a different
    # step must run every check again, even if it carries that same object
    # (or a new object at a reused id). Before the fix it was waved through on
    # the switch log and the screen check alone.
    from tests.test_approval_mode_gate import Harness

    h = Harness(tmp_path)
    h.begin()
    shared = StepResult(
        step_id="save",
        intent="save",
        ok=True,
        identity=IdentityCheck(status="verified"),
    )
    first = h.before_save(current=shared)
    assert first.proceed, first

    other = Step(
        id="save-2",
        intent="save the second form",
        action=ActionKind.CLICK,
        risk="irreversible",
        anchor=_anchor("Save"),
        identity_armed=True,
    )
    verdict = h.gate.before_save(
        h.save_context(current=shared, step=other, frame=lambda: None)
    )
    assert not verdict.proceed
    assert {c.code for c in verdict.checks} >= {"switch_on", "signed_in_user"}


# -- attempt 9: values sent by an API binding -------------------------------------------


class TableApi:
    """A made-up records API: a POST adds the filled body as a new row."""

    def __init__(self, table: RecordsTable) -> None:
        self.table = table
        self.requests = 0

    def actuate(self, binding, params):
        from openadapt_flow.runtime.actuators.api import (
            ActuationStatus,
            ApiActuationResult,
            _fill_body,
        )

        self.requests += 1
        body = _fill_body(binding.body_template, dict(params))
        self.table.rows.append({"id": self.table.new_id(), **body})
        return ApiActuationResult(
            status=ActuationStatus.ACTUATED,
            substrate="rest",
            http_status=201,
            request_summary="POST /referrals",
        )


def _api_save() -> Step:
    from openadapt_flow.ir import ApiBinding, ApiIdentityBinding

    step = _save([])
    step.expect = []
    step.anchor = None
    step.identity_armed = False
    step.api_binding = ApiBinding(
        kind="rest",
        on_unavailable="halt",
        method="POST",
        url_template="/referrals",
        body_template={
            "record_id": "{record_id}",
            "last_name": "{last_name}",
            "note": "{note}",
        },
        expected_status=[201],
        effects=[
            _new_referral("record_id", "last_name").model_copy(
                update={"match": {"record_id": ValueExpr(param="record_id")}}
            )
        ],
        identity=[
            ApiIdentityBinding(
                key="record_id",
                param="record_id",
                effect_field="record_id",
                request_pointers=["/body/record_id"],
            )
        ],
    )
    return step


def test_attempt_9_an_api_body_value_the_read_back_does_not_name_is_refused():
    workflow = _workflow("referral-api", [_api_save()])
    coverage = workflow_coverage(workflow)
    assert coverage[0].problems == ("input_not_read_back",)
    assert coverage[0].uncovered_inputs == ("note",)


def test_attempt_9_the_gate_refuses_an_api_body_value_it_cannot_read_back(tmp_path):
    # Before the fix, coverage counted only typed values, so this save passed
    # P5 and the requirements' E2.
    from tests.test_approval_mode_gate import Harness, _result

    h = Harness(tmp_path)
    h.begin()
    save = _api_save()
    workflow = _workflow("referral-api", [save])
    verdict = h.before_save(step=save, workflow=workflow, current=_result("save"))
    assert not verdict.proceed
    assert verdict.checks[-1].code == "record_check_ready"
    assert verdict.checks[-1].detail == "input_not_read_back"


def test_attempt_9_an_api_only_save_never_saves_without_a_person(tmp_path):
    # End to end, an API-only save also stops at P2: it has no screen frame on
    # which to see the signed-in user, so it always goes to a person today.
    table = RecordsTable()
    api = TableApi(table)
    workflow = _workflow("referral-api", [_api_save()])
    item = Item(tmp_path, workflow, FormApp(table, []), TableReadBack(table))
    params = {
        "record_id": "M-4821",
        "last_name": "Rivera",
        "note": "Wrong clinic, see fax",
    }
    item.gate.next_item(good_item())
    replayer = Replayer(
        item.app,
        vision=_ReadyVision(),
        effect_verifier=item.verifier,
        api_actuator=api,
        governed_authorization=item.authorization(params),
        durable=True,
        checkpoint_key=_KEY,
        require_settled=True,
        approval_gate=item.gate,
    )
    report = replayer.run(
        item.workflow, params=params, bundle_dir=item.bundle, run_dir=tmp_path / "run"
    )

    _assert_goes_to_a_person(report)
    assert api.requests == 0
    assert table.rows == []
    assert report.approval.decision is ItemDecisionKind.STOP_BEFORE_SAVING


# -- attempt 10: a save that can be skipped -------------------------------------------


def _skippable_save_workflow() -> Workflow:
    from openadapt_flow.ir import Guard, Predicate, PredicateKind

    first = _save([_new_referral("last_name")])
    first.id = "submit-name"
    first.guard = Guard(
        predicate=Predicate(kind=PredicateKind.TEXT_PRESENT, text="Name section"),
        on_unmet="skip",
    )
    second = _save([_new_referral("first_name")])
    return _workflow(
        "referral-skippable-save",
        [
            _type("enter-last", "last_name"),
            first,
            _type("enter-first", "first_name"),
            second,
        ],
    )


def test_attempt_10_a_skippable_save_is_refused_statically():
    rows = workflow_coverage(_skippable_save_workflow())
    assert all(not row.covered for row in rows)
    assert all("save_can_be_skipped" in row.problems for row in rows)


def test_attempt_10_a_skipped_save_never_hides_an_entry(tmp_path):
    # The first save is skipped (its section isn't on screen), so the last
    # name stays in the form and the second save writes it. The second save's
    # record check names only the first name. Before the fix this was already
    # caught after the save: the engine doesn't call a run with a skipped save
    # VERIFIED, so it ended "Check the record" with the row written. Now it
    # stops before the save.
    table = RecordsTable()
    app = FormApp(table, ["last_name", "first_name"])
    item = Item(tmp_path, _skippable_save_workflow(), app, TableReadBack(table))
    report = item.run({"last_name": "Rivera", "first_name": "Ana"})

    _assert_goes_to_a_person(report)
    assert app.saves == 0
    assert table.rows == []
    assert _failed_pre_check(report) == "save_can_be_skipped"


# -- what remains: writes the record check doesn't name ------------------------------
#
# These pass on purpose today and are marked xfail(strict=True): they show the
# gaps that the design's field-delta check and whole-item snapshot are meant
# to close. When either is built, the test starts passing and strict xfail
# turns that into a failure, so the marker has to be removed deliberately.


@pytest.mark.xfail(
    strict=True,
    reason="exact_new_set checks only the fields its selectors name; the "
    "field-delta check (design E2) isn't built",
)
def test_remaining_another_field_on_the_new_record_is_not_read_back(tmp_path):
    # The save also sets a field no input reaches (a recorded choice that
    # landed on the wrong option). No selector names it.
    def wrong_choice(table: RecordsTable, row: dict[str, Any]) -> None:
        row["priority"] = "routine"  # the clinic's list said "urgent"

    table = RecordsTable()
    app = FormApp(table, ["last_name", "first_name"], also_on_save=wrong_choice)
    item = Item(tmp_path, _create_workflow(), app, TableReadBack(table))
    report = item.run({"last_name": "Rivera", "first_name": "Ana"})

    _assert_goes_to_a_person(report)


@pytest.mark.xfail(
    strict=True,
    reason="a change to an existing record isn't seen by exact_new_set or "
    "forbid_collateral_loss; the whole-item snapshot (design E4) isn't built",
)
def test_remaining_a_stray_edit_to_another_record_is_not_read_back(tmp_path):
    def stray_edit(table: RecordsTable, row: dict[str, Any]) -> None:
        other = next(r for r in table.rows if r["id"] == "R-7")
        other["fax"] = "555-0100"

    table = RecordsTable([{"id": "R-7", "last_name": "Okafor", "fax": "555-0199"}])
    app = FormApp(table, ["last_name", "first_name"], also_on_save=stray_edit)
    item = Item(tmp_path, _create_workflow(), app, TableReadBack(table))
    report = item.run({"last_name": "Rivera", "first_name": "Ana"})

    _assert_goes_to_a_person(report)


@pytest.mark.xfail(
    strict=True,
    reason="coverage binds inputs, not fields: an input typed into two fields "
    "is read back from one of them",
)
def test_remaining_an_input_typed_twice_is_read_back_from_one_field_only(tmp_path):
    # The workflow types the last name into the name box and, on a second
    # step, into a box whose position changed: it lands in "allergies".
    table = RecordsTable()
    app = FormApp(table, ["last_name", "allergies"])
    workflow = _workflow(
        "referral-typed-twice",
        [
            _type("enter-last", "last_name"),
            _type("enter-preferred", "last_name"),
            _save([_new_referral("last_name")]),
        ],
    )
    item = Item(tmp_path, workflow, app, TableReadBack(table))
    report = item.run({"last_name": "Rivera"})

    _assert_goes_to_a_person(report)
