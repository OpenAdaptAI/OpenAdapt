"""Workflow-program IR, Phase 1 (RFC docs/design/WORKFLOW_PROGRAM_IR.md §6).

Additive, backward-compatible: typed parameters (``Workflow.param_specs``), a
per-step ``wait_until`` readiness predicate, and a per-step ``guard``
precondition. A bundle that declares none of them replays EXACTLY as a v0
linear bundle (see also tests/test_replayer.py, all of whose cases keep
passing unchanged).

Backend and vision are faked (reused from test_replayer) -- no Playwright, no
OCR stack, ZERO model calls.
"""

from __future__ import annotations

import pytest

from openadapt_flow.ir import (
    ActionKind,
    Guard,
    ParamKind,
    ParamSpec,
    Predicate,
    PredicateKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision, Match, click_step, make_png


@pytest.fixture()
def bundle(tmp_path):
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "templates").mkdir(parents=True)
    (bundle_dir / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    return bundle_dir


@pytest.fixture()
def run_dir(tmp_path):
    return tmp_path / "run"


def key_step(step_id="k1", key="Enter", *, guard=None, wait_until=None) -> Step:
    return Step(
        id=step_id,
        intent=f"press {key}",
        action=ActionKind.KEY,
        key=key,
        guard=guard,
        wait_until=wait_until,
    )


# -- worked example: typed param + wait_until + guard, round-tripped ----------


def test_worked_example_roundtrips_through_bundle_save_load(bundle):
    """The RFC add-patient-note-style skill expressed with a typed param, a
    wait_until, and a guard survives ``Workflow.save`` -> ``Workflow.load``
    byte-for-byte in its Phase-1 fields."""
    wf = Workflow(
        name="add-patient-note",
        params={"note": "Follow-up in 2 weeks", "encounter_type": "Triage"},
        param_specs={
            "note": ParamSpec(
                name="note",
                type=ParamKind.STRING,
                example="Follow-up in 2 weeks",
            ),
            "encounter_type": ParamSpec(
                name="encounter_type",
                type=ParamKind.ENUM,
                example="Triage",
                choices=["Triage", "Consult"],
            ),
        },
        steps=[
            # optional-branch step: only click "Triage" when the param says so
            Step(
                id="s_triage",
                intent="click Triage",
                action=ActionKind.CLICK,
                anchor=click_step().anchor,
                guard=Guard(
                    predicate=Predicate(
                        kind=PredicateKind.PARAM_EQUALS,
                        param="encounter_type",
                        value="Triage",
                    ),
                    on_unmet="skip",
                ),
            ),
            # readiness-gated typed step
            Step(
                id="s_note",
                intent="type the note",
                action=ActionKind.TYPE,
                param="note",
                wait_until=Predicate(
                    kind=PredicateKind.TEXT_PRESENT,
                    text="Save Encounter",
                    timeout_s=2.0,
                ),
            ),
        ],
    )
    wf.save(bundle)
    loaded = Workflow.load(bundle)

    assert loaded.param_specs["encounter_type"].type is ParamKind.ENUM
    assert loaded.param_specs["encounter_type"].choices == ["Triage", "Consult"]
    assert loaded.param_specs["note"].example == "Follow-up in 2 weeks"
    g = loaded.steps[0].guard
    assert g.on_unmet == "skip"
    assert g.predicate.kind is PredicateKind.PARAM_EQUALS
    assert g.predicate.param == "encounter_type"
    w = loaded.steps[1].wait_until
    assert w.kind is PredicateKind.TEXT_PRESENT and w.text == "Save Encounter"
    assert w.timeout_s == 2.0


# -- typed params supplied / defaulted at replay -----------------------------


def _type_param_workflow() -> Workflow:
    return Workflow(
        name="wf",
        param_specs={
            "note": ParamSpec(
                name="note", type=ParamKind.STRING, example="recorded default"
            )
        },
        steps=[Step(id="t1", intent="type note", action=ActionKind.TYPE, param="note")],
    )


def test_typed_param_example_is_the_replay_default(bundle, run_dir):
    """No caller value -> the ParamSpec.example is substituted (the generalized
    'note value at replay')."""
    backend = FakeBackend()
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        _type_param_workflow(), bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("type", "recorded default")]
    assert report.params["note"] == "recorded default"


def test_caller_param_overrides_typed_example(bundle, run_dir):
    backend = FakeBackend()
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        _type_param_workflow(),
        params={"note": "run value"},
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True
    assert backend.actions == [("type", "run value")]


def test_missing_required_param_fails_fast_naming_it(bundle, run_dir):
    """A required typed param with no caller value AND no example halts the run
    BEFORE any step executes, naming the parameter."""
    wf = Workflow(
        name="wf",
        param_specs={
            "patient": ParamSpec(
                name="patient", type=ParamKind.ENTITY_REF, required=True
            )
        },
        steps=[key_step()],
    )
    backend = FakeBackend()
    report = Replayer(backend, vision=FakeVision()).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert len(report.results) == 1
    assert report.results[0].step_id == "<params>"
    assert "patient" in report.results[0].error
    assert backend.actions == []  # nothing ran


# -- wait_until: bounded readiness, fail-safe HALT on timeout -----------------


def test_wait_until_holds_then_step_proceeds(bundle, run_dir):
    vision = FakeVision()
    # "Ready" absent on the first probe, present on the second.
    vision.text_results = {"Ready": [None, Match((10, 10), (0, 0, 5, 5))]}
    backend = FakeBackend()
    wf = Workflow(
        name="wf",
        steps=[
            key_step(
                wait_until=Predicate(
                    kind=PredicateKind.TEXT_PRESENT, text="Ready", timeout_s=1.0
                )
            )
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("press", "Enter")]


def test_wait_until_timeout_halts_and_never_proceeds(bundle, run_dir):
    vision = FakeVision()  # "Ready" is never present
    backend = FakeBackend()
    wf = Workflow(
        name="wf",
        steps=[
            key_step(
                wait_until=Predicate(
                    kind=PredicateKind.TEXT_PRESENT, text="Ready", timeout_s=0.05
                )
            )
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert len(report.results) == 1
    err = report.results[0].error
    assert "wait_until" in err and "k1" in err
    assert backend.actions == []  # HALT: never proceeded-anyway
    assert report.results[0].skipped is False


# -- guards: HALT-on-unmet (default) vs SKIP ---------------------------------


def test_guard_unmet_halts_by_default(bundle, run_dir):
    backend = FakeBackend()
    wf = Workflow(
        name="wf",
        params={"mode": "user"},
        steps=[
            key_step(
                guard=Guard(
                    predicate=Predicate(
                        kind=PredicateKind.PARAM_EQUALS, param="mode", value="admin"
                    )
                )
            )
        ],
    )
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert "Guard precondition" in report.results[0].error
    assert backend.actions == []


def test_guard_unmet_skip_makes_step_a_noop_success(bundle, run_dir):
    """on_unmet='skip' turns an expected-but-optional step (e.g. dismiss a
    survey modal only when present) into a no-op success -- the next step
    still runs. This is a guarded branch WITHOUT the Phase-2 state machine."""
    vision = FakeVision()  # "Survey" never present
    backend = FakeBackend()
    wf = Workflow(
        name="wf",
        steps=[
            key_step(
                "dismiss",
                key="Escape",
                guard=Guard(
                    predicate=Predicate(kind=PredicateKind.TEXT_PRESENT, text="Survey"),
                    on_unmet="skip",
                ),
            ),
            key_step("next", key="Tab"),
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert report.results[0].skipped is True
    assert report.results[0].ok is True
    assert report.results[1].skipped is False
    assert backend.actions == [("press", "Tab")]  # dismiss skipped, next ran


def test_guard_met_executes_step_normally(bundle, run_dir):
    vision = FakeVision()
    vision.text_results = {"Survey": Match((10, 10), (0, 0, 5, 5))}
    backend = FakeBackend()
    wf = Workflow(
        name="wf",
        steps=[
            key_step(
                "dismiss",
                key="Escape",
                guard=Guard(
                    predicate=Predicate(kind=PredicateKind.TEXT_PRESENT, text="Survey"),
                    on_unmet="skip",
                ),
            ),
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert report.results[0].skipped is False
    assert backend.actions == [("press", "Escape")]


# -- wait_until subsumes the SCROLL closed loop ------------------------------


def test_scroll_wait_until_predicate_is_the_stop_condition(bundle, run_dir):
    """A SCROLL step's readiness is a wait_until predicate: with an explicit
    text_present predicate the scroll loops until that text appears -- the same
    machinery that, by default, waits on the next anchor's ANCHOR_RESOLVES."""
    vision = FakeVision()
    # "Bottom" absent on the pre-scroll probe, present after the first scroll.
    vision.text_results = {"Bottom": [None, Match((10, 10), (0, 0, 5, 5))]}
    backend = FakeBackend()
    scroll = Step(
        id="sc1",
        intent="scroll to bottom",
        action=ActionKind.SCROLL,
        scroll_dx=0,
        scroll_dy=400,
        wait_until=Predicate(kind=PredicateKind.TEXT_PRESENT, text="Bottom"),
    )
    wf = Workflow(name="wf", steps=[scroll, key_step("k1", "Enter")])
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("scroll", 0, 400), ("press", "Enter")]


def test_default_scroll_still_waits_on_next_anchor(bundle, run_dir):
    """No explicit wait_until: the SCROLL default readiness is ANCHOR_RESOLVES
    on the next anchored step -- today's closed loop, now a predicate. Probe
    misses pre-scroll (local+global), resolves after the first scroll."""
    vision = FakeVision()
    target = Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    vision.template_results = [None, None, target, target]
    backend = FakeBackend()
    scroll = Step(
        id="sc1", intent="scroll", action=ActionKind.SCROLL, scroll_dx=0, scroll_dy=400
    )
    wf = Workflow(name="wf", steps=[scroll, click_step()])
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("scroll", 0, 400), ("click", 110, 105, False)]


# -- back-compat: a bundle with none of the Phase-1 fields is unchanged -------


def test_no_new_fields_replays_exactly_as_before(bundle, run_dir):
    """A workflow declaring no param_specs / guard / wait_until behaves
    identically to a v0 bundle: same actions, same success, skipped=False."""
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    backend = FakeBackend()
    wf = Workflow(name="wf", steps=[click_step(), key_step("k1", "Enter")])
    # sanity: the additive fields default to empty/None
    assert wf.param_specs == {}
    assert wf.steps[0].guard is None and wf.steps[0].wait_until is None
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("click", 110, 105, False), ("press", "Enter")]
    assert all(r.skipped is False for r in report.results)
    assert report.model_calls == 0  # $0 runtime preserved
