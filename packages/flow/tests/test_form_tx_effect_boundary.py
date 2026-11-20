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
        assert_frozen(reconciler, session, surface, session._effect_failure_result)
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
        result = session._effect_failure_result
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
