"""Workflow-program IR, Phase 2 (RFC docs/design/WORKFLOW_PROGRAM_IR.md §2).

The parameterized STATE MACHINE: the control flow a linear action list cannot
express -- LOOPS over a worklist, guarded BRANCHES, reusable SUBFLOWS, and
EXCEPTION paths -- interpreted deterministically (ZERO model calls, $0 replay).

Built ADDITIVELY on Phase 1: every ``action`` state runs through the SAME
per-step pipeline the linear replayer uses (resolve / identity gate / effect
verify / risk gate / heal), so no safety property is weakened by adding control
flow around the hardened leaf -- proven here by the identity- and effect-gate
tests that fire INSIDE a loop body. A linear (no-``program``) bundle replays
byte-for-byte as today, and its mechanical lift to the degenerate single-path
graph replays identically (``lift_to_program``).

Backend and vision are faked (reused from test_replayer): no Playwright, no OCR
stack, ZERO model calls.
"""

from __future__ import annotations

import pytest

from openadapt_flow.ir import (
    ActionKind,
    LoopSpec,
    Predicate,
    PredicateKind,
    ProgramGraph,
    Relation,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
    lift_to_program,
)
from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import (
    FakeBackend,
    FakeVision,
    Match,
    OcrLine,
    click_step,
    context_click_step,
    make_png,
    resolving_vision,
)


@pytest.fixture()
def bundle(tmp_path):
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "templates").mkdir(parents=True)
    (bundle_dir / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    return bundle_dir


@pytest.fixture()
def run_dir(tmp_path):
    return tmp_path / "run"


# -- tiny builders -----------------------------------------------------------


def key_step(step_id, key) -> Step:
    return Step(id=step_id, intent=f"press {key}", action=ActionKind.KEY, key=key)


def type_step(step_id, param) -> Step:
    return Step(id=step_id, intent=f"type {param}", action=ActionKind.TYPE, param=param)


def failing_click(step_id) -> Step:
    """A click whose target never resolves (no template match scripted). A short
    timeout keeps the resolution-retry loop fast in these fail-path tests."""
    step = click_step(step_id)
    step.timeout_s = 0.1
    return step


def action(step: Step, *, to: str, on_exception: str | None = None) -> State:
    """An ACTION state with a single unconditional transition to ``to``."""
    return State(
        id=step.id,
        kind=StateKind.ACTION,
        step=step,
        transitions=[Transition(target=to)],
        on_exception=on_exception,
    )


def terminal(state_id, outcome="success", reason="") -> State:
    return State(id=state_id, kind=StateKind.TERMINAL, outcome=outcome, reason=reason)


def graph(entry, *states: State) -> ProgramGraph:
    return ProgramGraph(entry=entry, states={s.id: s for s in states})


# ===========================================================================
# LOOPS over a worklist (RFC §2.3)
# ===========================================================================


def _loop_over_queue_workflow() -> Workflow:
    """A loop over the ``queue`` relation whose body types the row's ``patient``
    field once per row (so the recorded actions reveal the per-row binding and
    the iteration count)."""
    body = graph(
        "b_type",
        action(type_step("b_type", "patient"), to="b_end"),
        terminal("b_end"),
    )
    program = graph(
        "loop",
        State(
            id="loop",
            kind=StateKind.LOOP,
            loop=LoopSpec(relation="queue", body="body", var="patient"),
            transitions=[Transition(target="done")],
        ),
        terminal("done"),
    )
    return Workflow(
        name="clear-queue",
        program=program,
        subflows={"body": body},
    )


def test_loop_runs_body_once_per_row_binding_each_row(bundle, run_dir):
    """A 3-row worklist runs the body 3x, the loop variable binding each row in
    turn (Rousillon/Helena/WebRobot: for every row ...)."""
    wf = _loop_over_queue_workflow()
    wf.data_sources = {
        "queue": Relation(
            name="queue",
            rows=[{"patient": "Alice"}, {"patient": "Bob"}, {"patient": "Cara"}],
        )
    }
    backend = FakeBackend()
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert report.terminal_outcome == "success"
    assert backend.actions == [
        ("type", "Alice"),
        ("type", "Bob"),
        ("type", "Cara"),
    ]
    assert report.model_calls == 0  # $0 runtime preserved


def test_loop_over_zero_rows_runs_body_zero_times(bundle, run_dir):
    """A variable-length worklist that happens to be EMPTY runs the body 0x and
    still completes successfully -- the data-dependent case a single linear
    trace cannot express."""
    wf = _loop_over_queue_workflow()
    wf.data_sources = {"queue": Relation(name="queue", rows=[])}
    backend = FakeBackend()
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == []  # body never ran
    assert report.terminal_outcome == "success"


def test_loop_worklist_supplied_at_run_time(bundle, run_dir):
    """The worklist can be supplied at RUN time (a genuinely data-dependent
    queue whose length is unknown until then), overriding any inline rows."""
    wf = _loop_over_queue_workflow()  # no data_sources at all
    backend = FakeBackend()
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        wf,
        worklists={"queue": [{"patient": "X"}, {"patient": "Y"}]},
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True
    assert backend.actions == [("type", "X"), ("type", "Y")]


def test_loop_bound_is_enforced(bundle, run_dir):
    """More rows than ``max_iterations`` HALTs (fail-safe) rather than running
    unbounded."""
    wf = _loop_over_queue_workflow()
    wf.program.states["loop"].loop.max_iterations = 2
    backend = FakeBackend()
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        wf,
        worklists={"queue": [{"patient": str(i)} for i in range(3)]},
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is False
    assert report.terminal_outcome == "halt"
    assert backend.actions == []  # refused before iterating


# ===========================================================================
# BRANCHES / conditionals (RFC §2.2) -- guarded transitions pick the arm
# ===========================================================================


def _branch_on_param_workflow() -> Workflow:
    program = graph(
        "pick",
        State(
            id="pick",
            kind=StateKind.BRANCH,
            transitions=[
                Transition(
                    guard=Predicate(
                        kind=PredicateKind.PARAM_EQUALS,
                        param="encounter_type",
                        value="Triage",
                    ),
                    target="s_triage",
                ),
                Transition(
                    guard=Predicate(
                        kind=PredicateKind.PARAM_EQUALS,
                        param="encounter_type",
                        value="Consult",
                    ),
                    target="s_consult",
                ),
            ],
        ),
        action(key_step("s_triage", "T"), to="done"),
        action(key_step("s_consult", "C"), to="done"),
        terminal("done"),
    )
    return Workflow(name="pick-encounter", program=program)


def test_branch_takes_the_triage_arm(bundle, run_dir):
    backend = FakeBackend()
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        _branch_on_param_workflow(),
        params={"encounter_type": "Triage"},
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True
    assert backend.actions == [("press", "T")]
    assert "s_triage" in report.visited_states
    assert "s_consult" not in report.visited_states


def test_branch_takes_the_consult_arm(bundle, run_dir):
    backend = FakeBackend()
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        _branch_on_param_workflow(),
        params={"encounter_type": "Consult"},
        bundle_dir=bundle,
        run_dir=run_dir,
    )
    assert report.success is True
    assert backend.actions == [("press", "C")]
    assert "s_consult" in report.visited_states


def test_branch_on_screen_predicate_dismisses_optional_modal(bundle, run_dir):
    """The MockMed survey-modal case as a guarded BRANCH (RFC §2.2): after Save,
    a transition guarded on the modal's presence routes to a dismiss subflow,
    else straight to done -- an optional-but-expected popup, not drift/halt."""
    program = graph(
        "s_save",
        State(
            id="s_save",
            kind=StateKind.ACTION,
            step=key_step("s_save", "S"),
            transitions=[
                Transition(
                    guard=Predicate(kind=PredicateKind.TEXT_PRESENT, text="Survey"),
                    target="s_dismiss",
                ),
                Transition(target="done"),  # unconditional fall-through
            ],
        ),
        action(key_step("s_dismiss", "Escape"), to="done"),
        terminal("done"),
    )
    wf = Workflow(name="save-maybe-survey", program=program)

    # Modal PRESENT -> the guarded arm dismisses it, then rejoins.
    v_present = FakeVision()
    v_present.text_results = {"Survey": Match((10, 10), (0, 0, 5, 5))}
    b_present = FakeBackend()
    r_present = Replayer(b_present, vision=v_present, poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert r_present.success is True
    assert b_present.actions == [("press", "S"), ("press", "Escape")]

    # Modal ABSENT -> the unconditional fall-through goes straight to done.
    b_absent = FakeBackend()
    r_absent = Replayer(b_absent, vision=FakeVision(), poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir / "absent"
    )
    assert r_absent.success is True
    assert b_absent.actions == [("press", "S")]


def test_branch_with_no_matching_arm_halts_fail_safe(bundle, run_dir):
    """A branch whose guards all fail on the current screen is a dead end: the
    run HALTs (never guesses an edge) -- the refuse-rather-than-guess posture."""
    program = graph(
        "pick",
        State(
            id="pick",
            kind=StateKind.BRANCH,
            transitions=[
                Transition(
                    guard=Predicate(
                        kind=PredicateKind.PARAM_EQUALS, param="m", value="a"
                    ),
                    target="s_a",
                )
            ],
        ),
        action(key_step("s_a", "A"), to="done"),
        terminal("done"),
    )
    wf = Workflow(name="dead-end", program=program)
    backend = FakeBackend()
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        wf, params={"m": "z"}, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert report.terminal_outcome == "halt"
    assert backend.actions == []


# ===========================================================================
# SUBFLOWS -- reusable named subgraphs (RFC §2.2)
# ===========================================================================


def test_subflow_reused_as_loop_body_and_direct_call(bundle, run_dir):
    """One subflow ``greet`` (press 'G') is reused BOTH as a loop body (per row)
    AND as a direct subflow_call -- the reusable-component construct. 2 rows +
    1 direct call => 3 presses."""
    greet = graph("g", action(key_step("g", "G"), to="g_end"), terminal("g_end"))
    program = graph(
        "loop",
        State(
            id="loop",
            kind=StateKind.LOOP,
            loop=LoopSpec(relation="queue", body="greet"),
            transitions=[Transition(target="call")],
        ),
        State(
            id="call",
            kind=StateKind.SUBFLOW_CALL,
            subflow="greet",
            transitions=[Transition(target="done")],
        ),
        terminal("done"),
    )
    wf = Workflow(
        name="greet-all",
        program=program,
        subflows={"greet": greet},
        data_sources={"queue": Relation(name="queue", rows=[{"n": "1"}, {"n": "2"}])},
    )
    backend = FakeBackend()
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("press", "G")] * 3


def test_subflow_call_to_undefined_subflow_halts(bundle, run_dir):
    program = graph(
        "call",
        State(
            id="call",
            kind=StateKind.SUBFLOW_CALL,
            subflow="missing",
            transitions=[Transition(target="done")],
        ),
        terminal("done"),
    )
    wf = Workflow(name="bad", program=program)
    backend = FakeBackend()
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert report.terminal_outcome == "halt"


# ===========================================================================
# EXCEPTION paths (RFC §2.4) -- on_exception routes a failed action + continues
# ===========================================================================


def test_on_exception_catches_failed_action_and_continues(bundle, run_dir):
    """An expected-exceptional state (here a click whose target a popup hides,
    so the resolution ladder fails) routes to a local handler subflow that
    recovers and rejoins -- the run continues instead of aborting (the graph
    analog of try/except)."""
    recover = graph(
        "r",
        action(key_step("r", "Escape"), to="r_end"),
        terminal("r_end"),
    )
    program = graph(
        "s_open",
        # This click cannot resolve (no template match scripted) -> it FAILS,
        # and routes to the recover subflow via on_exception.
        State(
            id="s_open",
            kind=StateKind.ACTION,
            step=failing_click("s_open"),
            transitions=[Transition(target="done")],
            on_exception="s_recover",
        ),
        State(
            id="s_recover",
            kind=StateKind.SUBFLOW_CALL,
            subflow="recover",
            transitions=[Transition(target="done")],
        ),
        terminal("done"),
    )
    wf = Workflow(
        name="save-or-recover", program=program, subflows={"recover": recover}
    )
    backend = FakeBackend()
    # FakeVision with NO template match -> the click's resolution ladder fails.
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True  # the run CONTINUED via the handler
    assert report.terminal_outcome == "success"
    # The failed action is recorded, marked handled; the handler then ran.
    failed = next(r for r in report.results if r.step_id == "s_open")
    assert failed.ok is False
    assert failed.exception_handled is True
    assert backend.actions == [("press", "Escape")]  # recovery ran, rejoined


def test_unhandled_action_failure_halts_the_whole_run(bundle, run_dir):
    """WITHOUT an on_exception handler, a failed action HALTs the whole run --
    no regression vs. today's halt-on-failure."""
    program = graph(
        "s_open",
        action(failing_click("s_open"), to="done"),  # no on_exception
        terminal("done"),
    )
    wf = Workflow(name="no-handler", program=program)
    backend = FakeBackend()
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert report.terminal_outcome == "halt"


def test_halt_terminal_stops_the_run(bundle, run_dir):
    """A ``halt`` terminal (e.g. the patient-not-found branch the demo never
    showed) stops the run with success=False."""
    program = graph(
        "t",
        terminal("t", outcome="halt", reason="patient not found"),
    )
    wf = Workflow(name="halt-now", program=program)
    backend = FakeBackend()
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert report.terminal_outcome == "halt"


# ===========================================================================
# Safety gates STILL FIRE inside loop bodies (RFC §2.1, §2.3)
# ===========================================================================


def test_identity_gate_fires_inside_loop_body(bundle, run_dir):
    """A loop body's CLICK is identity-gated exactly as a linear click: when the
    live band names a DIFFERENT entity than the recorded target, the body action
    refuses and the run HALTs -- inside the loop. This is the entity_ref/
    re-resolve-by-identity guarantee (iteration N must click the RIGHT row)."""
    body = graph(
        "b_click",
        action(
            context_click_step(
                "Jane Sample Knee pain referral High", step_id="b_click"
            ),
            to="b_end",
        ),
        terminal("b_end"),
    )
    program = graph(
        "loop",
        State(
            id="loop",
            kind=StateKind.LOOP,
            loop=LoopSpec(relation="queue", body="body"),
            transitions=[Transition(target="done")],
        ),
        terminal("done"),
    )
    wf = Workflow(
        name="clear-queue-identity",
        program=program,
        subflows={"body": body},
        data_sources={"queue": Relation(name="queue", rows=[{"x": "1"}])},
    )
    vision = resolving_vision()
    # Live band names a DIFFERENT entity -> mismatch.
    vision.ocr_lines = [OcrLine("Taylor Duplicate Knee pain referral High")]
    backend = FakeBackend()
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert report.terminal_outcome == "halt"
    assert backend.actions == []  # never clicked the wrong row
    clicked = next(r for r in report.results if r.step_id == "b_click")
    assert clicked.identity is not None
    assert clicked.identity.status == "mismatch"


def test_effect_gate_fires_inside_loop_body(bundle, run_dir):
    """A loop body's step that declares system-of-record ``effects`` with NO
    verifier configured is a fail-safe HALT -- the effect gate fires for a state
    inside a loop body exactly as for a linear step."""
    save = key_step("b_save", "S")
    save.effects = [Effect(kind=EffectKind.RECORD_WRITTEN)]
    body = graph("b_save", action(save, to="b_end"), terminal("b_end"))
    program = graph(
        "loop",
        State(
            id="loop",
            kind=StateKind.LOOP,
            loop=LoopSpec(relation="queue", body="body"),
            transitions=[Transition(target="done")],
        ),
        terminal("done"),
    )
    wf = Workflow(
        name="write-queue",
        program=program,
        subflows={"body": body},
        data_sources={"queue": Relation(name="queue", rows=[{"x": "1"}])},
    )
    backend = FakeBackend()
    # No effect_verifier configured on the Replayer -> declared effect HALTs.
    report = Replayer(backend, vision=FakeVision(), poll_interval_s=0.01).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert report.terminal_outcome == "halt"
    saved = next(r for r in report.results if r.step_id == "b_save")
    assert saved.effect_verified is False


# ===========================================================================
# BACK-COMPAT -- the degenerate linear lift replays identically to today
# ===========================================================================


def _linear_workflow() -> Workflow:
    return Workflow(
        name="wf",
        steps=[
            click_step("s1"),
            type_step("s2", "note"),
            key_step("s3", "Enter"),
        ],
        params={"note": "hello"},
    )


def _resolving_for_linear() -> FakeVision:
    v = FakeVision()
    v.template_results = [Match((110, 105), (100, 100, 50, 20), 0.95)]
    return v


def test_lifted_linear_graph_replays_identically_to_linear(bundle, run_dir):
    """A linear workflow lifted to the degenerate single-path graph
    (``lift_to_program``, RFC §2.6) performs the IDENTICAL backend actions, in
    the identical order, with the identical per-step results as the linear
    replayer -- 'a linear bundle is the degenerate single-path graph'."""
    # Linear replay.
    lin = _linear_workflow()
    b_lin = FakeBackend()
    r_lin = Replayer(b_lin, vision=_resolving_for_linear(), poll_interval_s=0.01).run(
        lin, bundle_dir=bundle, run_dir=run_dir / "lin"
    )
    # Graph replay of the SAME steps, lifted.
    grf = _linear_workflow()
    grf.program = lift_to_program(grf)
    b_grf = FakeBackend()
    r_grf = Replayer(b_grf, vision=_resolving_for_linear(), poll_interval_s=0.01).run(
        grf, bundle_dir=bundle, run_dir=run_dir / "grf"
    )

    assert r_lin.success is r_grf.success is True
    assert b_lin.actions == b_grf.actions  # byte-identical actuation
    assert [r.step_id for r in r_lin.results] == [r.step_id for r in r_grf.results]
    assert [r.ok for r in r_lin.results] == [r.ok for r in r_grf.results]
    assert [(r.resolution.rung if r.resolution else None) for r in r_lin.results] == [
        (r.resolution.rung if r.resolution else None) for r in r_grf.results
    ]
    assert r_lin.model_calls == r_grf.model_calls == 0


def test_no_program_field_replays_exactly_as_v0(bundle, run_dir):
    """A workflow with ``program=None`` runs the linear ``steps`` loop -- the
    additive Phase-2 fields default empty and change nothing."""
    wf = _linear_workflow()
    assert wf.program is None and wf.subflows == {} and wf.data_sources == {}
    backend = FakeBackend()
    report = Replayer(
        backend, vision=_resolving_for_linear(), poll_interval_s=0.01
    ).run(wf, bundle_dir=bundle, run_dir=run_dir)
    assert report.success is True
    assert report.terminal_outcome is None  # linear runs set no terminal
    assert backend.actions == [
        ("click", 110, 105, False),
        ("type", "hello"),
        ("press", "Enter"),
    ]


def test_program_round_trips_through_bundle_save_load(bundle):
    """A program graph (states, transitions, loops, subflows, data_sources)
    survives ``Workflow.save`` -> ``Workflow.load``."""
    wf = _loop_over_queue_workflow()
    wf.data_sources = {"queue": Relation(name="queue", rows=[{"patient": "Alice"}])}
    wf.save(bundle)
    loaded = Workflow.load(bundle)
    assert loaded.program is not None
    assert loaded.program.states["loop"].kind is StateKind.LOOP
    assert loaded.program.states["loop"].loop.body == "body"
    assert loaded.subflows["body"].states["b_type"].step.param == "patient"
    assert loaded.data_sources["queue"].rows == [{"patient": "Alice"}]
