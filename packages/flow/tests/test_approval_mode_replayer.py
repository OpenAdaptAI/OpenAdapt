"""End-to-end approval modes through the real Replayer.

The backend and vision are fakes, and the system of record is a scripted
verifier, as in the execution-profile suite. Each test runs one made-up item
under the Regulated profile with a sealed, encrypted bundle and checks three
things about it: whether the save input was delivered (``backend.actions``),
the engine's own outcome words, and the approval block on the report.

The safety properties these pin:

- under "Only ask a person about problems" no save is counted as done
  without a person unless the deployment's read-back confirmed it;
- every mismatch, uncertain outcome or failed check sends the item to a
  person, and a failed pre-save check stops before the save is delivered;
- every fallback is per-item approval or "stop before saving", never a
  silent save;
- the outcome words are the engine's own and are never changed.
"""

from __future__ import annotations

import json

import pytest

from openadapt_flow.approval_mode.gate import ApprovalGate
from openadapt_flow.approval_mode.model import ItemDecisionKind, OffReason
from openadapt_flow.approval_mode.switch import turn_off
from openadapt_flow.execution_profiles import ExecutionProfile
from openadapt_flow.ir import ParamSpec
from openadapt_flow.run_gate import build_runtime_authorization
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    ValueExpr,
    Verdict,
)
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
from tests.test_execution_profiles import (
    _KEY,
    Replayer,
    _gate,
    _key_workflow,
    _ReadyVision,
    _sealed,
)
from tests.test_replayer import FakeBackend


class RecordSystem:
    """A scripted system-of-record verifier at INDEPENDENT_SYSTEM.

    ``answer`` is what the read-back finds after the save: ``match`` (the
    record shows the values it was given), ``mismatch`` (a record was written
    with other values) or ``unreachable``.
    """

    substrate = "rest"

    def __init__(
        self,
        *,
        answer: str = "match",
        tier: VerificationTier = VerificationTier.INDEPENDENT_SYSTEM,
        connection_ok: bool = True,
    ) -> None:
        self.answer = answer
        self.verification_tier = tier
        self.connection_ok = connection_ok
        self.reads = 0
        set_verifier_identity(self, VERIFIER_IDENTITY)

    def test_connection(self, context=None):
        return ConnectionProbe(ok=self.connection_ok, substrate=self.substrate)

    def capture_pre_state(self, context=None):
        return EffectState(substrate=self.substrate, reachable=True, records=[])

    def verify(self, effect, before, context=None):
        self.reads += 1
        if self.answer == "match":
            return EffectVerdict(
                verdict=Verdict.CONFIRMED, kind=effect.kind, substrate=self.substrate
            )
        if self.answer == "mismatch":
            return EffectVerdict(
                verdict=Verdict.REFUTED,
                kind=effect.kind,
                substrate=self.substrate,
                reason="the record shows other values",
                observed_count=1,
                expected_count=1,
            )
        return EffectVerdict(
            verdict=Verdict.INDETERMINATE,
            kind=effect.kind,
            substrate=self.substrate,
            unavailable=True,
        )


def _create_workflow(name: str = "referral-item"):
    """The one-save item workflow, with a create effect.

    Only a create, whose added record set is compared with the state before
    the save, can finish without a person until the field-delta check exists.
    """

    workflow = _key_workflow(name, with_effect=True)
    workflow.steps[0].effects = [
        Effect(
            kind=EffectKind.EXACT_NEW_SET,
            new_records=[{"record_id": ValueExpr(literal="synthetic-1")}],
            idempotency_key="profile-test-run",
            risk="irreversible",
        )
    ]
    return workflow


def _save_delivered(backend: FakeBackend) -> bool:
    return any(action[0] in {"press", "key"} for action in backend.actions)


class Item:
    """One made-up item run under an approval mode."""

    def __init__(
        self,
        tmp_path,
        *,
        mode="problems_only",
        verifier=None,
        workflow=None,
        params=None,
        **gate_overrides,
    ):
        self.tmp_path = tmp_path
        workflow = workflow or _create_workflow()
        self.workflow, self.bundle = _sealed(tmp_path, workflow, encrypted=True)
        gate_result = _gate(
            self.workflow,
            self.bundle,
            ExecutionProfile.REGULATED,
            verifier=verifier or RecordSystem(),
            durable=True,
        )
        assert gate_result.passed, gate_result.render()
        self.gate_result = gate_result
        assert self.workflow.manifest is not None
        self.record = make_record(
            bundle_content_digest=self.workflow.manifest.content_digest,
            rung_floor={"submit": "template"},
        )
        self.log = open_log(tmp_path, self.record)
        private, keys = status_keypair()
        self.cloud = FakeCloud(private)
        self.user = FakeSignedInUser()
        self.verifier = verifier or RecordSystem()
        self.params = params
        gate_kwargs = {
            "record": self.record,
            "log": self.log,
            "pinned_status_keys": keys,
            "fetch_status": self.cloud,
            "environment_digest": lambda: ENVIRONMENT_DIGEST,
            "runner_id": RUNNER_ID,
            "signed_in_user": self.user,
            "dedicated_user_key": USER_KEY,
            "item": good_item(),
        }
        gate_kwargs.update(gate_overrides)
        self.item_context = gate_kwargs["item"]
        self.gate = ApprovalGate(mode, **gate_kwargs)
        self.backend = FakeBackend()

    def run(self, name: str = "run", *, gate=True):
        # Each item brings its own list context, as Desktop's queue would.
        self.gate.next_item(self.item_context)
        replayer = Replayer(
            self.backend,
            vision=_ReadyVision(),
            effect_verifier=self.verifier,
            # Authorizations are single-use: each item run gets its own.
            governed_authorization=build_runtime_authorization(
                self.workflow, self.gate_result
            ),
            durable=True,
            checkpoint_key=_KEY,
            require_settled=True,
            approval_gate=self.gate if gate else None,
        )
        return replayer.run(
            self.workflow,
            params=self.params,
            bundle_dir=self.bundle,
            run_dir=self.tmp_path / name,
        )


# -- the one path that saves without a person ------------------------------------


def test_matching_read_back_is_the_only_way_to_finish_without_a_person(tmp_path):
    item = Item(tmp_path)
    report = item.run()

    assert _save_delivered(item.backend)
    assert item.verifier.reads == 1
    assert report.transaction_outcome == "VERIFIED"
    approval = report.approval
    assert approval is not None
    assert approval.decision is ItemDecisionKind.NO_PERSON
    assert not approval.needs_person
    assert approval.saves_without_person == 1
    assert [c.code for c in approval.post_checks] == ["verified", "whole_run_clean"]
    assert all(c.passed for c in approval.pre_checks + approval.post_checks)
    # The block is persisted with the report, and holds codes only.
    saved = json.loads((tmp_path / "run" / "report.json").read_text())
    assert saved["approval"]["decision"] == "no_person"
    assert item.log.state().finished[-1].decision is ItemDecisionKind.NO_PERSON
    assert item.log.state().open_items == []


# -- a non-matching or uncertain read-back goes to a person -------------------------


def test_a_read_back_that_does_not_match_goes_to_a_person_and_switches_off(tmp_path):
    item = Item(tmp_path, verifier=RecordSystem(answer="mismatch"))
    report = item.run()

    assert _save_delivered(item.backend)  # the save happened, then was checked
    assert report.transaction_outcome != "VERIFIED"
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"  # "Check the record"
    approval = report.approval
    assert approval.decision is ItemDecisionKind.PROBLEM
    assert approval.needs_person
    assert approval.switch_off_reason is OffReason.UNCERTAIN_RESULT
    assert item.log.state().off_reason is OffReason.UNCERTAIN_RESULT

    # Nothing retries: the next item can't save without a person.
    next_backend = FakeBackend()
    item.backend = next_backend
    item.verifier.answer = "match"
    next_report = item.run("run-next")
    assert not _save_delivered(next_backend)
    assert next_report.approval.decision is ItemDecisionKind.APPROVAL_REQUIRED
    assert next_report.approval.pre_checks[0].detail == "switched_off"


def test_an_unreachable_read_back_after_the_save_goes_to_a_person(tmp_path):
    item = Item(tmp_path, verifier=RecordSystem(answer="unreachable"))
    report = item.run()

    assert _save_delivered(item.backend)
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"
    assert report.approval.needs_person
    assert report.approval.switch_off_reason is OffReason.UNCERTAIN_RESULT


# -- a failed pre-save check stops before the save is delivered ----------------------


def test_a_switched_off_mode_falls_back_to_per_item_approval(tmp_path):
    item = Item(tmp_path)
    turn_off(item.log, OffReason.TURNED_OFF)
    report = item.run()

    assert not _save_delivered(item.backend)
    assert item.verifier.reads == 0
    assert report.transaction_outcome == "HALTED_BEFORE_EFFECT"
    assert report.approval.decision is ItemDecisionKind.APPROVAL_REQUIRED
    assert report.approval.needs_person
    assert report.approval.gated_step_id == "submit"
    assert "waiting for a person" in (report.results[-1].error or "")


def test_lost_contact_with_cloud_pauses_for_approval_without_switching_off(tmp_path):
    item = Item(tmp_path)
    item.cloud.fail = True
    report = item.run()

    assert not _save_delivered(item.backend)
    assert report.approval.decision is ItemDecisionKind.APPROVAL_REQUIRED
    assert not report.approval.counts_toward_problems
    assert report.approval.switch_off_reason is None
    assert not item.log.state().is_off


def test_the_wrong_signed_in_user_stops_before_saving(tmp_path):
    item = Item(tmp_path)
    item.user.verdict = "mismatch"
    report = item.run()

    assert not _save_delivered(item.backend)
    assert item.user.frames, "P2 read the frame the save resolved on"
    assert report.transaction_outcome == "HALTED_BEFORE_EFFECT"
    assert report.approval.decision is ItemDecisionKind.STOP_BEFORE_SAVING
    assert report.approval.decision_reason == "signed_in_user"


def test_a_lost_record_check_stops_before_saving_and_switches_off(tmp_path):
    item = Item(tmp_path, verifier=RecordSystem(connection_ok=False))
    report = item.run()

    assert not _save_delivered(item.backend)
    assert report.approval.decision is ItemDecisionKind.STOP_BEFORE_SAVING
    assert report.approval.switch_off_reason is OffReason.RECORD_CHECK_LOST
    assert item.log.state().off_reason is OffReason.RECORD_CHECK_LOST


def test_a_read_back_on_another_verifier_stops_before_saving(tmp_path):
    verifier = RecordSystem()
    set_verifier_identity(verifier, "sha256:" + "4" * 64)
    item = Item(tmp_path, verifier=verifier)
    report = item.run()

    assert not _save_delivered(item.backend)
    assert report.approval.decision is ItemDecisionKind.STOP_BEFORE_SAVING
    pre = [c for c in report.approval.pre_checks if not c.passed]
    assert pre[-1].detail == "verifier_identity_changed"


def test_an_item_check_failure_uses_per_item_approval(tmp_path):
    item = Item(tmp_path, item=good_item(is_retry=True))
    report = item.run()

    assert not _save_delivered(item.backend)
    assert report.approval.decision is ItemDecisionKind.APPROVAL_REQUIRED
    assert report.approval.decision_reason == "not_a_retry"
    assert not report.approval.counts_toward_problems
    assert not item.log.state().is_off


def test_a_changed_screen_rung_pauses_and_switches_off(tmp_path):
    item = Item(tmp_path)
    item.record = make_record(
        bundle_content_digest=item.record.bundle_content_digest,
        rung_floor={"submit": "structural"},
    )
    item.log = open_log(tmp_path / "second", item.record)
    item.gate.record = item.record
    item.gate.log = item.log
    report = item.run()

    assert not _save_delivered(item.backend)
    assert report.approval.decision is ItemDecisionKind.APPROVAL_REQUIRED
    assert report.approval.switch_off_reason is OffReason.SCREEN_CHANGED


def test_an_input_from_the_recording_is_refused_before_any_step(tmp_path):
    workflow = _create_workflow()
    workflow.param_specs = {
        "note": ParamSpec(name="note", example="recorded example note")
    }
    item = Item(tmp_path, workflow=workflow)
    report = item.run()

    assert item.backend.actions == []
    assert report.transaction_outcome == "REJECTED_POLICY"
    assert report.results[-1].step_id == "<params>"
    assert "recorded example note" not in (report.results[-1].error or "")
    assert report.approval.decision is ItemDecisionKind.APPROVAL_REQUIRED
    assert report.approval.decision_reason == "inputs_supplied"


# -- each_item, not_gated, and no gate ---------------------------------------------------


def test_each_item_mode_never_delivers_a_save_without_a_person(tmp_path):
    item = Item(tmp_path, mode="each_item")
    report = item.run()

    assert not _save_delivered(item.backend)
    assert report.transaction_outcome == "HALTED_BEFORE_EFFECT"
    assert report.approval.decision is ItemDecisionKind.APPROVAL_REQUIRED
    assert report.approval.decision_reason == "each_item"


def test_a_missing_mode_gates_like_each_item(tmp_path):
    item = Item(tmp_path, mode=None)
    report = item.run()
    assert not _save_delivered(item.backend)
    assert report.approval.mode.value == "each_item"


def test_not_gated_saves_as_today_and_says_so(tmp_path):
    item = Item(tmp_path, mode="not_gated")
    report = item.run()

    assert _save_delivered(item.backend)
    assert report.transaction_outcome == "VERIFIED"
    assert report.approval.decision is ItemDecisionKind.NOT_GATED
    assert report.approval.decision_reason == "no_per_item_pause"
    assert report.approval.saves_without_person == 0


def test_without_a_gate_the_report_is_unchanged(tmp_path):
    item = Item(tmp_path)
    report = item.run(gate=False)

    assert report.transaction_outcome == "VERIFIED"
    assert report.approval is None
    saved = json.loads((tmp_path / "run" / "report.json").read_text())
    assert "approval" not in saved


@pytest.mark.parametrize("answer", ["match", "mismatch", "unreachable"])
def test_outcome_words_are_identical_with_and_without_the_gate(tmp_path, answer):
    gated = Item(tmp_path / "gated", verifier=RecordSystem(answer=answer))
    plain = Item(tmp_path / "plain", verifier=RecordSystem(answer=answer))
    with_gate = gated.run()
    without = plain.run(gate=False)
    assert with_gate.transaction_outcome == without.transaction_outcome
    assert with_gate.execution_outcome == without.execution_outcome
