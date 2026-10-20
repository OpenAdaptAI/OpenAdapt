"""The pre-save gate (P1 to P5), the item checks, coverage and the finish.

These tests drive :class:`ApprovalGate` directly with made-up steps and
results. The properties under test:

- in ``problems_only`` a save proceeds without a person only when every item
  check and every pre-check passes;
- every failure stops before the save and sends the item to a person, either
  for per-item approval or as "stop before saving";
- a switch-off is written to the runner's log at once, applies to the next
  item, and can't be undone by a Cloud status;
- ``each_item`` never lets a save through, and ``not_gated`` is recorded.
"""

from __future__ import annotations

import pytest

from openadapt_flow.approval_mode.coverage import save_coverage, workflow_coverage
from openadapt_flow.approval_mode.gate import (
    ApprovalGate,
    SaveContext,
    ScreenTextSignedInUser,
    dedicated_user_reference,
)
from openadapt_flow.approval_mode.model import (
    ApprovalMode,
    ItemDecisionKind,
    OffReason,
)
from openadapt_flow.approval_mode.switch import SwitchLog, turn_off
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    IdentityCheck,
    Resolution,
    RunReport,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.runtime.effects.adapter import (
    ConnectionProbe,
    set_verifier_identity,
)
from openadapt_flow.runtime.effects.effect import ValueExpr
from openadapt_flow.verification import VerificationTier
from openadapt_flow.vision.ocr import OcrLine
from tests._approval_mode_support import (
    BUNDLE_DIGEST,
    DEDICATED_USER,
    ENVIRONMENT_DIGEST,
    RUNNER_ID,
    USER_KEY,
    VERIFIER_IDENTITY,
    Clock,
    FakeCloud,
    FakeSignedInUser,
    good_item,
    make_record,
    open_log,
    status_keypair,
)
from tests.test_approval_mode_post import (
    _halted_report,
    _save,
    _verified_report,
)

# -- fixtures ------------------------------------------------------------------


def _anchor(text: str) -> Anchor:
    return Anchor(
        template="t.png",
        region=(0, 0, 10, 10),
        click_point=(5, 5),
        ocr_text=text,
        context_text="Synthetic record",
    )


def _effects(*, cover_note: bool = True) -> list[Effect]:
    effects = [
        Effect(
            kind=EffectKind.EXACT_NEW_SET,
            new_records=[{"patient_id": ValueExpr(param="patient_id")}],
            idempotency_key=ValueExpr(param="__run_id__"),
            risk="irreversible",
        )
    ]
    if cover_note:
        effects.append(
            Effect(
                kind=EffectKind.FIELD_EQUALS,
                match={"patient_id": ValueExpr(param="patient_id")},
                field="note",
                value=ValueExpr(param="note"),
            )
        )
    return effects


def _workflow(*, effects=None, note_param: str | None = "note") -> Workflow:
    return Workflow(
        name="referral",
        steps=[
            Step(
                id="patient",
                intent="type the patient id",
                action=ActionKind.TYPE,
                param="patient_id",
                anchor=_anchor("Patient"),
            ),
            Step(
                id="note",
                intent="type the note",
                action=ActionKind.TYPE,
                param=note_param,
                text=None if note_param else "recorded text",
                anchor=_anchor("Note"),
            ),
            Step(
                id="save",
                intent="save referral",
                action=ActionKind.CLICK,
                risk="irreversible",
                anchor=_anchor("Save"),
                identity_armed=True,
                effects=_effects() if effects is None else effects,
            ),
        ],
    )


def _resolution(rung: str = "template") -> Resolution:
    return Resolution(rung=rung, point=(5, 5), confidence=0.99, elapsed_ms=1.0)


def _result(step_id: str, *, status: str = "verified", rung: str = "template"):
    return StepResult(
        step_id=step_id,
        intent=step_id,
        ok=True,
        resolution=_resolution(rung),
        identity=IdentityCheck(status=status) if status else None,
    )


class FakeVerifier:
    substrate = "rest"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(self, *, ok: bool = True, identity: str = VERIFIER_IDENTITY):
        self.ok = ok
        self.probes = 0
        set_verifier_identity(self, identity)

    def test_connection(self, context=None):
        self.probes += 1
        return ConnectionProbe(ok=self.ok, substrate=self.substrate)


FLOORS = {"patient": "template", "note": "template", "save": "template"}


class Harness:
    def __init__(self, tmp_path, *, mode="problems_only", record=None, **gate_kw):
        self.private, self.keys = status_keypair()
        self.record = record or make_record(rung_floor=FLOORS)
        self.log = open_log(tmp_path, self.record)
        self.cloud = FakeCloud(self.private)
        self.clock = Clock()
        self.user = FakeSignedInUser()
        self.environment = ENVIRONMENT_DIGEST
        self.verifier = FakeVerifier()
        kwargs = {
            "record": self.record,
            "log": self.log,
            "pinned_status_keys": self.keys,
            "fetch_status": self.cloud,
            "environment_digest": lambda: self.environment,
            "runner_id": RUNNER_ID,
            "signed_in_user": self.user,
            "dedicated_user_key": USER_KEY,
            "item": good_item(),
            "monotonic": self.clock,
        }
        kwargs.update(gate_kw)
        self.item = kwargs["item"]
        self.gate = ApprovalGate(mode, **kwargs)
        self.workflow = _workflow()

    def begin(self, run_id="run-0001", *, fresh_item=True, **overrides):
        if fresh_item and self.item is not None:
            self.gate.next_item(self.item)
        fields = {
            "run_id": run_id,
            "bundle_content_digest": BUNDLE_DIGEST,
            "profile": "regulated",
        }
        fields.update(overrides)
        return self.gate.begin_item(**fields)

    def save_context(self, *, prior=None, current=None, workflow=None, **overrides):
        workflow = workflow or self.workflow
        fields = {
            "step": workflow.steps[-1],
            "workflow": workflow,
            "result": current or _result("save"),
            "prior_results": (
                prior
                if prior is not None
                else [_result("patient", status=""), _result("note", status="")]
            ),
            "effect_verifier": self.verifier,
            "governed_authorization": None,
            "model_calls": 0,
            "frame": lambda: b"frame-bytes",
        }
        fields.update(overrides)
        return SaveContext(**fields)

    def before_save(self, **overrides):
        return self.gate.before_save(self.save_context(**overrides))


# -- the passing path ------------------------------------------------------------


def test_every_check_passing_lets_the_save_proceed_and_logs_its_start(tmp_path):
    h = Harness(tmp_path)
    assert h.begin() is None
    verdict = h.before_save()
    assert verdict.proceed, verdict
    assert [c.code for c in verdict.checks] == [
        "switch_on",
        "signed_in_user",
        "right_record",
        "same_screens",
        "record_check_ready",
    ]
    assert all(c.passed for c in verdict.checks)
    state = h.log.state()
    assert len(state.open_items) == 1
    # The status request carried the runner's nonce and latest event.
    request = h.cloud.requests[0]
    assert request.runner_event_seq == 1
    assert h.verifier.probes == 1


# -- item checks fall back to per-item approval -----------------------------------


@pytest.mark.parametrize(
    "item_change,code",
    [
        ({"channel": None}, "channel"),
        ({"channel": "cloud_schedule"}, "channel"),
        ({"header_maps_every_input": False}, "inputs_strict"),
        ({"rows_well_formed": False}, "inputs_strict"),
        ({"values_pass_types": False}, "inputs_strict"),
        ({"is_retry": True}, "not_a_retry"),
        ({"allow_repeat": True}, "not_a_retry"),
        ({"record_key_seen_recently": True}, "record_key_not_recent"),
        ({"record_key_seen_recently": None}, "record_key_not_recent"),
        ({"caller_supplied_configuration": True}, "deployment_config"),
        ({"holds_switch_lease": False}, "switch_lease"),
    ],
)
def test_a_failed_item_check_means_per_item_approval_not_a_switch_off(
    tmp_path, item_change, code
):
    h = Harness(tmp_path, item=good_item(**item_change))
    h.begin()
    verdict = h.before_save()
    assert not verdict.proceed
    assert verdict.decision.kind is ItemDecisionKind.APPROVAL_REQUIRED
    assert verdict.decision.reason == code
    assert not verdict.decision.counts_toward_problems
    assert verdict.decision.switch_off is None
    assert not h.log.state().is_off
    assert h.cloud.requests == []


def test_a_default_item_context_never_saves_without_a_person(tmp_path):
    from openadapt_flow.approval_mode.model import ItemContext

    h = Harness(tmp_path, item=ItemContext())
    h.begin()
    assert not h.before_save().proceed


@pytest.mark.parametrize(
    "begin_change,code",
    [
        ({"profile": "standard"}, "deployment_config"),
        ({"profile": "demo"}, "deployment_config"),
        ({"model_egress_possible": True}, "deployment_config"),
        ({"durable_resume": True}, "not_resumed"),
    ],
)
def test_run_level_item_checks(tmp_path, begin_change, code):
    h = Harness(tmp_path)
    h.begin(**begin_change)
    verdict = h.before_save()
    assert not verdict.proceed
    assert verdict.decision.reason == code


def test_an_input_from_the_recording_is_refused_before_anything_runs(tmp_path):
    h = Harness(tmp_path)
    refusal = h.begin(inputs_from_recording=["note"])
    assert refusal is not None and "recording" in refusal
    assert "note" not in refusal  # names are counted, not echoed
    assert not h.before_save().proceed


def test_each_item_refuses_recorded_inputs_too(tmp_path):
    h = Harness(tmp_path, mode="each_item")
    assert h.begin(inputs_from_recording=["note"]) is not None


# -- P1 switch_on ------------------------------------------------------------------


def test_no_switch_record_means_per_item_approval(tmp_path):
    gate = ApprovalGate("problems_only", item=good_item())
    gate.begin_item(
        run_id="r", bundle_content_digest=BUNDLE_DIGEST, profile="regulated"
    )
    h = Harness(tmp_path)
    verdict = gate.before_save(h.save_context())
    assert not verdict.proceed
    assert verdict.decision.kind is ItemDecisionKind.APPROVAL_REQUIRED
    # Without a record even the channel can't be matched; P1 would say the
    # same once the item checks passed.
    assert verdict.checks[0].detail == "channel_not_covered"


def test_a_local_switch_off_applies_before_the_next_save_even_if_cloud_says_on(
    tmp_path,
):
    h = Harness(tmp_path)
    turn_off(h.log, OffReason.TURNED_OFF)
    h.begin()
    verdict = h.before_save()
    assert not verdict.proceed
    assert verdict.checks[0].detail == "switched_off"
    # Cloud was never even asked: the local log is the authority for "off".
    assert h.cloud.requests == []


def test_missing_or_broken_log_counts_as_off(tmp_path):
    h = Harness(tmp_path)
    h.log.path.write_bytes(h.log.path.read_bytes()[:-3])
    h.begin()
    assert h.before_save().checks[0].detail == "switch_log_unreadable"
    h.log.path.unlink()
    h.begin(run_id="run-0002")
    assert h.before_save().checks[0].detail == "switch_log_unreadable"


def test_a_log_for_another_record_is_refused(tmp_path):
    h = Harness(tmp_path)
    other = make_record(switch_id="switch-test-0002", rung_floor=FLOORS)
    h.gate.log = SwitchLog.open_new(tmp_path / "other" / "log.jsonl", other)
    h.begin()
    assert h.before_save().checks[0].detail == "switch_log_for_other_record"


@pytest.mark.parametrize(
    "mutate,detail,off",
    [
        ("bundle", "version_changed", OffReason.VERSION_CHANGED),
        ("environment", "environment_changed", OffReason.ENVIRONMENT_CHANGED),
        ("runner", "runner_changed", OffReason.ENVIRONMENT_CHANGED),
    ],
)
def test_a_changed_version_environment_or_runner_switches_off(
    tmp_path, mutate, detail, off
):
    kwargs = {"runner_id": "runner-test-9999"} if mutate == "runner" else {}
    h = Harness(tmp_path, **kwargs)
    if mutate == "environment":
        h.environment = "sha256:" + "0" * 64
    h.begin(bundle_content_digest="b" * 64 if mutate == "bundle" else BUNDLE_DIGEST)
    verdict = h.before_save()
    assert not verdict.proceed
    assert verdict.checks[0].detail == detail
    assert verdict.decision.switch_off is off
    assert h.log.state().off_reason is off


def test_an_unreadable_environment_pauses_without_switching_off(tmp_path):
    h = Harness(tmp_path)
    h.environment = None
    h.begin()
    verdict = h.before_save()
    assert verdict.checks[0].detail == "environment_unreadable"
    assert not h.log.state().is_off


def test_an_interrupted_earlier_item_switches_off_with_uncertain_result(tmp_path):
    h = Harness(tmp_path)
    h.begin(run_id="run-crashed")
    assert h.before_save().proceed
    # The process died before the item finished: no ``item_finished``.
    h.begin(run_id="run-next")
    verdict = h.before_save()
    assert not verdict.proceed
    assert verdict.checks[0].detail == "interrupted_item"
    assert h.log.state().off_reason is OffReason.UNCERTAIN_RESULT


def test_a_stale_status_pauses_then_recovers_by_itself(tmp_path):
    h = Harness(tmp_path)
    h.cloud.fail = True
    h.begin()
    verdict = h.before_save()
    assert not verdict.proceed
    assert verdict.checks[0].detail == "status_fetch_failed"
    assert verdict.decision.kind is ItemDecisionKind.APPROVAL_REQUIRED
    assert not verdict.decision.counts_toward_problems
    assert verdict.decision.switch_off is None
    assert h.log.state().status_unconfirmed
    # The next item, with Cloud reachable again, resumes by itself.
    h.cloud.fail = False
    h.begin(run_id="run-0002")
    assert h.before_save().proceed
    assert not h.log.state().status_unconfirmed


def test_status_freshness_is_measured_on_the_monotonic_clock(tmp_path):
    h = Harness(tmp_path)
    h.begin()
    ctx = h.save_context()
    assert h.gate.before_save(ctx).proceed
    # Another delivery edge of the same save, 16 minutes later.
    h.clock.now += 16 * 60
    verdict = h.gate.before_save(ctx)
    assert not verdict.proceed
    assert verdict.checks[0].detail == "status_unconfirmed"


def test_a_tampered_or_unpinned_status_is_unconfirmed_never_on(tmp_path):
    h = Harness(tmp_path)
    h.cloud.tamper = {"state": "on", "runner_event_seq": 7}
    h.begin()
    assert h.before_save().checks[0].detail == "status_rejected"
    h2 = Harness(tmp_path / "other", pinned_status_keys={})
    h2.begin()
    assert h2.before_save().checks[0].detail == "status_rejected"


def test_cloud_off_is_written_to_the_local_log(tmp_path):
    h = Harness(tmp_path)
    h.cloud.state = "off"
    h.cloud.off_reason = "turned_off"
    h.begin()
    verdict = h.before_save()
    assert verdict.checks[0].detail == "cloud_switched_off"
    assert h.log.state().off_reason is OffReason.TURNED_OFF


def test_no_status_source_never_means_on(tmp_path):
    h = Harness(tmp_path, fetch_status=None)
    h.begin()
    assert h.before_save().checks[0].detail == "no_status_source"


# -- P2 signed_in_user ------------------------------------------------------------


@pytest.mark.parametrize(
    "verdict,detail",
    [("mismatch", "other_user_signed_in"), ("unreadable", "signed_in_user_unreadable")],
)
def test_the_wrong_signed_in_user_stops_before_saving(tmp_path, verdict, detail):
    h = Harness(tmp_path)
    h.user.verdict = verdict
    h.begin()
    result = h.before_save()
    assert not result.proceed
    assert result.decision.kind is ItemDecisionKind.STOP_BEFORE_SAVING
    assert result.checks[-1].code == "signed_in_user"
    assert result.checks[-1].detail == detail


def test_no_frame_or_no_check_stops_before_saving(tmp_path):
    h = Harness(tmp_path)
    h.begin()
    assert h.before_save(frame=lambda: None).checks[-1].detail == "frame_unavailable"
    h2 = Harness(tmp_path / "b", signed_in_user=None)
    h2.begin()
    assert h2.before_save().checks[-1].detail == "not_configured"


def test_another_dedicated_user_switches_off(tmp_path):
    h = Harness(tmp_path, signed_in_user=FakeSignedInUser(identifier="someone.else"))
    h.begin()
    verdict = h.before_save()
    assert verdict.checks[-1].detail == "dedicated_user_changed"
    assert h.log.state().off_reason is OffReason.ENVIRONMENT_CHANGED


class _Vision:
    def __init__(self, texts):
        self.texts = texts

    def ocr(self, png, *, region=None):
        return [
            OcrLine(text=text, region=(0, 0, 10, 10), confidence=0.99)
            for text in self.texts
        ]


def test_screen_text_check_matches_whole_tokens_only():
    check = ScreenTextSignedInUser(
        _Vision(["Signed in: openadapt.intake"]), DEDICATED_USER
    )
    assert check.check(b"x") == "match"
    near = ScreenTextSignedInUser(_Vision(["openadapt.intake2"]), DEDICATED_USER)
    assert near.check(b"x") == "mismatch"
    assert ScreenTextSignedInUser(_Vision([]), DEDICATED_USER).check(b"x") == (
        "unreadable"
    )
    assert check.reference(USER_KEY) == dedicated_user_reference(
        DEDICATED_USER, USER_KEY
    )


# -- P3 right_record ---------------------------------------------------------------


@pytest.mark.parametrize("status", ["abstain", "unreadable", "mismatch"])
def test_any_non_affirmative_identity_stops_before_saving(tmp_path, status):
    h = Harness(tmp_path)
    h.begin()
    verdict = h.before_save(current=_result("save", status=status))
    assert not verdict.proceed
    assert verdict.decision.kind is ItemDecisionKind.STOP_BEFORE_SAVING
    assert verdict.checks[-1].code == "right_record"


def test_an_unverified_save_target_stops_before_saving(tmp_path):
    h = Harness(tmp_path)
    h.begin()
    verdict = h.before_save(current=_result("save", status=""))
    assert verdict.checks[-1].detail == "save_target_not_verified"


# -- P4 same_screens ----------------------------------------------------------------


@pytest.mark.parametrize(
    "prior_change,detail",
    [
        ({"rung": "ocr"}, "rung_below_floor"),
        ({"rung": "grounder"}, "grounder_rung"),
    ],
)
def test_a_changed_screen_pauses_for_approval_and_switches_off(
    tmp_path, prior_change, detail
):
    h = Harness(tmp_path)
    h.begin()
    prior = [_result("patient", status="", **prior_change), _result("note", status="")]
    verdict = h.before_save(prior=prior)
    assert not verdict.proceed
    assert verdict.decision.kind is ItemDecisionKind.APPROVAL_REQUIRED
    assert verdict.checks[-1].detail == detail
    assert h.log.state().off_reason is OffReason.SCREEN_CHANGED


def test_a_model_call_earlier_in_the_run_pauses_and_switches_off(tmp_path):
    h = Harness(tmp_path)
    h.begin()
    verdict = h.before_save(model_calls=1)
    assert verdict.checks[-1].detail == "model_call"
    assert h.log.state().off_reason is OffReason.SCREEN_CHANGED


# -- P5 record_check_ready -----------------------------------------------------------


def test_a_failed_connection_probe_stops_and_switches_off(tmp_path):
    h = Harness(tmp_path)
    h.verifier.ok = False
    h.begin()
    verdict = h.before_save()
    assert not verdict.proceed
    assert verdict.decision.kind is ItemDecisionKind.STOP_BEFORE_SAVING
    assert verdict.checks[-1].detail == "record_check_unreachable"
    assert h.log.state().off_reason is OffReason.RECORD_CHECK_LOST


def test_no_verifier_or_another_verifier_stops(tmp_path):
    h = Harness(tmp_path)
    h.begin()
    assert h.before_save(effect_verifier=None).checks[-1].detail == "no_record_check"
    h2 = Harness(tmp_path / "b")
    h2.verifier = FakeVerifier(identity="sha256:" + "3" * 64)
    h2.begin()
    assert h2.before_save().checks[-1].detail == "verifier_identity_changed"


def test_the_ui_read_back_pilot_never_saves_without_a_person(tmp_path):
    h = Harness(tmp_path)
    h.verifier.verification_tier = VerificationTier.INDEPENDENT_SESSION
    h.begin()
    verdict = h.before_save()
    assert not verdict.proceed
    assert verdict.decision.needs_person
    assert verdict.checks[-1].detail == "ui_readback_pilot_each_item_only"


def test_a_weaker_read_back_tier_stops(tmp_path):
    h = Harness(tmp_path)
    h.verifier.verification_tier = VerificationTier(3)
    h.begin()
    assert h.before_save().checks[-1].detail == "read_back_not_system_of_record"


def test_an_input_the_record_check_does_not_read_back_stops(tmp_path):
    h = Harness(tmp_path)
    workflow = _workflow(effects=_effects(cover_note=False))
    h.begin()
    verdict = h.before_save(workflow=workflow)
    assert not verdict.proceed
    assert verdict.checks[-1].detail == "input_not_read_back"


def test_a_save_with_no_record_check_stops(tmp_path):
    h = Harness(tmp_path)
    workflow = _workflow(effects=[])
    h.begin()
    assert h.before_save(workflow=workflow).checks[-1].detail == "no_record_check"


# -- each_item and not_gated -----------------------------------------------------------


def test_each_item_never_lets_a_save_through(tmp_path):
    h = Harness(tmp_path, mode="each_item")
    h.begin()
    verdict = h.before_save()
    assert not verdict.proceed
    assert verdict.decision.kind is ItemDecisionKind.APPROVAL_REQUIRED
    assert verdict.decision.reason == "each_item"
    # Even an otherwise perfect switch setup doesn't matter in each_item.
    assert h.cloud.requests == []


def test_missing_mode_gates_like_each_item(tmp_path):
    h = Harness(tmp_path, mode=None)
    assert h.gate.mode is ApprovalMode.EACH_ITEM
    h.begin()
    assert not h.before_save().proceed


def test_not_gated_proceeds_and_is_recorded(tmp_path):
    h = Harness(tmp_path, mode="not_gated")
    h.begin()
    assert h.before_save().proceed
    evidence = h.gate.finish_item(
        RunReport(workflow_name="w", started_at="2026-10-12T10:00:00+00:00")
    )
    assert evidence.decision is ItemDecisionKind.NOT_GATED
    assert evidence.decision_reason == "no_per_item_pause"


def test_a_gate_that_already_stopped_never_reopens_for_the_item(tmp_path):
    h = Harness(tmp_path)
    h.user.verdict = "mismatch"
    h.begin()
    assert not h.before_save().proceed
    h.user.verdict = "match"
    assert not h.before_save().proceed


def test_a_gate_exception_goes_to_a_person(tmp_path):
    def boom():
        raise RuntimeError("observer crashed")

    h = Harness(tmp_path)
    h.begin()
    verdict = h.before_save(frame=boom)  # handled inside P2 as no frame
    assert not verdict.proceed
    h2 = Harness(tmp_path / "b")
    h2.gate._began = False  # noqa: SLF001 - simulate a caller that skipped begin
    verdict = h2.gate.before_save(h2.save_context())
    assert not verdict.proceed
    assert verdict.decision.reason == "gate_error"


# -- coverage ------------------------------------------------------------------------------


def test_coverage_requires_every_entered_input_to_be_read_back():
    assert save_coverage(_workflow(), _workflow().steps[-1]).covered
    partial = _workflow(effects=_effects(cover_note=False))
    uncovered = save_coverage(partial, partial.steps[-1])
    assert uncovered.problems == ("input_not_read_back",)
    assert uncovered.uncovered_inputs == ("note",)


def test_coverage_refuses_recorded_values_and_secrets():
    recorded = _workflow(note_param=None)
    assert (
        "recorded_value_entry" in save_coverage(recorded, recorded.steps[-1]).problems
    )
    secret = _workflow()
    secret.steps[1].secret = True
    assert "secret_entry" in save_coverage(secret, secret.steps[-1]).problems


def test_idempotency_key_alone_does_not_cover_an_input():
    effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"clinic": ValueExpr(literal="c1")},
            idempotency_key=ValueExpr(param="patient_id"),
        )
    ]
    workflow = _workflow(effects=effects)
    coverage = save_coverage(workflow, workflow.steps[-1])
    assert "patient_id" in coverage.uncovered_inputs


def test_workflow_coverage_lists_every_save():
    rows = workflow_coverage(_workflow())
    assert [row.step_id for row in rows] == ["save"]
    assert rows[0].covered


# -- finishing an item ------------------------------------------------------------------


def _gated_save(**overrides) -> StepResult:
    """A save result as the replayer hands it to the gate, then to the report.

    R1 checks that every consequential result in the report is one the gate
    passed, so the same object goes through ``before_save`` and the report.
    """

    return _save(identity=IdentityCheck(status="verified"), **overrides)


def test_a_verified_clean_item_is_saved_without_a_person_and_logged(tmp_path):
    h = Harness(tmp_path)
    h.begin()
    saved = _gated_save()
    assert h.before_save(current=saved).proceed
    evidence = h.gate.finish_item(_verified_report(saved))
    assert evidence.decision is ItemDecisionKind.NO_PERSON
    assert not evidence.needs_person
    assert evidence.switch_log_recorded
    assert evidence.saves_without_person == 1
    state = h.log.state()
    assert state.open_items == []
    assert state.finished[-1].decision is ItemDecisionKind.NO_PERSON
    # Finishing again with the same result writes nothing new.
    events = len(h.log.read())
    h.gate.finish_item(_verified_report(saved))
    assert len(h.log.read()) == events


def test_a_verified_save_the_gate_never_passed_goes_to_a_person(tmp_path):
    h = Harness(tmp_path)
    h.begin()
    assert h.before_save(current=_gated_save()).proceed
    # The report's save is another result object: the gate never checked it.
    evidence = h.gate.finish_item(_verified_report(_gated_save()))
    assert evidence.decision is not ItemDecisionKind.NO_PERSON
    assert evidence.needs_person
    assert evidence.post_checks[0].detail == "save_not_gated"


def test_a_save_the_read_back_contradicts_goes_to_a_person_and_switches_off(
    tmp_path,
):
    h = Harness(tmp_path)
    h.begin()
    assert h.before_save().proceed
    evidence = h.gate.finish_item(_halted_report("RECONCILIATION_REQUIRED"))
    assert evidence.decision is ItemDecisionKind.PROBLEM
    assert evidence.needs_person
    assert evidence.switch_off_reason is OffReason.UNCERTAIN_RESULT
    assert h.log.state().off_reason is OffReason.UNCERTAIN_RESULT
    # The next item can't save without a person.
    h.begin(run_id="run-0002")
    assert h.before_save().checks[0].detail == "switched_off"


def test_a_workaround_after_a_verified_save_goes_to_a_person(tmp_path):
    h = Harness(tmp_path)
    h.begin()
    saved = _gated_save()
    assert h.before_save(current=saved).proceed
    # A vision rescue of the save's postcondition happens after delivery.
    saved.postcondition_drift_rescues = ["Saved"]
    report = _verified_report(saved)
    evidence = h.gate.finish_item(report)
    assert report.transaction_outcome == "VERIFIED"  # the word never changes
    assert evidence.decision is ItemDecisionKind.PROBLEM
    assert evidence.decision_reason == "screen_changed"
    assert evidence.needs_person
    assert h.log.state().off_reason is OffReason.SCREEN_CHANGED


def test_a_stop_before_saving_is_finished_as_a_counted_problem(tmp_path):
    h = Harness(tmp_path)
    h.user.verdict = "mismatch"
    h.begin()
    assert not h.before_save().proceed
    evidence = h.gate.finish_item(_halted_report("HALTED_BEFORE_EFFECT"))
    assert evidence.decision is ItemDecisionKind.STOP_BEFORE_SAVING
    assert evidence.counts_toward_problems
    assert evidence.switch_off_reason is None
    assert evidence.gated_step_id == "save"


def test_three_counted_problems_in_a_row_switch_the_mode_off(tmp_path):
    h = Harness(tmp_path)
    h.user.verdict = "mismatch"
    for index in range(3):
        h.begin(run_id=f"run-{index}")
        assert not h.before_save().proceed
        evidence = h.gate.finish_item(_halted_report("HALTED_BEFORE_EFFECT"))
    assert evidence.switch_off_reason is OffReason.TOO_MANY_PROBLEMS
    assert h.log.state().off_reason is OffReason.TOO_MANY_PROBLEMS


def test_items_sent_to_approval_by_a_stale_status_never_switch_off(tmp_path):
    h = Harness(tmp_path)
    h.cloud.fail = True
    for index in range(6):
        h.begin(run_id=f"run-{index}")
        assert not h.before_save().proceed
        h.gate.finish_item(_halted_report("HALTED_BEFORE_EFFECT"))
    assert not h.log.state().is_off


def test_an_unloggable_result_is_never_counted_as_no_person(tmp_path):
    h = Harness(tmp_path)
    h.begin()
    saved = _gated_save()
    assert h.before_save(current=saved).proceed
    h.log.path.write_bytes(h.log.path.read_bytes() + b"garbage\n")
    evidence = h.gate.finish_item(_verified_report(saved))
    assert evidence.decision is ItemDecisionKind.PROBLEM
    assert evidence.decision_reason == "switch_log"
    assert evidence.needs_person
    assert not evidence.switch_log_recorded


def test_each_item_finish_always_needs_a_person(tmp_path):
    h = Harness(tmp_path, mode="each_item")
    h.begin()
    assert not h.before_save().proceed
    evidence = h.gate.finish_item(_halted_report("HALTED_BEFORE_EFFECT"))
    assert evidence.decision is ItemDecisionKind.APPROVAL_REQUIRED
    assert evidence.needs_person
    assert evidence.saves_without_person == 0


def test_a_reused_gate_never_carries_one_items_context_to_the_next(tmp_path):
    h = Harness(tmp_path)
    h.begin(run_id="run-a")
    assert h.before_save().proceed
    h.gate.finish_item(_verified_report())
    # The next item without a fresh context gets the fail-safe default.
    h.begin(run_id="run-b", fresh_item=False)
    verdict = h.before_save()
    assert not verdict.proceed
    assert verdict.decision.kind is ItemDecisionKind.APPROVAL_REQUIRED
    # With its own context it may proceed.
    h.begin(run_id="run-c")
    assert h.before_save().proceed


def test_an_unknown_runner_or_unsealed_bundle_pauses_without_switching_off(tmp_path):
    h = Harness(tmp_path, runner_id=None)
    h.begin()
    assert h.before_save().checks[0].detail == "runner_unknown"
    assert not h.log.state().is_off
    h2 = Harness(tmp_path / "b")
    h2.begin(bundle_content_digest=None)
    assert h2.before_save().checks[0].detail == "bundle_unsealed"
    assert not h2.log.state().is_off


def test_the_gate_classifies_saves_as_the_run_gate_does(monkeypatch):
    # A click a qualification classified as reversible (it opens a
    # confirmation dialog) whose label reads like a write. With the
    # certifying policy the gate honors that classification while the
    # certification is current, as the run gate does; without it the
    # conservative heuristic treats the click as a save.
    from types import SimpleNamespace

    from openadapt_flow import qualification
    from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow
    from openadapt_flow.qualification import (
        ActionRiskClass,
        ActionRiskClassification,
        EnvironmentBoundary,
        init_project,
        set_action_classification,
    )

    step = Step(
        id="open_confirm",
        intent="Create New Patient",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/create.png",
            region=(0, 0, 10, 10),
            click_point=(5, 5),
            ocr_text="Create New Patient",
        ),
        risk="reversible",
        risk_explanation="operator-qualified override: reversible",
    )
    workflow = Workflow(name="register", steps=[step])
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="Synthetic fixture",
            application_version="1",
            environment_digest="e" * 64,
            runtime_version="test",
        ),
    )
    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id="open_confirm",
            classification=ActionRiskClass.READ_ONLY,
            explanation="opens a confirmation dialog; the save is the next click",
            operator_confirmed=True,
        ),
    )
    monkeypatch.setattr(
        qualification,
        "current_certification_matches",
        lambda workflow, **kw: kw.get("policy") is not None,
    )
    ctx = SimpleNamespace(step=step, workflow=workflow)

    assert (
        ApprovalGate("problems_only", policy_name="clinical-write")._is_save(ctx)
        is False
    )
    assert ApprovalGate("problems_only")._is_save(ctx) is True
