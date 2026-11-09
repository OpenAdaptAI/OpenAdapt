"""Interruption classes: cleared only when provably read-only.

The four conditions (``docs/INTERRUPTION_RECOVERY.md``): identity by text and
control, a non-committing action, no write seen by any watcher, and the
expected screen afterwards. Each test below breaks one and expects a halt,
or holds all four and expects the run to complete with the dismissal in its
report. Backend and vision are faked; the real-browser cases are in
``tests/test_interruptions_browser.py``.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from openadapt_flow.interruptions import (
    InterruptionClassError,
    LearnRefused,
    interruption_from_data,
    is_non_committing_label,
    learn_interruption,
    load_interruption_classes,
    save_interruption_class,
)
from openadapt_flow.ir import (
    Anchor,
    Interstitial,
    Predicate,
    PredicateKind,
    StructuralHandle,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.runtime.effect_watch import watch_effects
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, Match, click_step, make_png
from tests.test_state_dependency import SettleVision

SESSION = {
    "name": "session expiry warning",
    "texts": ["Your session will expire"],
    "dismiss": {"control": {"role": "button", "name": "Stay signed in"}},
}


@pytest.fixture()
def bundle(tmp_path):
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "templates").mkdir(parents=True)
    (bundle_dir / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    return bundle_dir


@pytest.fixture()
def run_dir(tmp_path):
    return tmp_path / "run"


class _Fence:
    def __init__(self, refusal=None, attempts=()):
        self.refusal = refusal
        self.attempts = list(attempts)


class _Guard:
    def __init__(self, refusal=None):
        self.refusal = refusal
        self.violations: list[str] = []
        self.events: list[str] = []


class _LoggingFence(_Fence):
    def __init__(self, requests):
        super().__init__()
        self.requests = list(requests)


class WatchedBackend(FakeBackend):
    """A structural backend with a scripted write fence."""

    def __init__(
        self,
        *,
        fence=True,
        refusal=None,
        writes=(),
        whole="Your session will expire in 5 minutes. Stay signed in",
        title="",
        live=None,
        can_read_live=True,
        guard_refusal=None,
        guard_violations=(),
        can_guard=True,
    ):
        super().__init__()
        self._guard_refusal = guard_refusal
        self._guard_violations = list(guard_violations)
        self.guarded: list[bool] = []
        self._guard_up = False
        if not can_guard:
            self.structural_label_guard = None  # type: ignore[assignment]
        # The live control's (role, name); None means the declared one.
        self._live = live
        self.live_checks: list[tuple[str, str]] = []
        if not can_read_live:
            self.structural_control_is = None  # type: ignore[assignment]
        self._whole = whole
        self._title = title
        self._fence = fence
        self._refusal = refusal
        self._writes = list(writes)
        self.fenced: list[bool] = []
        self._up = False
        if not fence:
            self.write_fence = None  # type: ignore[assignment]

    def interruption_text(self, locator, texts):
        if self._whole is None:
            return None
        return {"text": self._whole, "title": self._title, "window": ""}

    def structural_control_is(self, locator, handle, *, role, name):
        self.live_checks.append((role, name))
        live = self._live or (locator.role, locator.name)
        return tuple(live) == (role, name)

    def locate_structural(self, locator):
        return StructuralHandle(
            point=(15, 15), region=(10, 10, 10, 10), target_fingerprint="a" * 64
        )

    @contextmanager
    def structural_label_guard(self, locator, handle, *, role, name):
        report = _Guard(refusal=self._guard_refusal)
        self._guard_up = self._guard_refusal is None
        try:
            yield report
        finally:
            self._guard_up = False
            if self._guard_refusal is None:
                report.violations.extend(self._guard_violations)

    def act_structural(self, locator, handle, *, double=False):
        self.guarded.append(self._guard_up)
        self.fenced.append(self._up)
        self.actions.append(("act_structural", locator.name))

    def press(self, key):
        self.fenced.append(self._up)
        super().press(key)

    @contextmanager
    def write_fence(self):  # type: ignore[override]
        report = _Fence(refusal=self._refusal)
        self._up = self._refusal is None
        try:
            yield report
        finally:
            self._up = False
        if self._refusal is None:
            report.attempts.extend(self._writes)


class Verifier:
    """A system-of-record read-back whose records a dismissal may change."""

    substrate = "fake"

    def __init__(self, change=False, reachable=True):
        self.reads = 0
        self.change = change
        self.reachable = reachable

    def capture_pre_state(self, context=None):
        from openadapt_flow.runtime.effects.effect import EffectState

        self.reads += 1
        records = [{"id": 1}]
        if self.change and self.reads > 1:
            records.append({"id": 2})
        return EffectState(substrate="fake", reachable=self.reachable, records=records)


def _vision(text="Your session will expire", target=True):
    vision = SettleVision(settled=True)
    vision.text_results = {text: [Match((10, 10), (0, 0, 5, 5)), None, None]}
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))] if target else []
    return vision


def _run(backend, vision, bundle, run_dir, classes, **kw):
    workflow = Workflow(name="wf", steps=[click_step()], interstitials=classes)
    return Replayer(backend, vision=vision, poll_interval_s=0.005, **kw).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )


# -- (a) and (b): what a class may be -------------------------------------------


@pytest.mark.parametrize(
    "label",
    ["Close", "Dismiss", "Not now", "Stay signed in", "Continue session", "Got it"],
)
def test_non_committing_labels(label):
    assert is_non_committing_label(label)


@pytest.mark.parametrize(
    "label",
    [
        "OK",
        "Continue",
        "Yes",
        "Accept",
        "Accept all",
        "Save",
        "Submit",
        "Delete",
        "Close account",
        "Confirm",
        "Sign out",
        "",
    ],
)
def test_committing_or_ambiguous_labels_are_outside_the_closed_set(label):
    assert not is_non_committing_label(label)


def test_a_class_whose_control_may_commit_is_refused():
    entry = dict(SESSION, dismiss={"control": {"role": "button", "name": "OK"}})
    with pytest.raises(InterruptionClassError, match="non-committing"):
        interruption_from_data(entry)


def test_a_class_found_only_by_position_is_refused():
    anchor = Anchor(
        template="",
        structural=StructuralLocator(role="button"),
        region=(0, 0, 10, 10),
        click_point=(5, 5),
        ocr_text="Close",
    )
    with pytest.raises(ValueError, match="identity|structural"):
        Interstitial(
            name="x",
            detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="x"),
            dismiss_anchor=anchor,
            risk="reversible",
            consequential=False,
            clearance=Predicate(kind=PredicateKind.TEXT_ABSENT, text="x"),
            effect_watch=True,
        )


def test_an_existing_interstitial_serializes_as_before():
    it = Interstitial(
        name="rate us",
        detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="rate us"),
        dismiss_key="Escape",
        risk="reversible",
        consequential=False,
        clearance=Predicate(kind=PredicateKind.TEXT_ABSENT, text="rate us"),
    )
    assert "effect_watch" not in it.model_dump()


def test_class_file_round_trip_and_bad_files(tmp_path):
    path = tmp_path / "interruptions.yaml"
    save_interruption_class(path, interruption_from_data(SESSION))
    tour = {"name": "tour", "texts": ["Take a tour"], "dismiss": {"key": "Escape"}}
    save_interruption_class(path, interruption_from_data(tour))
    classes = load_interruption_classes(path)
    assert [c.name for c in classes] == ["session expiry warning", "tour"]
    assert all(c.effect_watch for c in classes)
    path.write_text(
        "interruptions:\n  - {name: a, texts: [a], dismiss: {key: Enter}}\n"
    )
    with pytest.raises(InterruptionClassError):
        load_interruption_classes(path)


# -- (c) and (d) at run time ------------------------------------------------------


def test_a_watched_dismissal_clears_and_is_reported(bundle, run_dir):
    backend = WatchedBackend()
    report = _run(
        backend, _vision(), bundle, run_dir, [interruption_from_data(SESSION)]
    )
    assert report.success is True, report.results[0].error
    assert backend.actions[0] == ("act_structural", "Stay signed in")
    assert backend.fenced == [True]
    event = report.results[0].interstitial_actions[0]
    assert event.ok and event.effect_watchers == ["network"]
    assert event.write_attempts == []
    assert event.control_label == "Stay signed in"


def test_a_dismissal_that_tries_to_write_halts(bundle, run_dir):
    backend = WatchedBackend(writes=["POST /session/end"])
    report = _run(
        backend, _vision(), bundle, run_dir, [interruption_from_data(SESSION)]
    )
    assert report.success is False
    result = report.results[0]
    assert result.safety_halt is True
    assert "tried to write" in (result.error or "")
    assert ("click", 110, 105, False) not in backend.actions
    assert result.interstitial_actions[0].write_attempts


def test_a_run_that_cannot_watch_never_dismisses(bundle, run_dir):
    backend = WatchedBackend(fence=False)
    report = _run(
        backend, _vision(), bundle, run_dir, [interruption_from_data(SESSION)]
    )
    assert report.success is False
    assert "no way to see a write" in (report.results[0].error or "")
    assert backend.actions == []


def test_a_fence_that_cannot_go_up_never_dismisses(bundle, run_dir):
    backend = WatchedBackend(refusal="a worker runs on this page")
    report = _run(
        backend, _vision(), bundle, run_dir, [interruption_from_data(SESSION)]
    )
    assert report.success is False
    assert backend.actions == []


def test_record_read_back_watches_a_desktop_dismissal(bundle, run_dir):
    backend = WatchedBackend(fence=False)
    verifier = Verifier()
    tour = {
        "name": "tour",
        "texts": ["Your session will expire"],
        "dismiss": {"key": "Escape"},
    }
    report = _run(
        backend,
        _vision(),
        bundle,
        run_dir,
        [interruption_from_data(tour)],
        effect_verifier=verifier,
    )
    event = report.results[0].interstitial_actions[0]
    assert event.effect_watchers == ["record"]
    assert event.ok
    assert backend.actions[0] == ("press", "Escape")


def test_record_change_during_a_dismissal_halts(bundle, run_dir):
    backend = WatchedBackend(fence=False)
    report = _run(
        backend,
        _vision(),
        bundle,
        run_dir,
        [interruption_from_data(SESSION)],
        effect_verifier=Verifier(change=True),
    )
    assert report.success is False
    assert report.results[0].safety_halt is True
    assert "system of record changed" in (report.results[0].error or "")


def test_dismissal_onto_an_unexpected_screen_halts_at_identity(bundle, run_dir):
    backend = WatchedBackend()
    report = _run(
        backend,
        _vision(target=False),
        bundle,
        run_dir,
        [interruption_from_data(SESSION)],
    )
    assert report.success is False
    assert report.results[0].interstitial_actions[0].ok
    assert not any(a[0] == "click" for a in backend.actions)


def test_an_unknown_dialog_halts(bundle, run_dir):
    backend = WatchedBackend()
    vision = _vision(text="Something else", target=False)
    report = _run(backend, vision, bundle, run_dir, [interruption_from_data(SESSION)])
    assert report.success is False
    assert report.results[0].interstitial_actions == []
    assert backend.actions == []


# -- learning a class from a halt --------------------------------------------------


def test_learning_proves_the_dismissal_before_keeping_it():
    backend = WatchedBackend()
    on_screen = {"Your session will expire": True}

    def shows(text):
        return on_screen.get(text, False)

    def settle():
        on_screen["Your session will expire"] = False

    learned = learn_interruption(
        name="session expiry warning",
        texts=["Your session will expire"],
        control={"role": "button", "name": "Stay signed in"},
        backend=backend,
        shows=shows,
        settle=settle,
    )
    assert learned.watchers == ("network",)
    assert learned.interstitial.effect_watch is True


def test_learning_refuses_a_dismissal_that_writes():
    backend = WatchedBackend(writes=["POST /x"])
    with pytest.raises(LearnRefused, match="tried to write"):
        learn_interruption(
            name="n",
            texts=["t"],
            control={"role": "button", "name": "Close"},
            backend=backend,
            shows=lambda text: True,
        )


def test_learning_refuses_a_committing_control_without_clicking():
    backend = WatchedBackend()
    with pytest.raises(InterruptionClassError):
        learn_interruption(
            name="n",
            texts=["t"],
            control={"role": "button", "name": "Confirm"},
            backend=backend,
            shows=lambda text: True,
        )
    assert backend.actions == []


def test_watch_needs_a_watcher():
    with watch_effects(object(), None) as report:
        assert report.refusal is not None
    assert not report.held


# -- the whole dialog decides, never the button word ------------------------------


@pytest.mark.parametrize(
    "whole",
    [
        "Your session will expire. Apply template to patient? Stay signed in",
        "Your session will expire. Keep my changes? Stay signed in",
        "Your session will expire. Unsaved record will be discarded. Stay signed in",
    ],
)
def test_a_business_question_in_disguise_halts_before_any_input(bundle, run_dir, whole):
    backend = WatchedBackend(whole=whole)
    report = _run(
        backend, _vision(), bundle, run_dir, [interruption_from_data(SESSION)]
    )
    assert report.success is False
    assert "business question" in (report.results[0].error or "")
    assert backend.actions == []


def test_a_surface_that_cannot_read_the_whole_dialog_halts(bundle, run_dir):
    backend = WatchedBackend(whole=None)
    report = _run(
        backend, _vision(), bundle, run_dir, [interruption_from_data(SESSION)]
    )
    assert report.success is False
    assert "can't be proven" in (report.results[0].error or "")
    assert backend.actions == []


def test_a_dialog_with_much_more_to_say_is_not_the_class(bundle, run_dir):
    backend = WatchedBackend(
        whole="Your session will expire. " + "Lorem ipsum dolor sit amet. " * 12
    )
    report = _run(
        backend, _vision(), bundle, run_dir, [interruption_from_data(SESSION)]
    )
    assert report.success is False
    assert backend.actions == []


def test_a_declared_title_must_match(bundle, run_dir):
    entry = dict(SESSION, title="Session timeout")
    backend = WatchedBackend(title="Unsaved changes")
    report = _run(backend, _vision(), bundle, run_dir, [interruption_from_data(entry)])
    assert report.success is False
    assert backend.actions == []


def test_a_class_that_mentions_business_content_is_refused():
    entry = dict(SESSION, texts=["Apply template to patient"])
    with pytest.raises(InterruptionClassError, match="business question"):
        interruption_from_data(entry)


# -- the clicked control is the declared one --------------------------------------


def test_a_selector_that_resolves_to_another_control_halts_before_any_input(
    bundle, run_dir
):
    # Declared "Close", but the selector resolves to a "Sign out" button.
    entry = dict(
        SESSION,
        dismiss={"control": {"selector": "#danger", "role": "button", "name": "Close"}},
    )
    backend = WatchedBackend(
        whole="Your session will expire in 5 minutes. Sign out",
        live=("button", "Sign out"),
    )
    report = _run(backend, _vision(), bundle, run_dir, [interruption_from_data(entry)])
    assert report.success is False
    assert "not the button 'Close'" in (report.results[0].error or "")
    assert report.results[0].interstitial_actions == []
    assert backend.actions == []
    assert backend.live_checks == [("button", "Close")]


def test_a_backend_that_cannot_read_the_live_control_halts(bundle, run_dir):
    backend = WatchedBackend(can_read_live=False)
    report = _run(
        backend, _vision(), bundle, run_dir, [interruption_from_data(SESSION)]
    )
    assert report.success is False
    assert "can't read the live control" in (report.results[0].error or "")
    assert backend.actions == []


def test_the_live_control_is_checked_again_at_delivery(bundle, run_dir):
    # The label changes between the first check and the click.
    backend = WatchedBackend()
    answers = iter([True, False])
    backend.structural_control_is = (  # type: ignore[method-assign]
        lambda locator, handle, *, role, name: next(answers)
    )
    report = _run(
        backend, _vision(), bundle, run_dir, [interruption_from_data(SESSION)]
    )
    assert report.success is False
    assert not any(a[0] == "act_structural" for a in backend.actions)


def test_a_class_without_a_declared_role_and_name_is_refused():
    entry = dict(SESSION, dismiss={"control": {"selector": "#x", "name": "Close"}})
    with pytest.raises(InterruptionClassError, match="role and accessible name"):
        interruption_from_data(entry)


def test_learning_refuses_a_control_whose_live_label_differs_without_clicking():
    backend = WatchedBackend(live=("button", "Sign out"))
    with pytest.raises(LearnRefused, match="not the button 'Close'"):
        learn_interruption(
            name="n",
            texts=["t"],
            control={"selector": "#danger", "role": "button", "name": "Close"},
            backend=backend,
            shows=lambda text: True,
        )
    assert backend.actions == []


# -- the label holds for the whole click ------------------------------------------


def test_a_watched_dismissal_is_clicked_under_the_click_guard(bundle, run_dir):
    backend = WatchedBackend()
    report = _run(
        backend, _vision(), bundle, run_dir, [interruption_from_data(SESSION)]
    )
    assert report.success is True, report.results[0].error
    assert backend.guarded == [True]


def test_a_backend_without_a_click_guard_never_clicks(bundle, run_dir):
    backend = WatchedBackend(can_guard=False)
    report = _run(
        backend, _vision(), bundle, run_dir, [interruption_from_data(SESSION)]
    )
    assert report.success is False
    assert "for the whole click" in (report.results[0].error or "")
    assert not any(a[0] == "act_structural" for a in backend.actions)


def test_a_click_guard_that_cannot_arm_never_clicks(bundle, run_dir):
    backend = WatchedBackend(guard_refusal="this document has no click guard")
    report = _run(
        backend, _vision(), bundle, run_dir, [interruption_from_data(SESSION)]
    )
    assert report.success is False
    assert not any(a[0] == "act_structural" for a in backend.actions)
    event = report.results[0].interstitial_actions[0]
    assert event.delivered is False
    assert "no click guard" in (event.error or "")


def test_a_label_change_during_the_click_halts_as_uncertain_delivery(bundle, run_dir):
    backend = WatchedBackend(
        guard_violations=["pointerdown: the control or its label changed"]
    )
    report = _run(
        backend, _vision(), bundle, run_dir, [interruption_from_data(SESSION)]
    )
    result = report.results[0]
    assert report.success is False
    assert result.safety_halt is True
    assert result.delivery_uncertainty is not None
    assert result.delivery_uncertainty.cause_type == "LabelChangedDuringClick"
    event = result.interstitial_actions[0]
    assert event.delivered is True
    assert event.label_violations == ["pointerdown: the control or its label changed"]


def test_learning_refuses_when_the_label_changes_during_the_click():
    backend = WatchedBackend(
        guard_violations=["click: the control or its label changed"]
    )
    with pytest.raises(LearnRefused, match="label changed during the click"):
        learn_interruption(
            name="n",
            texts=["t"],
            control={"role": "button", "name": "Close"},
            backend=backend,
            shows=lambda text: False if text == "t" and backend.actions else True,
        )


def test_learning_refuses_without_a_click_guard():
    backend = WatchedBackend(can_guard=False)
    with pytest.raises(LearnRefused, match="for the whole click"):
        learn_interruption(
            name="n",
            texts=["t"],
            control={"role": "button", "name": "Close"},
            backend=backend,
            shows=lambda text: True,
        )
    assert backend.actions == []


# -- the report agrees with the fence's request log -------------------------------


def test_a_write_in_the_request_log_is_never_reported_as_no_write():
    from openadapt_flow.runtime.effect_watch import reconcile_requests

    assert reconcile_requests(["GET /ping"], []) == []
    assert (
        reconcile_requests(
            ["POST /logout"], ["network: POST /logout; it was cancelled"]
        )
        == []
    )
    assert reconcile_requests(["POST /logout"], []) == [
        "network: POST /logout was sent while the fence was up"
    ]
    assert reconcile_requests(["? (a request that could not be read)"], [])


def test_the_fence_log_reaches_the_report(bundle, run_dir):
    class Logging(WatchedBackend):
        @contextmanager
        def write_fence(self):  # type: ignore[override]
            report = _LoggingFence(["GET /ping", "PUT /x"])
            self._up = True
            try:
                yield report
            finally:
                self._up = False

    backend = Logging()
    report = _run(
        backend, _vision(), bundle, run_dir, [interruption_from_data(SESSION)]
    )
    result = report.results[0]
    assert report.success is False
    assert result.safety_halt is True
    event = result.interstitial_actions[0]
    assert event.requests_seen == ["GET /ping", "PUT /x"]
    assert event.write_attempts == ["network: PUT /x was sent while the fence was up"]
