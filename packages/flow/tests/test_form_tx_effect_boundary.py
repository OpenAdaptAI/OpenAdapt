"""Direct API failures cannot turn possibly delivered actions into safe retries."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from openadapt_flow.form_tx.reconciler import FormReconciler, ReconcilerSession
from openadapt_flow.form_tx.types import (
    ControlKind,
    DossierFact,
    FieldAnchorSpec,
    FieldBindingSpec,
    FormPack,
    FormSnapshot,
    FormTxOutcome,
    HumanDecisionAction,
    HumanResolutionInput,
    InputDossier,
    ObservedControl,
    ObservedOption,
    ProvenanceKind,
    SummaryCheckSpec,
    UIProfileSpec,
    WizardScreenSpec,
)
from tests.test_form_tx_frames import app as app
from tests.test_form_tx_frames import browser as browser


def inputs(kind=ControlKind.TEXT):
    binding = FieldBindingSpec(
        binding_id="value",
        dossier_key="value",
        anchor=FieldAnchorSpec(css_selector="#value"),
        control_kind=kind,
    )
    pack = FormPack(
        pack_id="effect-boundary",
        domain_schema="synthetic/v1",
        ui_profile_id="synthetic",
        bindings=(binding,),
        screens=(
            WizardScreenSpec(
                screen_id="form",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(css_selector="#save"),
            ),
        ),
        summary_check=SummaryCheckSpec(field_readbacks=(binding,)),
    )
    dossier = InputDossier(
        dossier_id="synthetic",
        request_id="synthetic",
        facts={
            "value": DossierFact(
                key="value",
                value="Yes",
                provenance=ProvenanceKind.INTAKE_DOCUMENT,
                source_id="synthetic",
            )
        },
    )
    return pack, dossier


class Surface:
    ui_profile = UIProfileSpec(profile_id="synthetic")

    def __init__(self, failure=None, when="after", exception=RuntimeError):
        self.failure = failure
        self.when = when
        self.exception = exception
        self.value = ""
        self.saved = None
        self.mutation_count = 0
        self.calls = []
        self.snapshots = 0

    def _raise(self):
        raise self.exception("synthetic private input must not enter result")

    def snapshot(self, **kwargs):
        self.calls.append("snapshot")
        self.snapshots += 1
        if self.failure == "initial_snapshot" or (
            self.failure == "post_snapshot" and self.saved is not None
        ):
            self._raise()
        return FormSnapshot(
            url_origin_and_path="https://example.test/form",
            screen_id="form",
            controls=(
                ObservedControl(
                    control_id="value",
                    dom_selector="#value",
                    tag="input",
                    input_type="text",
                    label_text="Value",
                    current_value=self.value,
                    checked=None,
                    readonly=self.saved is not None,
                    options=(ObservedOption(value="Yes", label="Yes"),),
                ),
                ObservedControl(
                    control_id="save", dom_selector="#save", tag="button", role="button"
                ),
            ),
        )

    def _act(self, name, value="Yes"):
        self.calls.append(name)
        if self.failure == name and self.when == "before":
            self._raise()
        self.value = value
        if name == "click":
            self.saved = self.value
            self.mutation_count += 1
        if self.failure == name and self.when == "after":
            self._raise()

    def fill_text(self, selector, value):
        self._act("fill_text", value)

    def select_native_option(self, selector, value):
        self._act("select_native_option", value)

    def set_checkbox_or_radio(self, selector, checked=True):
        self._act("set_checkbox_or_radio", "Yes" if checked else "No")

    def open_portal_combobox_options(self, selector):
        self._act("open_portal_combobox_options", "")
        return (ObservedOption(value="Yes", label="Yes"),)

    def choose_portal_combobox_option(self, selector, option):
        self._act("choose_portal_combobox_option", option.value)

    def click(self, selector):
        self._act("click")

    def goto(self, path):
        self._act("goto")

    @contextmanager
    def open_popup_surface(self, *args, **kwargs):
        self._act("popup_enter")
        try:
            yield self
        finally:
            self._act("popup_exit")


def assert_frozen(reconciler, session, surface, result):
    assert result.outcome == FormTxOutcome.DELIVERY_UNCERTAIN
    assert result.safe_to_retry is False
    assert result.receipt is None and result.outputs == {}
    assert "synthetic private input" not in result.what_happened
    calls = list(surface.calls)
    surface.failure = None
    assert reconciler.run_until_halt_or_done(session) is result
    assert (
        reconciler.apply_human_resolution(
            session,
            HumanResolutionInput(
                halt_id="new-halt",
                action=HumanDecisionAction.PROVIDE_VALUE,
                actor_id="synthetic-operator",
                idempotency_key="new-attempt",
                provided_value="Yes",
            ),
        )
        is result
    )
    assert surface.calls == calls


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize(
    "operation,kind",
    [
        ("fill_text", ControlKind.TEXT),
        ("select_native_option", ControlKind.SELECT),
        ("set_checkbox_or_radio", ControlKind.CHECKBOX),
        ("open_portal_combobox_options", ControlKind.PORTAL_COMBOBOX),
        ("choose_portal_combobox_option", ControlKind.PORTAL_COMBOBOX),
        ("click", ControlKind.TEXT),
    ],
)
def test_backend_entry_is_uncertain_even_without_an_observed_mutation(
    operation, kind, when
):
    surface = Surface(operation, when)
    pack, dossier = inputs(kind)
    reconciler = FormReconciler(surface)
    session = ReconcilerSession(pack, dossier)
    result = reconciler.run_until_halt_or_done(session)
    assert operation in surface.calls
    assert_frozen(reconciler, session, surface, result)


def test_success_control_reaches_real_summary_judge_and_signed_receipt():
    surface = Surface()
    pack, dossier = inputs()
    result = FormReconciler(surface).run_until_halt_or_done(
        ReconcilerSession(pack, dossier)
    )
    assert surface.saved == "Yes"
    assert result.outcome == FormTxOutcome.VERIFIED_DONE
    assert result.receipt is not None


def test_initial_backend_observation_failure_is_a_no_dispatch_refusal_and_can_be_reobserved():
    surface = Surface("initial_snapshot")
    pack, dossier = inputs()
    session = ReconcilerSession(pack, dossier)
    reconciler = FormReconciler(surface)
    result = reconciler.run_until_halt_or_done(session)
    assert result.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert result.safe_to_retry and result.receipt is None
    assert surface.calls == ["snapshot"] and surface.saved is None
    assert "synthetic private input" not in result.what_happened
    surface.failure = None
    assert (
        reconciler.run_until_halt_or_done(session).outcome
        == FormTxOutcome.VERIFIED_DONE
    )


def test_saved_effect_then_snapshot_failure_has_no_receipt_or_resend():
    surface = Surface("post_snapshot")
    pack, dossier = inputs()
    session = ReconcilerSession(pack, dossier)
    reconciler = FormReconciler(surface)
    result = reconciler.run_until_halt_or_done(session)
    assert surface.saved == "Yes" and surface.mutation_count == 1
    assert_frozen(reconciler, session, surface, result)


@pytest.mark.parametrize("exception", [RuntimeError, ValueError])
def test_injected_post_commit_readback_failure_freezes_delivery(exception):
    class FailingJudge:
        def evaluate(self, **kwargs):
            raise exception("synthetic private input must not enter result")

    surface = Surface()
    pack, dossier = inputs()
    session = ReconcilerSession(pack, dossier)
    reconciler = FormReconciler(surface, summary_judge=FailingJudge())
    result = reconciler.run_until_halt_or_done(session)
    assert surface.saved == "Yes"
    assert_frozen(reconciler, session, surface, result)


@pytest.mark.parametrize("exception", [KeyboardInterrupt, asyncio.CancelledError])
@pytest.mark.parametrize("operation", ["initial_snapshot", "click"])
def test_cancellation_propagates_but_possible_dispatch_cannot_be_retried(
    exception, operation
):
    surface = Surface(operation, exception=exception)
    pack, dossier = inputs()
    session = ReconcilerSession(pack, dossier)
    reconciler = FormReconciler(surface)
    with pytest.raises(exception):
        reconciler.run_until_halt_or_done(session)
    if operation == "click":
        assert surface.saved == "Yes"
        assert_frozen(reconciler, session, surface, session._terminal_result)
    else:
        surface.failure = None
        assert (
            reconciler.run_until_halt_or_done(session).outcome
            == FormTxOutcome.VERIFIED_DONE
        )


@pytest.mark.parametrize(
    "operation", ["goto", "popup_enter", "popup_exit", "child_fill"]
)
def test_navigation_and_popup_boundaries_share_the_session_effect_state(operation):
    class BoundaryJudge:
        def evaluate(self, *, surface, **kwargs):
            if operation == "goto":
                surface.goto("/summary")
            else:
                with surface.open_popup_surface("#popup") as child:
                    if operation == "child_fill":
                        child.fill_text("#value", "Yes")
            raise AssertionError("the planted backend failure must be reached")

    surface = Surface("fill_text" if operation == "child_fill" else operation)
    pack, dossier = inputs()
    # No pre-read field entry or explicit commit: the judge is the first user
    # of these sibling surfaces, so this checks the shared state independently.
    pack = pack.model_copy(update={"bindings": (), "screens": ()})
    session = ReconcilerSession(pack, dossier)
    reconciler = FormReconciler(surface, summary_judge=BoundaryJudge())
    assert_frozen(
        reconciler, session, surface, reconciler.run_until_halt_or_done(session)
    )


def test_human_value_actuation_failure_is_uncertain_and_new_keys_cannot_resend():
    surface = Surface()
    surface.value = "No"
    pack, dossier = inputs()
    session = ReconcilerSession(pack, dossier)
    reconciler = FormReconciler(surface)
    halted = reconciler.run_until_halt_or_done(session)
    assert halted.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    surface.failure = "fill_text"
    result = reconciler.apply_human_resolution(
        session,
        HumanResolutionInput(
            halt_id=halted.halts[0].halt_id,
            action=HumanDecisionAction.PROVIDE_VALUE,
            actor_id="synthetic-operator",
            idempotency_key="one",
            provided_value="Yes",
        ),
    )
    assert surface.value == "Yes" and surface.saved is None
    assert_frozen(reconciler, session, surface, result)


@pytest.mark.parametrize(
    "action",
    [
        HumanDecisionAction.DISMISS_PORTAL_ENTITY,
        HumanDecisionAction.CONFIRM_MODAL_ACTION,
    ],
)
def test_human_click_boundaries_freeze_after_a_delivered_action(action):
    from tests.test_form_tx_reconciler_and_receipts import (
        FakeStatefulFormSurface,
        _build_dossier,
        _build_pack,
    )

    surface = FakeStatefulFormSurface(
        **(
            {
                "entity_rows": [
                    {
                        "full_name": "Unknown Person",
                        "relationship": "Spouse",
                        "dob": "1980-01-01",
                    }
                ]
            }
            if action == HumanDecisionAction.DISMISS_PORTAL_ENTITY
            else {"trigger_duplicate_modal_on_commit": True}
        )
    )
    reconciler = FormReconciler(surface)
    session = ReconcilerSession(_build_pack(), _build_dossier(has_pool="No"))
    halted = reconciler.run_until_halt_or_done(session)
    halt = next(h for h in halted.halts if action in h.allowed_actions)
    surface.calls = []
    original_click, original_snapshot = surface.click, surface.snapshot

    def failing_click(selector):
        surface.calls.append("click")
        original_click(selector)
        raise RuntimeError("settle lost after delivered human click")

    def observed_snapshot(**kwargs):
        surface.calls.append("snapshot")
        return original_snapshot(**kwargs)

    surface.click, surface.snapshot = failing_click, observed_snapshot
    result = reconciler.apply_human_resolution(
        session,
        HumanResolutionInput(
            halt_id=halt.halt_id,
            action=action,
            actor_id="synthetic-operator",
            idempotency_key="one",
        ),
    )
    assert surface.calls.count("click") == 1
    if action == HumanDecisionAction.DISMISS_PORTAL_ENTITY:
        assert not surface.household_rows
    else:
        assert surface.committed_record is not None
    assert_frozen(reconciler, session, surface, result)


def test_real_headless_frame_post_then_settle_failure_cannot_resend(request):
    case = request.getfixturevalue("app")
    from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface
    from tests.test_form_tx_frames import load, profile

    frame = load(
        case,
        """<!doctype html><title>Effect fixture</title><body data-authenticated>
<label>Value<input id="value" value="Yes"></label>
<button id="save" onclick="fetch('/save', {method:'POST'}).then(() => {
    document.body.dataset.busy = 'true';
})">Save</button></body>""",
    )
    surface = PlaywrightFormSurface(frame, profile(500))
    pack, dossier = inputs()
    reconciler = FormReconciler(surface)
    session = ReconcilerSession(pack, dossier)
    result = reconciler.run_until_halt_or_done(session)
    assert result.outcome == FormTxOutcome.DELIVERY_UNCERTAIN
    assert result.receipt is None and result.safe_to_retry is False
    assert case.posts == ["/save"]
    assert reconciler.run_until_halt_or_done(session) is result
    assert (
        reconciler.apply_human_resolution(
            session,
            HumanResolutionInput(
                halt_id="new",
                action=HumanDecisionAction.CONFIRM_MODAL_ACTION,
                actor_id="synthetic-operator",
                idempotency_key="new",
            ),
        )
        is result
    )
    assert case.posts == ["/save"]


@pytest.mark.parametrize(
    "selector", ["#btn-next-applicant", "#modal-duplicate-confirm"]
)
def test_wizard_next_and_automatic_modal_confirmation_share_dispatch_guard(selector):
    from tests.test_form_tx_reconciler_and_receipts import (
        FakeStatefulFormSurface,
        _build_dossier,
        _build_pack,
    )

    surface = FakeStatefulFormSurface(trigger_duplicate_modal_on_commit=True)
    surface.calls = []
    original = surface.click

    def failing_click(target):
        surface.calls.append(target)
        original(target)
        if target == selector:
            raise RuntimeError("settle failed after transition")

    surface.click = failing_click
    session = ReconcilerSession(
        _build_pack(modal_policy="confirm_if_dossier_flag"),
        _build_dossier(has_pool="No", extra_facts={"allow_duplicate_contact": True}),
    )
    reconciler = FormReconciler(surface)
    result = reconciler.run_until_halt_or_done(session)
    assert surface.calls.count(selector) == 1
    if selector == "#modal-duplicate-confirm":
        assert surface.committed_record is not None
    assert_frozen(reconciler, session, surface, result)


def test_mutation_counter_read_failure_cannot_erase_a_possible_delivery():
    class CounterFailure(Surface):
        @property
        def mutation_count(self):
            if self.saved is not None:
                raise RuntimeError("counter unavailable after save")
            return self._mutations

        @mutation_count.setter
        def mutation_count(self, value):
            self._mutations = value

    surface = CounterFailure()
    pack, dossier = inputs()
    session = ReconcilerSession(pack, dossier)
    reconciler = FormReconciler(surface)
    result = reconciler.run_until_halt_or_done(session)
    assert surface.saved == "Yes"
    assert_frozen(reconciler, session, surface, result)


def test_resumed_commit_readback_value_error_is_uncertain_without_a_new_click():
    from tests.test_form_tx_reconciler_and_receipts import (
        FakeStatefulFormSurface,
        _build_dossier,
        _build_pack,
    )

    class FailingJudge:
        def evaluate(self, **kwargs):
            raise ValueError("readback failed after a prior dispatched commit")

    surface = FakeStatefulFormSurface(trigger_duplicate_modal_on_commit=True)
    reconciler = FormReconciler(surface, summary_judge=FailingJudge())
    session = ReconcilerSession(_build_pack(), _build_dossier(has_pool="No"))
    halted = reconciler.run_until_halt_or_done(session)
    assert halted.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    # A person confirmed the pending save on screen while the engine paused.
    surface.click("#modal-duplicate-confirm")
    assert surface.committed_record is not None
    surface.calls = []
    original = surface.click

    def observe_click(selector):
        surface.calls.append(selector)
        return original(selector)

    surface.click = observe_click
    result = reconciler.run_until_halt_or_done(session)
    assert not surface.calls
    assert_frozen(reconciler, session, surface, result)


@pytest.mark.parametrize("exception", [RuntimeError, asyncio.CancelledError])
def test_popup_context_cannot_suppress_child_failure_or_cancellation(
    exception, monkeypatch
):
    from openadapt_flow.form_tx import reconciler as module
    from openadapt_flow.form_tx.types import SummaryVerdict

    class SuppressingSurface(Surface):
        @contextmanager
        def open_popup_surface(self, *args, **kwargs):
            try:
                yield self
            except BaseException:
                pass

    class Judge:
        def evaluate(self, *, surface, **kwargs):
            with surface.open_popup_surface("#popup") as child:
                child.fill_text("#value", "Yes")
            return SummaryVerdict(passed=True, checked_count=1)

    issued = []
    monkeypatch.setattr(
        module, "issue_form_tx_receipt", lambda **kwargs: issued.append(kwargs)
    )
    surface = SuppressingSurface("fill_text", exception=exception)
    pack, dossier = inputs()
    pack = pack.model_copy(update={"bindings": (), "screens": ()})
    session = ReconcilerSession(pack, dossier)
    reconciler = FormReconciler(surface, summary_judge=Judge())
    if exception is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            reconciler.run_until_halt_or_done(session)
        result = session._terminal_result
    else:
        result = reconciler.run_until_halt_or_done(session)
    assert not issued
    assert_frozen(reconciler, session, surface, result)


@pytest.mark.parametrize("budget", ["commit", "transition"])
def test_existing_uncertain_budget_outcomes_also_block_run_and_human_resend(budget):
    surface = Surface()
    pack, dossier = inputs()
    if budget == "commit":
        pack = pack.model_copy(update={"max_commit_mutations": 0})
    session = ReconcilerSession(pack, dossier)
    reconciler = FormReconciler(
        surface, max_screen_transitions=0 if budget == "transition" else 25
    )
    result = reconciler.run_until_halt_or_done(session)
    reconciler.max_screen_transitions = 25
    assert_frozen(reconciler, session, surface, result)


def test_surface_retained_by_a_judge_is_rebound_to_the_current_session():
    from openadapt_flow.form_tx.judge import SummaryJudge

    class CapturingJudge(SummaryJudge):
        observed_surface = None

        def evaluate(self, *, surface, **kwargs):
            self.observed_surface = surface
            return super().evaluate(surface=surface, **kwargs)

    surface = Surface()
    pack, dossier = inputs()
    judge = CapturingJudge()
    first = FormReconciler(surface, summary_judge=judge)
    assert (
        first.run_until_halt_or_done(ReconcilerSession(pack, dossier)).outcome
        == FormTxOutcome.VERIFIED_DONE
    )
    second = FormReconciler(judge.observed_surface)
    session = ReconcilerSession(pack, dossier)
    surface.failure = "click"
    result = second.run_until_halt_or_done(session)
    assert_frozen(second, session, surface, result)


@pytest.mark.parametrize("refuted", [False, True])
def test_terminal_readback_outcome_cannot_resend_on_an_unchanged_commit_screen(refuted):
    from openadapt_flow.form_tx.types import SummaryVerdict

    class RefutingJudge:
        def evaluate(self, **kwargs):
            return SummaryVerdict(passed=False, checked_count=1, failed_keys=("value",))

    surface = Surface()
    pack, dossier = inputs()
    session = ReconcilerSession(pack, dossier)
    reconciler = FormReconciler(
        surface, **({"summary_judge": RefutingJudge()} if refuted else {})
    )
    result = reconciler.run_until_halt_or_done(session)
    assert result.outcome == (
        FormTxOutcome.READBACK_REFUTED if refuted else FormTxOutcome.VERIFIED_DONE
    )
    assert result.safe_to_retry is False and surface.mutation_count == 1
    assert reconciler.run_until_halt_or_done(session) is result
    assert surface.mutation_count == 1


@pytest.mark.parametrize(
    "outcome",
    [
        FormTxOutcome.VERIFIED_DONE,
        FormTxOutcome.READBACK_REFUTED,
        FormTxOutcome.DELIVERY_UNCERTAIN,
        FormTxOutcome.ABORTED_BY_HUMAN,
        FormTxOutcome.ESCALATED_TO_OWNER,
    ],
)
@pytest.mark.parametrize(
    "operations",
    [
        ("run", "run", "same_key", "new_key", "run"),
        ("new_key", "same_key", "run", "new_key", "same_key"),
    ],
)
def test_terminal_run_state_rejects_every_followup_without_surface_calls(
    outcome, operations
):
    from openadapt_flow.form_tx.types import SummaryVerdict

    class RefutingJudge:
        def evaluate(self, **kwargs):
            return SummaryVerdict(passed=False, checked_count=1, failed_keys=("value",))

    surface = Surface(
        "post_snapshot" if outcome == FormTxOutcome.DELIVERY_UNCERTAIN else None
    )
    pack, dossier = inputs()
    session = ReconcilerSession(pack, dossier)
    reconciler = FormReconciler(
        surface,
        **(
            {"summary_judge": RefutingJudge()}
            if outcome == FormTxOutcome.READBACK_REFUTED
            else {}
        ),
    )
    if outcome in (FormTxOutcome.ABORTED_BY_HUMAN, FormTxOutcome.ESCALATED_TO_OWNER):
        surface.value = "No"
        halt = reconciler.run_until_halt_or_done(session).halts[0]
        action = (
            HumanDecisionAction.ABORT_RUN
            if outcome == FormTxOutcome.ABORTED_BY_HUMAN
            else HumanDecisionAction.ESCALATE_TO_OWNER
        )
        decision = HumanResolutionInput(
            halt_id=halt.halt_id,
            action=action,
            actor_id="operator",
            idempotency_key="original",
        )
        result = reconciler.apply_human_resolution(session, decision)
    else:
        decision = HumanResolutionInput(
            halt_id="stale",
            action=HumanDecisionAction.PROVIDE_VALUE,
            actor_id="operator",
            idempotency_key="original",
            provided_value="Yes",
        )
        result = reconciler.run_until_halt_or_done(session)
    assert result.outcome == outcome
    calls, mutations = list(surface.calls), surface.mutation_count
    surface.failure = None
    for operation in operations:
        if operation == "run":
            observed = reconciler.run_until_halt_or_done(session)
        else:
            observed = reconciler.apply_human_resolution(
                session,
                decision
                if operation == "same_key"
                else decision.model_copy(update={"idempotency_key": "another"}),
            )
        assert observed is result
        assert surface.calls == calls and surface.mutation_count == mutations
    if "original" in session.seen_idempotency_keys:
        with pytest.raises(ValueError, match="Conflicting human decision"):
            reconciler.apply_human_resolution(
                session, decision.model_copy(update={"note": "changed payload"})
            )
        assert surface.calls == calls


@pytest.mark.parametrize(
    "paused",
    [
        FormTxOutcome.HALTED_NEEDS_HUMAN,
        FormTxOutcome.AUTH_REQUIRED,
        FormTxOutcome.VALIDATION_BLOCKED,
    ],
)
def test_explicit_paused_states_remain_resumable_until_a_terminal_result(paused):
    class PausingSurface(Surface):
        paused = True

        def _act(self, name, value="Yes"):
            if (
                self.paused
                and paused == FormTxOutcome.VALIDATION_BLOCKED
                and name == "click"
            ):
                # Genuine validation refusal: no save was applied. The unsafe
                # applied-write/banner sibling is tested separately below.
                self.calls.append(name)
                return
            return super()._act(name, value)

        def snapshot(self, **kwargs):
            snap = super().snapshot(**kwargs)
            if self.paused and paused == FormTxOutcome.AUTH_REQUIRED:
                return snap.model_copy(update={"auth_wall_present": True})
            if self.paused and paused == FormTxOutcome.VALIDATION_BLOCKED:
                return snap.model_copy(
                    update={"banner_errors": ("synthetic validation",)}
                )
            return snap

    surface = PausingSurface()
    if paused == FormTxOutcome.HALTED_NEEDS_HUMAN:
        surface.value = "No"
    pack, dossier = inputs()
    session = ReconcilerSession(pack, dossier)
    reconciler = FormReconciler(surface)
    result = reconciler.run_until_halt_or_done(session)
    assert result.outcome == paused and session._terminal_result is None
    surface.paused = False
    action = (
        HumanDecisionAction.PROVIDE_VALUE
        if paused == FormTxOutcome.HALTED_NEEDS_HUMAN
        else HumanDecisionAction.FIXED_ON_SCREEN
    )
    decision = HumanResolutionInput(
        halt_id=result.halts[0].halt_id,
        action=action,
        actor_id="operator",
        idempotency_key="resume",
        provided_value="Yes" if action == HumanDecisionAction.PROVIDE_VALUE else None,
    )
    done = reconciler.apply_human_resolution(session, decision)
    assert done.outcome == FormTxOutcome.VERIFIED_DONE
    assert surface.mutation_count == 1
    calls = list(surface.calls)
    assert reconciler.apply_human_resolution(session, decision) is done
    assert reconciler.run_until_halt_or_done(session) is done
    assert surface.calls == calls


def test_pending_post_commit_modal_is_resumable_without_authorizing_a_second_commit():
    from tests.test_form_tx_reconciler_and_receipts import (
        FakeStatefulFormSurface,
        _build_dossier,
        _build_pack,
    )

    surface = FakeStatefulFormSurface(trigger_duplicate_modal_on_commit=True)
    reconciler = FormReconciler(surface)
    session = ReconcilerSession(_build_pack(), _build_dossier(has_pool="No"))
    paused = reconciler.run_until_halt_or_done(session)
    assert paused.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert paused.safe_to_retry is False and session._terminal_result is None
    decision = HumanResolutionInput(
        halt_id=paused.halts[0].halt_id,
        action=HumanDecisionAction.CONFIRM_MODAL_ACTION,
        actor_id="operator",
        idempotency_key="confirm",
    )
    done = reconciler.apply_human_resolution(session, decision)
    assert done.outcome == FormTxOutcome.VERIFIED_DONE
    mutations = surface.mutation_count
    assert mutations == 1
    assert reconciler.apply_human_resolution(session, decision) is done
    assert reconciler.run_until_halt_or_done(session) is done
    assert surface.mutation_count == mutations


@pytest.mark.parametrize("refuted", [False, True])
def test_real_headless_terminal_readback_on_same_commit_screen_never_posts_twice(
    request, refuted
):
    from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface
    from tests.test_form_tx_frames import load, profile

    case = request.getfixturevalue("app")
    record_value = "No" if refuted else "Yes"
    frame = load(
        case,
        '''<!doctype html><body data-authenticated>
<label>Value<input id="value" value="Yes"></label>
<label>Independent read view<input id="readback" readonly value="'''
        + record_value
        + """"></label>
<button id="save" onclick="fetch('/save', {method:'POST'})">Save</button></body>""",
    )
    surface = PlaywrightFormSurface(frame, profile(1000))
    pack, dossier = inputs()
    readback = pack.bindings[0].model_copy(
        update={"anchor": FieldAnchorSpec(css_selector="#readback")}
    )
    pack = pack.model_copy(
        update={"summary_check": SummaryCheckSpec(field_readbacks=(readback,))}
    )
    session = ReconcilerSession(pack, dossier)
    reconciler = FormReconciler(surface)
    result = reconciler.run_until_halt_or_done(session)
    assert result.outcome == (
        FormTxOutcome.READBACK_REFUTED if refuted else FormTxOutcome.VERIFIED_DONE
    )
    assert frame.locator("#save").is_visible() and case.posts == ["/save"]
    assert (result.receipt is None) == refuted
    for key in ("one", "one", "different"):
        assert reconciler.run_until_halt_or_done(session) is result
        assert (
            reconciler.apply_human_resolution(
                session,
                HumanResolutionInput(
                    halt_id="stale",
                    action=HumanDecisionAction.PROVIDE_VALUE,
                    actor_id="operator",
                    idempotency_key=key,
                    provided_value="Yes",
                ),
            )
            is result
        )
    assert case.posts == ["/save"]


def test_unsafe_paused_validation_cannot_reopen_a_commit_after_a_write_was_observed():
    class AppliedThenBlocked(Surface):
        blocked = True

        def snapshot(self, **kwargs):
            snap = super().snapshot(**kwargs)
            if self.saved is not None and self.blocked:
                return snap.model_copy(
                    update={"banner_errors": ("post-write validation",)}
                )
            return snap

    surface = AppliedThenBlocked()
    pack, dossier = inputs()
    reconciler = FormReconciler(surface)
    session = ReconcilerSession(pack, dossier)
    result = reconciler.run_until_halt_or_done(session)
    assert result.outcome == FormTxOutcome.VALIDATION_BLOCKED
    assert result.safe_to_retry is False and surface.mutation_count == 1
    calls = list(surface.calls)
    surface.blocked = False
    assert (
        reconciler.apply_human_resolution(
            session,
            HumanResolutionInput(
                halt_id=result.halts[0].halt_id,
                action=HumanDecisionAction.FIXED_ON_SCREEN,
                actor_id="operator",
                idempotency_key="cleared-banner",
            ),
        )
        is result
    )
    assert reconciler.run_until_halt_or_done(session) is result
    assert surface.calls == calls and surface.mutation_count == 1
