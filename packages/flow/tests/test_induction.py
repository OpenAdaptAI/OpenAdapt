"""Multi-trace induction (RFC docs/design/WORKFLOW_PROGRAM_IR.md §3 [4]+[5]).

Infer a parameterized PROGRAM (the Phase-2 ``ProgramGraph``) from MULTIPLE
demonstrations of the same task -- the induction loop the whole PBD lineage
(Rousillon, WebRobot, Skill-DisCo, PROLEX) says a demonstration compiler must
have. "One demonstration is evidence, not specification."

Since we lack real multi-worker traces, a small generator builds trace VARIANTS
of a MockMed task:

* (a) two traces differing only in a typed value       -> induce a PARAM
* (b) traces with a worklist of length 2 vs 3           -> induce a LOOP
* (c) traces with an optional dialog present vs absent  -> induce a BRANCH
* (d) contradictory traces                              -> induction REJECTS

Everything is deterministic and model-free: the compile-time proposer is a
deterministic FAKE (zero model calls), and its suggestions are FLAGGED, never
silently trusted. The induced program is REPLAYED through the real Phase-2
interpreter (``runtime.replayer``) with the faked backend/vision from
``test_replayer`` -- proving round-trip.
"""

from __future__ import annotations


import pytest

from openadapt_flow.compiler.induction import (
    induce_program,
    reproduction_score,
    validate_held_out,
)
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    StateKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision, Match, make_png


# ===========================================================================
# Synthetic MockMed corpus: trace-variant generators
# ===========================================================================


def _type(step_id: str, field: str, value: str, *, risk: str = "reversible") -> Step:
    return Step(
        id=step_id,
        intent=f"type {field}",
        action=ActionKind.TYPE,
        text=value,
        risk=risk,
    )


def _key(
    step_id: str, key: str, *, intent: str | None = None, risk="reversible"
) -> Step:
    return Step(
        id=step_id,
        intent=intent or f"press {key}",
        action=ActionKind.KEY,
        key=key,
        risk=risk,
    )


def mockmed_param_traces() -> list[Workflow]:
    """(a) Two+ traces of 'open a chart and record a dose' that differ ONLY in
    the patient typed -- the dose is constant. => patient is a PARAM, dose a
    literal."""

    def trace(patient: str) -> Workflow:
        return Workflow(
            name="record-dose",
            steps=[
                _type("s_patient", "patient", patient),
                _type("s_dose", "dose", "10mg"),
                _key("s_save", "Enter", intent="press Enter to save"),
            ],
        )

    return [trace("Alice Alvarez"), trace("Bob Baker"), trace("Cara Chen")]


def mockmed_loop_traces() -> list[Workflow]:
    """(b) 'Clear the worklist': one trace processes 2 patients, another 3. The
    repeated body (type each patient) with a DIFFERING count => a LOOP over a
    Relation."""

    def trace(patients: list[str]) -> Workflow:
        steps: list[Step] = [_type("s_login", "clinic", "MockMed General")]
        for i, p in enumerate(patients):
            steps.append(_type(f"s_row{i}", "patient", p))
        steps.append(_key("s_done", "Enter", intent="press Enter to finish"))
        return Workflow(name="clear-worklist", steps=steps)

    return [trace(["Alice", "Bob"]), trace(["Cara", "Dan", "Eve"])]


def mockmed_optional_traces() -> list[Workflow]:
    """(c) 'Save an encounter': in one trace a Survey popup appears and is
    dismissed; in another it does not. => a BRANCH guarded on the popup's
    presence (guard proposed / flagged)."""

    def trace(with_survey: bool) -> Workflow:
        steps = [
            _type("s_patient", "patient", "Alice"),
            _key("s_save", "S", intent="press S to save the encounter"),
        ]
        if with_survey:
            steps.append(
                _key(
                    "s_dismiss",
                    "Escape",
                    intent="survey popup appeared - dismiss it",
                )
            )
        steps.append(_key("s_close", "Enter", intent="press Enter to close"))
        return Workflow(name="save-encounter", steps=steps)

    return [trace(True), trace(False)]


def mockmed_contradiction_traces() -> list[Workflow]:
    """(d) Two traces that CONTRADICT at the same aligned position with no
    detectable condition: one APPROVES the claim, the other REJECTS it -- both
    irreversible. => induction REFUSES (underdetermined)."""

    def trace(decision_key: str, label: str) -> Workflow:
        return Workflow(
            name="adjudicate-claim",
            steps=[
                _type("s_patient", "patient", "Alice"),
                _key(
                    "s_decide",
                    decision_key,
                    intent=f"{label} the claim",
                    risk="irreversible",
                ),
            ],
        )

    return [trace("A", "approve"), trace("R", "reject")]


# ===========================================================================
# Deterministic FAKE compile-time proposer (zero model calls)
# ===========================================================================


class FakeProposer:
    """A deterministic stand-in for the #78 compile-time StepAnnotator. Makes
    ZERO model calls; returns a canned suggestion and records that it was
    asked. Its output is advisory -- the tests assert it is flagged and NEVER
    flips an underdetermined point to certified."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def propose(self, target: str, kind: str, context) -> str:
        self.calls.append((target, kind, context))
        return f"[annotator guess for {target}: {kind}]"


# ===========================================================================
# Replay harness (round-trip through the real Phase-2 interpreter)
# ===========================================================================


@pytest.fixture()
def bundle(tmp_path):
    bd = tmp_path / "bundle"
    (bd / "templates").mkdir(parents=True)
    (bd / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    return bd


def _run(workflow, bundle, run_dir, *, vision=None, worklists=None, params=None):
    backend = FakeBackend()
    report = Replayer(backend, vision=vision or FakeVision(), poll_interval_s=0.01).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        worklists=worklists,
        params=params or {},
    )
    return backend, report


# ===========================================================================
# (a) PARAMETER induction
# ===========================================================================


def test_a_varying_value_induces_a_param_constant_stays_literal():
    result = induce_program(mockmed_param_traces())
    assert result.certified is True
    assert result.program is not None

    # 'patient' varied -> a param; 'dose' constant -> a baked literal.
    kinds = {d.field: d.kind for d in result.column_decisions}
    assert kinds["patient"] == "param"
    assert kinds["dose"] == "literal"

    pname = next(d.param_name for d in result.column_decisions if d.field == "patient")
    assert pname in result.param_specs
    assert result.param_specs[pname].example == "Alice Alvarez"

    # The dose literal is frozen in the emitted step; the patient step is a param.
    steps = {
        s.step.id: s.step
        for s in result.program.states.values()
        if s.kind is StateKind.ACTION and s.step is not None
    }
    dose = next(s for s in steps.values() if "dose" in s.intent)
    assert dose.text == "10mg" and dose.param is None
    patient = next(s for s in steps.values() if "patient" in s.intent)
    assert patient.param == pname and patient.text is None


def test_a_param_program_round_trips_with_a_new_value(bundle, tmp_path):
    result = induce_program(mockmed_param_traces())
    pname = next(d.param_name for d in result.column_decisions if d.field == "patient")
    backend, report = _run(
        result.workflow,
        bundle,
        tmp_path / "run",
        params={pname: "Zoe Zhang"},  # a value never demonstrated
    )
    assert report.success is True
    assert backend.actions == [
        ("type", "Zoe Zhang"),
        ("type", "10mg"),
        ("press", "Enter"),
    ]
    assert report.model_calls == 0  # $0 runtime preserved


# ===========================================================================
# (b) LOOP induction
# ===========================================================================


def test_b_differing_repetition_count_induces_a_loop_over_a_relation():
    result = induce_program(mockmed_loop_traces())
    assert result.certified is True

    loops = [s for s in result.program.states.values() if s.kind is StateKind.LOOP]
    assert len(loops) == 1
    loop_state = loops[0]
    rel = loop_state.loop.relation
    assert rel in result.workflow.data_sources
    # The body subflow exists and is bound to the loop.
    assert loop_state.loop.body in result.workflow.subflows

    dec = next(d for d in result.column_decisions if d.kind == "loop")
    assert sorted(dec.counts) == [2, 3]  # counts DIFFER across traces


def test_b_loop_program_replays_body_once_per_row(bundle, tmp_path):
    result = induce_program(mockmed_loop_traces())
    loop_state = next(
        s for s in result.program.states.values() if s.kind is StateKind.LOOP
    )
    rel = loop_state.loop.relation

    # Run-time worklist of a length neither demo used (data-dependent queue).
    backend, report = _run(
        result.workflow,
        bundle,
        tmp_path / "run",
        worklists={
            rel: [
                {"patient": "Q1"},
                {"patient": "Q2"},
                {"patient": "Q3"},
                {"patient": "Q4"},
            ]
        },
    )
    assert report.success is True
    assert backend.actions == [
        ("type", "MockMed General"),
        ("type", "Q1"),
        ("type", "Q2"),
        ("type", "Q3"),
        ("type", "Q4"),
        ("press", "Enter"),
    ]


def test_b_loop_inline_rows_replay_without_a_supplied_worklist(bundle, tmp_path):
    """The bundle is self-contained: the representative (longest) demonstrated
    worklist is inlined, so a run with no worklist still iterates it."""
    result = induce_program(mockmed_loop_traces())
    backend, report = _run(result.workflow, bundle, tmp_path / "run")
    assert report.success is True
    assert backend.actions == [
        ("type", "MockMed General"),
        ("type", "Cara"),
        ("type", "Dan"),
        ("type", "Eve"),
        ("press", "Enter"),
    ]


# ===========================================================================
# (c) BRANCH / optional-step induction
# ===========================================================================


def test_c_optional_dialog_induces_a_guarded_branch():
    result = induce_program(mockmed_optional_traces())
    assert result.certified is True

    branches = [s for s in result.program.states.values() if s.kind is StateKind.BRANCH]
    assert len(branches) == 1
    branch = branches[0]
    # Two arms: guarded (dialog present) + unconditional fall-through.
    guarded = [t for t in branch.transitions if t.guard is not None]
    fall = [t for t in branch.transitions if t.guard is None]
    assert len(guarded) == 1 and len(fall) == 1

    # The guard is PROPOSED / flagged for confirmation, never silently trusted.
    assert any(p.kind == "guard" and p.trusted is False for p in result.proposed)


def test_c_branch_program_dismisses_when_present_skips_when_absent(bundle, tmp_path):
    result = induce_program(mockmed_optional_traces())
    branch = next(
        s for s in result.program.states.values() if s.kind is StateKind.BRANCH
    )
    guard_text = next(t.guard.text for t in branch.transitions if t.guard)

    # Popup PRESENT -> the guarded arm dismisses it.
    v_present = FakeVision()
    v_present.text_results = {guard_text: Match((10, 10), (0, 0, 5, 5))}
    backend, report = _run(
        result.workflow, bundle, tmp_path / "present", vision=v_present
    )
    assert report.success is True
    assert backend.actions == [
        ("type", "Alice"),
        ("press", "S"),
        ("press", "Escape"),
        ("press", "Enter"),
    ]

    # Popup ABSENT -> fall-through skips the dismiss.
    backend2, report2 = _run(
        result.workflow, bundle, tmp_path / "absent", vision=FakeVision()
    )
    assert report2.success is True
    assert backend2.actions == [("type", "Alice"), ("press", "S"), ("press", "Enter")]


def test_c_optional_non_dialog_step_becomes_a_guarded_skip(bundle, tmp_path):
    """An optional step with NO derivable condition (not a dialog) becomes a
    guarded step that SKIPs when its own target is absent -- not a branch."""

    def trace(with_extra: bool) -> Workflow:
        steps = [_type("s_patient", "patient", "Alice")]
        if with_extra:
            # A non-dialog optional click with a resolvable anchor.
            steps.append(
                Step(
                    id="s_flag",
                    intent="mark the chart reviewed",
                    action=ActionKind.CLICK,
                    anchor=Anchor(
                        template="templates/btn.png",
                        region=(100, 100, 50, 20),
                        click_point=(110, 105),
                        ocr_text="Reviewed",
                    ),
                )
            )
        steps.append(_key("s_done", "Enter"))
        return Workflow(name="review", steps=steps)

    result = induce_program([trace(True), trace(False)])
    assert result.certified is True
    assert any(d.kind == "optional" for d in result.column_decisions)
    step = next(
        s.step
        for s in result.program.states.values()
        if s.kind is StateKind.ACTION and s.step and s.step.id == "s_flag"
    )
    assert step.guard is not None and step.guard.on_unmet == "skip"

    # The anchor never resolves (empty vision) -> the guard is unmet -> the
    # step is SKIPPED and the run still succeeds.
    backend, report = _run(result.workflow, bundle, tmp_path / "run")
    assert report.success is True
    assert backend.actions == [("type", "Alice"), ("press", "Enter")]


# ===========================================================================
# (d) CONTRADICTION -> REJECT rather than guess
# ===========================================================================


def test_d_contradictory_traces_are_rejected_not_guessed():
    result = induce_program(mockmed_contradiction_traces())
    assert result.underdetermined is True
    assert result.certified is False
    assert result.program is None  # quarantined -- NOT emitted
    assert result.workflow is None

    u = result.uncertainties[0]
    assert u.kind == "ambiguous_branch"
    assert u.consequential is True  # both arms are irreversible
    # Routed to the disambiguation flow (#74).
    assert u.question is not None


def test_d_proposal_is_flagged_but_never_flips_underdetermined_to_certified():
    proposer = FakeProposer()
    result = induce_program(mockmed_contradiction_traces(), propose=proposer)
    # The proposer WAS consulted...
    assert proposer.calls, "proposer should be asked about the divergence"
    # ...its guess is surfaced (flagged, not trusted)...
    assert result.proposed and all(p.trusted is False for p in result.proposed)
    assert result.uncertainties[0].proposal is not None
    # ...but the point stays underdetermined and the program is NOT emitted.
    assert result.certified is False
    assert result.program is None


# ===========================================================================
# Held-out validation (RFC §3 [5])
# ===========================================================================


def test_held_out_scores_a_good_param_induction_high():
    hv = validate_held_out(mockmed_param_traces())
    assert hv.n_traces == 3
    assert hv.mean == pytest.approx(1.0)  # reproduces every held-out trace


def test_held_out_scores_an_over_specialized_induction_low():
    """Two IDENTICAL demos cannot reveal that the value varies, so the compiler
    freezes it as a literal (over-specialization). Held against a trace with a
    DIFFERENT value, that literal does not reproduce -> a LOW score. Contrast
    with the param induction, which reproduces it exactly."""
    traces = mockmed_param_traces()

    identical = [traces[0], traces[0].model_copy(deep=True)]
    frozen = induce_program(identical)
    # No param was inferred -- the varying field is frozen as a literal.
    assert not frozen.param_specs
    bad = reproduction_score(frozen, traces[1])  # a different patient

    good = induce_program([traces[0], traces[1]])
    good_score = reproduction_score(good, traces[2])

    assert good_score == pytest.approx(1.0)
    assert bad < good_score
    assert bad < 1.0


def test_held_out_reproduces_the_loop_corpus_exactly():
    # A loop is inducible from ANY single held-out fold's training trace (each
    # remaining trace still shows the repeated body), so every fold reproduces.
    assert validate_held_out(mockmed_loop_traces()).mean == pytest.approx(1.0)


def test_held_out_partially_reproduces_a_two_trace_branch_corpus():
    """A branch needs BOTH the present and absent variant to be inducible, so
    leave-one-out over just two traces (training on one) cannot reproduce the
    branch fully -- an HONEST partial score, not a fabricated 1.0. It is still
    high (the linear skeleton reproduces); a third variant would raise it."""
    mean = validate_held_out(mockmed_optional_traces()).mean
    assert 0.7 <= mean < 1.0


# ===========================================================================
# Bootstrap + invariants
# ===========================================================================


def test_single_trace_induces_the_degenerate_linear_bootstrap():
    """One demo is the bootstrap seed (RFC §3 [1]): a linear program, every
    typed value a literal (a single demo cannot reveal a parameter)."""
    [one] = [mockmed_param_traces()[0]]
    result = induce_program([one])
    assert result.certified is True
    assert result.program is not None
    assert not result.param_specs  # nothing varies -> nothing is a param
    assert all(
        d.kind in ("literal",)
        for d in result.column_decisions
        if d.field in ("patient", "dose")
    )


def test_induction_makes_zero_model_calls_and_replays_at_zero_cost(bundle, tmp_path):
    proposer = FakeProposer()
    result = induce_program(mockmed_param_traces(), propose=proposer)
    pname = next(d.param_name for d in result.column_decisions if d.field == "patient")
    _, report = _run(
        result.workflow, bundle, tmp_path / "run", params={pname: "New Name"}
    )
    # The proposer is deterministic (no model); the runtime is $0 / 0-call.
    assert report.model_calls == 0
    assert report.est_model_cost_usd == 0.0


def test_alignment_failure_on_unrelated_traces_is_refused():
    """Traces that share no aligned steps are not the same task -- refuse rather
    than induce noise."""
    a = Workflow(name="a", steps=[_type("x", "alpha", "1"), _key("y", "F1")])
    b = Workflow(name="b", steps=[_type("z", "omega", "2"), _key("w", "F9")])
    result = induce_program([a, b])
    assert result.certified is False
    assert result.program is None
    assert result.uncertainties[0].kind == "alignment_failure"
