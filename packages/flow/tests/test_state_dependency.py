"""State-dependency robustness (docs/LIMITS.md "state dependency").

Replay must not assume an identical starting state. These tests pin three
model-free defenses, all added around the unchanged hardened action leaf:

* ``wait_settled_result`` exposes whether the screen actually settled, so a
  caller can refuse to act on a stale / mid-transition frame (the flagged
  "wait_settled proceeds on stale frames" bug);
* the replayer's opt-in ``require_settled`` readiness gate HALTs gracefully on a
  screen that never settles (a slow load), rather than acting on it;
* a KNOWN reversible, non-consequential interstitial at a step's entry is
  dismissed through an audited action, then re-settled and checked against an
  explicit visual clearance postcondition; anything else HALTs before the step.

Backend and vision are faked (reused from test_replayer) -- no Playwright, no
OCR stack, ZERO model calls.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Interstitial,
    Postcondition,
    PostconditionKind,
    Predicate,
    PredicateKind,
    ProgramGraph,
    RunReport,
    State,
    StateKind,
    StructuralHandle,
    StructuralLocator,
    Transition,
    Workflow,
)
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.vision import wait_settled, wait_settled_result
from tests.test_replayer import (
    FakeBackend,
    FakeVision,
    Match,
    click_step,
    make_png,
)

VIEWPORT = (300, 200)


# -- fixtures ----------------------------------------------------------------


@pytest.fixture()
def bundle(tmp_path):
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "templates").mkdir(parents=True)
    (bundle_dir / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    return bundle_dir


@pytest.fixture()
def run_dir(tmp_path):
    return tmp_path / "run"


class _AnimatedBackend(FakeBackend):
    """A backend whose frame changes every screenshot -- it never settles."""

    def __init__(self, viewport=VIEWPORT):
        super().__init__(viewport=viewport)
        self._tick = 0

    def screenshot(self):
        # Move a large black bar across the frame each poll so the PERCEPTUAL
        # hash actually differs frame to frame (a flat shade change would hash
        # identically and count as settled).
        self._tick += 1
        img = Image.new("RGB", self._viewport, (240, 240, 240))
        w, h = self._viewport
        x = (self._tick * 37) % max(1, w - 40)
        for px in range(x, min(x + 40, w)):
            for py in range(h):
                img.putpixel((px, py), (0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


class SettleVision(FakeVision):
    """FakeVision plus the readiness-aware ``wait_settled_result`` the real
    vision module exposes. ``settled`` is scripted so a test can force the
    never-settling (slow-load) path deterministically."""

    def __init__(self, settled: bool | list[bool] = True):
        super().__init__()
        self.settled = [settled] if isinstance(settled, bool) else list(settled)
        self.result_calls = 0
        self.timeouts: list[float] = []

    def wait_settled_result(
        self, backend, *, interval_s=0.1, stable_frames=2, timeout_s=3.0
    ):
        from openadapt_flow.vision import SettleResult

        outcome = self.settled[min(self.result_calls, len(self.settled) - 1)]
        self.result_calls += 1
        self.timeouts.append(timeout_s)
        png = backend.screenshot()
        return SettleResult(
            png=png,
            settled=outcome,
            stable_frames=stable_frames if outcome else 1,
            required_frames=stable_frames,
            elapsed_s=0.0,
        )


# -- wait_settled_result: the settled signal is no longer swallowed ----------


def test_wait_settled_result_reports_settled_on_a_stable_screen():
    backend = FakeBackend()  # returns one fixed frame forever
    result = wait_settled_result(backend, interval_s=0.001, stable_frames=2)
    assert result.settled is True
    assert result.png == backend.screenshot()


def test_wait_settled_result_reports_unsettled_on_a_changing_screen():
    backend = _AnimatedBackend()
    result = wait_settled_result(
        backend, interval_s=0.001, stable_frames=3, timeout_s=0.05
    )
    assert result.settled is False
    assert result.stable_frames < result.required_frames


def test_wait_settled_wrapper_returns_frame_and_warns(caplog):
    """The bare wrapper still returns the most recent frame on timeout, but the
    not-settled fact is now recoverable (via wait_settled_result) and logged."""
    backend = _AnimatedBackend()
    import logging

    with caplog.at_level(logging.WARNING):
        png = wait_settled(backend, interval_s=0.001, stable_frames=3, timeout_s=0.05)
    assert isinstance(png, bytes)
    assert any("did not settle" in r.message for r in caplog.records)


# -- require_settled readiness gate: never act on a mid-transition frame ------


def test_require_settled_halts_on_a_screen_that_never_settles(bundle, run_dir):
    """A slow-loading / perpetually-animating screen HALTs gracefully instead
    of acting on a stale frame -- and never issues the click."""
    vision = SettleVision(settled=False)
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = _AnimatedBackend()
    wf = Workflow(name="wf", steps=[click_step()])
    report = Replayer(
        backend,
        vision=vision,
        poll_interval_s=0.005,
        require_settled=True,
        settle_readiness_timeout_s=0.05,
    ).run(wf, bundle_dir=bundle, run_dir=run_dir)
    assert report.success is False
    assert "starting state not ready" in report.results[0].error
    assert "bounded per-step wait_until readiness predicate" in report.results[0].error
    assert "require_settled=False" not in report.results[0].error
    assert backend.actions == []  # no click on the un-ready screen
    assert vision.result_calls == 1  # the settle primitive owns the full bound
    assert vision.timeouts == [0.05]


def test_require_settled_proceeds_once_the_screen_settles(bundle, run_dir):
    vision = SettleVision(settled=True)
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = FakeBackend()
    wf = Workflow(name="wf", steps=[click_step()])
    report = Replayer(
        backend,
        vision=vision,
        poll_interval_s=0.005,
        require_settled=True,
        settle_readiness_timeout_s=0.05,
    ).run(wf, bundle_dir=bundle, run_dir=run_dir)
    assert report.success is True
    assert ("click", 110, 105, False) in backend.actions
    assert vision.result_calls == 1


def test_require_settled_off_by_default_preserves_behavior(bundle, run_dir):
    """Default (off): even a never-settling screen acts exactly as before, so
    inherently-animated UIs are not over-halted and behavior is unchanged."""
    vision = SettleVision(settled=False)
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = _AnimatedBackend()
    wf = Workflow(name="wf", steps=[click_step()])
    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert any(a[0] == "click" for a in backend.actions)


def test_require_settled_halts_when_facade_cannot_report(bundle, run_dir):
    """Opting into readiness cannot silently degrade to proceed-anyway."""
    vision = FakeVision()  # no wait_settled_result
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = FakeBackend()
    wf = Workflow(name="wf", steps=[click_step()])
    report = Replayer(
        backend, vision=vision, poll_interval_s=0.005, require_settled=True
    ).run(wf, bundle_dir=bundle, run_dir=run_dir)
    assert report.success is False
    assert "cannot report whether the screen settled" in report.results[0].error
    assert backend.actions == []


def test_require_settled_rejects_an_invalid_timeout():
    with pytest.raises(ValueError, match="settle_readiness_timeout_s"):
        Replayer(
            FakeBackend(),
            vision=SettleVision(),
            require_settled=True,
            settle_readiness_timeout_s=0,
        )


def test_program_exception_handler_cannot_bypass_readiness_halt(bundle, run_dir):
    """A never-settled entry frame is a safety refusal, not a recoverable task
    exception that an ``on_exception`` edge may convert into success."""

    step = click_step()
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
            "recover": State(id="recover", kind=StateKind.TERMINAL, outcome="success"),
            "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
        },
    )
    backend = FakeBackend()
    report = Replayer(
        backend,
        vision=SettleVision(settled=False),
        require_settled=True,
        settle_readiness_timeout_s=0.05,
    ).run(
        Workflow(name="program", program=program),
        bundle_dir=bundle,
        run_dir=run_dir,
    )

    assert report.success is False
    assert report.terminal_outcome == "halt"
    assert backend.actions == []
    assert report.results[0].safety_halt is True
    assert report.results[0].exception_handled is False


# -- interstitials: dismiss-if-known, else halt gracefully -------------------


def _present_then_absent(match: Match) -> list:
    """Scripted find_text/text_present: present on the first probe, absent
    after (i.e. once the interstitial is dismissed)."""
    return [match, None]


def _cleared(text: str) -> Predicate:
    return Predicate(kind=PredicateKind.TEXT_ABSENT, text=text)


def test_known_interstitial_dismissed_by_key_then_step_runs(bundle, run_dir):
    vision = SettleVision(settled=True)
    vision.text_results = {
        "rate us": _present_then_absent(Match((10, 10), (0, 0, 5, 5)))
    }
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = FakeBackend()
    wf = Workflow(
        name="wf",
        steps=[click_step()],
        interstitials=[
            Interstitial(
                name="satisfaction survey",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="rate us"),
                dismiss_key="Escape",
                risk="reversible",
                consequential=False,
                clearance=_cleared("rate us"),
            )
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    # The overlay was dismissed BEFORE the target click.
    assert backend.actions[0] == ("press", "Escape")
    assert ("click", 110, 105, False) in backend.actions
    dismissal = report.results[0].interstitial_actions[0]
    assert dismissal.action == "key"
    assert dismissal.key == "Escape"
    assert dismissal.risk == "reversible"
    assert dismissal.consequential is False
    assert dismissal.attempted is True
    assert dismissal.delivered is True
    assert dismissal.clearance_ok is True
    assert dismissal.ok is True
    saved = RunReport.model_validate_json((run_dir / "report.json").read_text())
    saved_dismissal = saved.results[0].interstitial_actions[0]
    assert saved_dismissal == dismissal
    assert saved_dismissal.expected_clearance.kind == PredicateKind.TEXT_ABSENT


def test_known_interstitial_dismissed_by_anchor_click(bundle, run_dir):
    vision = SettleVision(settled=True)
    vision.text_results = {
        "What's New": _present_then_absent(Match((10, 10), (0, 0, 5, 5)))
    }
    # First find_template resolves the dismiss button; second resolves the step.
    vision.template_results = [
        Match((200, 20), (190, 10, 30, 20)),  # "Continue" button
        Match((110, 105), (100, 100, 50, 20)),  # the step's target
    ]
    backend = FakeBackend()
    wf = Workflow(
        name="wf",
        steps=[click_step()],
        interstitials=[
            Interstitial(
                name="release notice",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="What's New"),
                dismiss_anchor=Anchor(
                    template="templates/btn.png",
                    region=(190, 10, 30, 20),
                    click_point=(200, 20),
                    ocr_text="Close",
                ),
                risk="reversible",
                consequential=False,
                clearance=_cleared("What's New"),
            )
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions[0] == ("click", 200, 20, False)  # dismiss first
    assert ("click", 110, 105, False) in backend.actions  # then the target
    dismissal = report.results[0].interstitial_actions[0]
    assert dismissal.action == "click"
    assert dismissal.resolution is not None
    assert dismissal.delivered is True
    assert dismissal.clearance_ok is True
    assert dismissal.ok is True


@pytest.mark.parametrize(
    ("kind", "state_key", "before", "after"),
    [
        (PostconditionKind.URL_CHANGED, "url", "https://before", "https://after"),
        (PostconditionKind.TITLE_CHANGED, "page_title", "before", "after"),
        (PostconditionKind.NEW_TAB_OPENED, "page_count", 1, 2),
    ],
)
def test_interstitial_change_cannot_satisfy_action_structural_postcondition(
    bundle, run_dir, kind, state_key, before, after
):
    """The structural baseline belongs to the workflow action, not Escape."""

    class StructuralStateBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.state = before

        def __getattr__(self, name):
            if name == state_key:
                return self.state
            raise AttributeError(name)

        def press(self, key):
            super().press(key)
            self.state = after

    vision = SettleVision()
    vision.text_results = {"release note": [Match((10, 10), (0, 0, 5, 5)), None, None]}
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = StructuralStateBackend()
    step = click_step()
    step.expect = [Postcondition(kind=kind, timeout_s=0)]
    workflow = Workflow(
        name="wf",
        steps=[step],
        interstitials=[
            Interstitial(
                name="release note",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="release note"),
                dismiss_key="Escape",
                risk="reversible",
                consequential=False,
                clearance=_cleared("release note"),
            )
        ],
    )

    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )

    assert report.success is False
    assert report.results[0].postconditions_ok is False
    assert backend.actions == [
        ("press", "Escape"),
        ("click", 110, 105, False),
    ]


def test_resolution_change_cannot_satisfy_action_structural_postcondition(
    bundle, run_dir
):
    """Observation during target resolution is not an action outcome."""

    class ResolutionChangesTitleBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.page_title = "before"

        def locate_structural(self, locator):
            self.page_title = "after"
            return StructuralHandle(
                point=(110, 105),
                region=(100, 100, 50, 20),
                target_fingerprint="a" * 64,
            )

    backend = ResolutionChangesTitleBackend()
    step = click_step()
    assert step.anchor is not None
    step.anchor.structural = StructuralLocator(role="button", name="Submit")
    step.expect = [Postcondition(kind=PostconditionKind.TITLE_CHANGED, timeout_s=0)]

    report = Replayer(backend, vision=SettleVision(), poll_interval_s=0.005).run(
        Workflow(name="wf", steps=[step]), bundle_dir=bundle, run_dir=run_dir
    )

    assert report.success is False
    assert report.results[0].postconditions_ok is False
    assert backend.actions == [("click", 110, 105, False)]


def test_structural_only_interstitial_uses_native_locate_and_act(bundle, run_dir):
    class StructuralBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.locators = []

        def locate_structural(self, locator):
            self.locators.append(locator)
            return StructuralHandle(
                point=(15, 15),
                region=(10, 10, 10, 10),
                target_fingerprint="a" * 64,
            )

        def act_structural(self, locator, handle, *, double=False):
            self.actions.append(
                ("act_structural", locator.name, handle.target_fingerprint)
            )

    vision = SettleVision()
    vision.text_results = {"release note": [Match((10, 10), (0, 0, 5, 5)), None, None]}
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = StructuralBackend()
    workflow = Workflow(
        name="wf",
        steps=[click_step()],
        interstitials=[
            Interstitial(
                name="release note",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="release note"),
                dismiss_anchor=Anchor(
                    template="",
                    structural=StructuralLocator(role="button", name="Close notice"),
                    region=(10, 10, 10, 10),
                    click_point=(15, 15),
                ),
                risk="reversible",
                consequential=False,
                clearance=_cleared("release note"),
            )
        ],
    )

    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )

    assert report.success is True, report.results[0]
    assert len(backend.locators) == 1
    assert backend.actions[0] == (
        "act_structural",
        "Close notice",
        "a" * 64,
    )
    assert backend.actions[1] == ("click", 110, 105, False)


def test_structural_only_interstitial_never_downgrades_to_ocr(bundle, run_dir):
    vision = SettleVision()
    vision.text_results = {
        "release note": Match((10, 10), (0, 0, 5, 5)),
        "Close notice": Match((15, 15), (10, 10, 10, 10)),
    }
    backend = FakeBackend()  # no structural locate/act capability
    workflow = Workflow(
        name="wf",
        steps=[click_step()],
        interstitials=[
            Interstitial(
                name="release note",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="release note"),
                dismiss_anchor=Anchor(
                    template="",
                    structural=StructuralLocator(role="button", name="Close notice"),
                    region=(10, 10, 10, 10),
                    click_point=(15, 15),
                    ocr_text="Close notice",
                ),
                risk="reversible",
                consequential=False,
                clearance=_cleared("release note"),
            )
        ],
    )

    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )

    assert report.success is False
    assert backend.actions == []
    assert "refusing an OCR/geometry/coordinate downgrade" in (
        report.results[0].error or ""
    )


def test_interstitial_resettle_failure_halts_before_underlying_step(bundle, run_dir):
    """The screen must become ready again after the declared dismissal."""
    vision = SettleVision(settled=[True, False])
    vision.text_results = {
        "rate us": _present_then_absent(Match((10, 10), (0, 0, 5, 5)))
    }
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = FakeBackend()
    wf = Workflow(
        name="wf",
        steps=[click_step()],
        interstitials=[
            Interstitial(
                name="satisfaction survey",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="rate us"),
                dismiss_key="Escape",
                risk="reversible",
                consequential=False,
                clearance=_cleared("rate us"),
            )
        ],
    )

    report = Replayer(
        backend,
        vision=vision,
        poll_interval_s=0.005,
        require_settled=True,
        settle_readiness_timeout_s=0.05,
    ).run(wf, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is False
    assert "did not settle after the interstitial dismissal" in report.results[0].error
    assert backend.actions == [("press", "Escape")]
    assert vision.result_calls == 2
    dismissal = report.results[0].interstitial_actions[0]
    assert dismissal.delivered is True
    assert dismissal.clearance_ok is None
    assert dismissal.ok is False
    assert dismissal.error is not None


def test_interstitial_resettle_is_fail_closed_when_entry_gate_is_off(bundle, run_dir):
    """Automatic dismissal requires a readiness-aware outcome even when the
    optional global entry-frame ``require_settled`` gate is disabled."""

    vision = FakeVision()  # legacy facade cannot report settled vs timed out
    vision.text_results = {
        "rate us": _present_then_absent(Match((10, 10), (0, 0, 5, 5)))
    }
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[click_step()],
        interstitials=[
            Interstitial(
                name="satisfaction survey",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="rate us"),
                dismiss_key="Escape",
                risk="reversible",
                consequential=False,
                clearance=_cleared("rate us"),
            )
        ],
    )

    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )

    assert report.success is False
    assert backend.actions == [("press", "Escape")]
    assert "cannot verify that the screen settled" in (report.results[0].error or "")
    assert report.results[0].safety_halt is True


def test_blocking_interstitial_with_no_dismissal_halts_gracefully(bundle, run_dir):
    """A known interstitial with no safe auto-dismissal HALTs naming it -- a
    clear report, and it never issues the underlying click."""
    vision = FakeVision()
    vision.text_results = {"maintenance": Match((10, 10), (0, 0, 5, 5))}
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = FakeBackend()
    wf = Workflow(
        name="wf",
        steps=[click_step()],
        interstitials=[
            Interstitial(
                name="maintenance banner",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="maintenance"),
            )
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert "maintenance banner" in report.results[0].error
    assert "blocking interstitial" in report.results[0].error
    assert backend.actions == []  # never clicked beneath the overlay


def test_program_exception_handler_cannot_bypass_interstitial_halt(bundle, run_dir):
    step = click_step()
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
    workflow = Workflow(
        name="program",
        program=program,
        interstitials=[
            Interstitial(
                name="maintenance notice",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="maintenance"),
            )
        ],
    )
    vision = FakeVision()
    vision.text_results = {"maintenance": Match((10, 10), (0, 0, 5, 5))}
    backend = FakeBackend()

    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )

    assert report.success is False
    assert backend.actions == []
    assert report.results[0].safety_halt is True
    assert report.results[0].exception_handled is False


def test_interstitial_failed_clearance_halts_without_blind_retry(bundle, run_dir):
    """A dismissal whose declared clearance fails emits exactly one audited
    action, then HALTs rather than retrying or acting beneath the overlay."""
    vision = SettleVision(settled=True)
    # Always present (never absent): dismissal never clears it.
    vision.text_results = {"stuck": Match((10, 10), (0, 0, 5, 5))}
    backend = FakeBackend()
    wf = Workflow(
        name="wf",
        steps=[click_step()],
        interstitials=[
            Interstitial(
                name="stuck modal",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="stuck"),
                dismiss_key="Escape",
                risk="reversible",
                consequential=False,
                clearance=_cleared("stuck"),
            )
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert "declared visual clearance" in report.results[0].error
    assert backend.actions.count(("press", "Escape")) == 1  # no blind retries
    assert not any(a[0] == "click" for a in backend.actions)
    dismissal = report.results[0].interstitial_actions[0]
    assert dismissal.delivered is True
    assert dismissal.clearance_ok is False
    assert dismissal.ok is False


def test_alternating_interstitials_halt_on_recurrence(bundle, run_dir):
    """Two overlays may reveal each other, but the runtime never enters an
    unbounded A -> B -> A automatic-action cycle within one workflow step."""

    match = Match((10, 10), (0, 0, 5, 5))
    vision = SettleVision(settled=True)
    vision.text_results = {
        # A: detect, clear, confirm absent, skip while B is active, then recur.
        "overlay A": [match, None, None, None, match],
        # B: detect after A, clear, and confirm absent.
        "overlay B": [match, None, None],
    }
    backend = FakeBackend()
    workflow = Workflow(
        name="cycle",
        steps=[click_step()],
        interstitials=[
            Interstitial(
                name="overlay A",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="overlay A"),
                dismiss_key="Escape",
                risk="reversible",
                consequential=False,
                clearance=_cleared("overlay A"),
            ),
            Interstitial(
                name="overlay B",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="overlay B"),
                dismiss_key="Escape",
                risk="reversible",
                consequential=False,
                clearance=_cleared("overlay B"),
            ),
        ],
    )

    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )

    assert report.success is False
    assert backend.actions == [("press", "Escape"), ("press", "Escape")]
    assert len(report.results[0].interstitial_actions) == 2
    assert "reappeared" in (report.results[0].error or "")
    assert report.results[0].safety_halt is True


def test_operator_supplied_interstitial_applies_without_recompiling(bundle, run_dir):
    """An operator can pass interstitials to the Replayer; they merge with the
    bundle's own (here the bundle declares none)."""
    vision = SettleVision(settled=True)
    vision.text_results = {
        "release tip": _present_then_absent(Match((10, 10), (0, 0, 5, 5)))
    }
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = FakeBackend()
    wf = Workflow(name="wf", steps=[click_step()])  # no bundle interstitials
    report = Replayer(
        backend,
        vision=vision,
        poll_interval_s=0.005,
        interstitials=[
            Interstitial(
                name="release tip",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="release tip"),
                dismiss_key="Escape",
                risk="reversible",
                consequential=False,
                clearance=_cleared("release tip"),
            )
        ],
    ).run(wf, bundle_dir=bundle, run_dir=run_dir)
    assert report.success is True
    assert backend.actions[0] == ("press", "Escape")
    assert report.results[0].interstitial_actions[0].ok is True


def test_no_interstitials_declared_is_byte_for_byte_unchanged(bundle, run_dir):
    vision = FakeVision()
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = FakeBackend()
    wf = Workflow(name="wf", steps=[click_step()])
    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("click", 110, 105, False)]


def test_interstitial_rejects_ambiguous_dismissal():
    with pytest.raises(ValueError, match="at most one dismissal"):
        Interstitial(
            name="ambiguous",
            detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="notice"),
            dismiss_key="Escape",
            risk="reversible",
            consequential=False,
            clearance=_cleared("notice"),
            dismiss_anchor=Anchor(
                template="templates/btn.png",
                region=(0, 0, 10, 10),
                click_point=(5, 5),
            ),
        )


def test_interstitial_rejects_nonvisual_or_negative_detection():
    with pytest.raises(ValueError, match="affirmative visual evidence"):
        Interstitial(
            name="blind",
            detect=Predicate(kind=PredicateKind.TEXT_ABSENT, text="notice"),
            dismiss_key="Escape",
            risk="reversible",
            consequential=False,
            clearance=_cleared("notice"),
        )


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"consequential": False}, "risk='reversible'"),
        (
            {"risk": "irreversible", "consequential": False},
            "risk='reversible'",
        ),
        ({"risk": "reversible", "consequential": True}, "consequential=False"),
        (
            {
                "risk": "reversible",
                "consequential": False,
                "clearance": None,
            },
            "clearance",
        ),
        (
            {
                "dismiss_key": "Enter",
                "risk": "reversible",
                "consequential": False,
                "clearance": _cleared("notice"),
            },
            "only permits Escape",
        ),
        (
            {
                "risk": "reversible",
                "consequential": False,
                "clearance": Predicate(
                    kind=PredicateKind.PARAM_EQUALS, param="state", value="closed"
                ),
            },
            "visual postcondition",
        ),
    ],
)
def test_automatic_dismissal_requires_governed_nonconsequential_contract(
    overrides,
    error,
):
    kwargs = {
        "name": "notice",
        "detect": Predicate(kind=PredicateKind.TEXT_PRESENT, text="notice"),
        "dismiss_key": "Escape",
        "clearance": _cleared("notice"),
        **overrides,
    }
    with pytest.raises(ValueError, match=error):
        Interstitial(**kwargs)


def test_dismissal_delivery_failure_is_still_audited(bundle, run_dir):
    class FailingDismissBackend(FakeBackend):
        def press(self, key):
            self.actions.append(("press", key))
            raise RuntimeError("delivery unavailable")

    vision = FakeVision()
    vision.text_results = {"release note": Match((10, 10), (0, 0, 5, 5))}
    backend = FailingDismissBackend()
    workflow = Workflow(
        name="wf",
        steps=[click_step()],
        interstitials=[
            Interstitial(
                name="release note",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="release note"),
                dismiss_key="Escape",
                risk="reversible",
                consequential=False,
                clearance=_cleared("release note"),
            )
        ],
    )

    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )

    assert report.success is False
    assert backend.actions == [("press", "Escape")]
    event = report.results[0].interstitial_actions[0]
    assert event.attempted is True
    assert event.delivered is False
    assert event.clearance_ok is None
    assert event.ok is False
    assert "delivery failed" in (event.error or "")
    assert "delivery failed" in (report.results[0].error or "")


def test_in_memory_dismissal_policy_mutation_refuses_before_action(bundle, run_dir):
    interstitial = Interstitial(
        name="release note",
        detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="release note"),
        dismiss_key="Escape",
        risk="reversible",
        consequential=False,
        clearance=_cleared("release note"),
    )
    vision = FakeVision()
    vision.text_results = {"release note": Match((10, 10), (0, 0, 5, 5))}
    backend = FakeBackend()
    workflow = Workflow(name="wf", steps=[click_step()], interstitials=[interstitial])
    workflow.interstitials[0].risk = "irreversible"

    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )

    assert report.success is False
    assert backend.actions == []
    assert report.results[0].interstitial_actions == []
    assert "refusing to emit an action" in (report.results[0].error or "")


def test_click_dismissal_requires_sealed_template_before_any_action(bundle, run_dir):
    empty_anchor = Anchor(
        template="",
        region=(0, 0, 10, 10),
        click_point=(5, 5),
        ocr_text="Close",
    )
    with pytest.raises(ValueError, match="sealed anchor template"):
        Interstitial(
            name="release note",
            detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="release note"),
            dismiss_anchor=empty_anchor,
            risk="reversible",
            consequential=False,
            clearance=_cleared("release note"),
        )

    structural_only = Interstitial(
        name="structural release note",
        detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="release note"),
        dismiss_anchor=empty_anchor.model_copy(
            update={
                "structural": StructuralLocator(
                    role="button", name="Close release note"
                )
            }
        ),
        risk="reversible",
        consequential=False,
        clearance=_cleared("release note"),
    )
    assert structural_only.dismiss_anchor is not None
    assert structural_only.dismiss_anchor.template == ""

    interstitial = Interstitial(
        name="release note",
        detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="release note"),
        dismiss_anchor=empty_anchor.model_copy(
            update={"template": "templates/btn.png"}
        ),
        risk="reversible",
        consequential=False,
        clearance=_cleared("release note"),
    )
    workflow = Workflow(name="wf", steps=[click_step()], interstitials=[interstitial])
    assert workflow.interstitials[0].dismiss_anchor is not None
    workflow.interstitials[0].dismiss_anchor.template = ""
    vision = FakeVision()
    vision.text_results = {"release note": Match((10, 10), (0, 0, 5, 5))}
    backend = FakeBackend()

    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )

    assert report.success is False
    assert backend.actions == []
    assert report.results[0].interstitial_actions == []
    assert report.results[0].safety_halt is True


def test_click_dismissal_template_is_in_sealed_file_hashes(tmp_path):
    bundle = tmp_path / "sealed"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "assets" / "target.png").write_bytes(make_png((50, 20)))
    (bundle / "assets" / "close.png").write_bytes(make_png((30, 20)))
    workflow = Workflow(
        name="wf",
        steps=[click_step(template="assets/target.png")],
        interstitials=[
            Interstitial(
                name="release note",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="release note"),
                dismiss_anchor=Anchor(
                    template="assets/close.png",
                    region=(0, 0, 30, 20),
                    click_point=(15, 10),
                    ocr_text="Close",
                ),
                risk="reversible",
                consequential=False,
                clearance=_cleared("release note"),
            )
        ],
    )

    workflow.save(bundle)
    loaded = Workflow.load(bundle)

    assert loaded.manifest is not None
    assert "assets/close.png" in loaded.manifest.file_hashes


# -- IR round-trip ------------------------------------------------------------


def test_interstitials_round_trip_through_bundle(bundle):
    wf = Workflow(
        name="wf",
        steps=[click_step()],
        interstitials=[
            Interstitial(
                name="survey",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="rate us"),
                dismiss_key="Escape",
                risk="reversible",
                consequential=False,
                clearance=_cleared("rate us"),
            )
        ],
    )
    out = bundle / "wf"
    wf.save(out)
    loaded = Workflow.load(out)
    assert len(loaded.interstitials) == 1
    assert loaded.interstitials[0].name == "survey"
    assert loaded.interstitials[0].dismiss_key == "Escape"
    assert loaded.interstitials[0].risk == "reversible"
    assert loaded.interstitials[0].consequential is False
    assert loaded.interstitials[0].clearance is not None
    assert loaded.interstitials[0].detect.kind == PredicateKind.TEXT_PRESENT
