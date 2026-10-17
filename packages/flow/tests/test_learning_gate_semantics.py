"""The program regression gate compares program SEMANTICS, not step IDs.

Both external reviews of the halt->learn loop found the same hole: the gate only
checked the per-step identity/effect/risk of step ids that SURVIVE from the
active program into the candidate. A candidate could therefore silently

  * remove an identity-armed safety step (no surviving id -> never gated),
  * remove a system-of-record effect verification,
  * replace a consequential step with a NEW id,
  * add a new irreversible step with NO effects,
  * downgrade a write's risk or drop its operator-confirmation requirement, or
  * broaden a write's execution domain (make it reachable under more conditions)

and still PASS. These tests pin the fix: each weakening must QUARANTINE the
candidate (the gate fails) and leave the active version unchanged, while a benign
improvement that touches no consequential action still PASSES.

Steps are matched across versions by structural ROLE (action kind + intent), not
raw ``step.id`` -- so a renamed-but-equivalent write is still recognised as the
SAME action (no false alarm) and a genuinely-new consequential step is caught.
"""

from __future__ import annotations

from typing import Callable

import pytest

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Guard,
    Predicate,
    PredicateKind,
    ProgramGraph,
    State,
    StateKind,
    Step,
    Transition,
)
from openadapt_flow.learning import (
    SkillLibrary,
    learn_from_traces,
    program_regression_gate,
)
from openadapt_flow.learning.synth_stream import (
    Drift,
    StructuralDiffInducer,
    generate_stream,
    mockmed_base_program,
)

# -- program builders ---------------------------------------------------------


def _base() -> ProgramGraph:
    """The canonical MockMed program: sign in -> open patient (identity-armed) ->
    encounter -> note -> SAVE (irreversible, ``record_written`` effect)."""
    return mockmed_base_program()


def _approval_base() -> ProgramGraph:
    """Base whose SAVE write additionally requires operator confirmation (an
    unconfirmed system-of-record binding)."""
    g = mockmed_base_program()
    for e in g.states["s_save"].step.effects:  # type: ignore[union-attr]
        e.needs_operator_confirmation = True
    return g


def _gated_write_base() -> ProgramGraph:
    """Base whose SAVE write only executes when ``encounter_type == Triage`` (a
    HALT precondition guard) -- so the write's execution domain is narrowed."""
    g = mockmed_base_program()
    g.states["s_save"].step.guard = Guard(  # type: ignore[union-attr]
        predicate=Predicate(
            kind=PredicateKind.PARAM_EQUALS, param="encounter_type", value="Triage"
        ),
        on_unmet="halt",
    )
    return g


# -- damage functions (applied to a candidate copy) ---------------------------


def _drop_open(g: ProgramGraph) -> None:
    """Remove the identity-armed ``s_open`` step and rewire around it."""
    removed = g.states.pop("s_open")
    target = removed.transitions[0].target
    for st in g.states.values():
        for tr in st.transitions:
            if tr.target == "s_open":
                tr.target = target


def _drop_save_effect(g: ProgramGraph) -> None:
    g.states["s_save"].step.effects = []  # type: ignore[union-attr]


def _downgrade_save_risk(g: ProgramGraph) -> None:
    g.states["s_save"].step.risk = "reversible"  # type: ignore[union-attr]


def _drop_save_approval(g: ProgramGraph) -> None:
    for e in g.states["s_save"].step.effects:  # type: ignore[union-attr]
        e.needs_operator_confirmation = False


def _drop_save_guard(g: ProgramGraph) -> None:
    g.states["s_save"].step.guard = None  # type: ignore[union-attr]


def _add_unverified_write(g: ProgramGraph) -> None:
    """Splice in a NEW irreversible action carrying no effect contract."""
    note = g.states["s_note"]
    target = note.transitions[0].target
    g.states["s_del"] = State(
        id="s_del",
        kind=StateKind.ACTION,
        step=Step(
            id="s_del",
            intent="Delete prior draft",
            action=ActionKind.CLICK,
            anchor=Anchor(template="t.png", region=(0, 0, 4, 4), click_point=(2, 2)),
            risk="irreversible",
        ),
        transitions=[Transition(target=target)],
    )
    note.transitions[0].target = "s_del"


# =====================================================================
# DIRECT gate: each weakening fails; a benign improvement passes
# =====================================================================


def test_dropped_identity_armed_step_fails_gate():
    """Dropping the identity-armed ``s_open`` shrinks the dominating identity
    checks of the SAVE write it precedes -- even though no surviving-id anchor
    regressed."""
    base = _base()
    cand = base.model_copy(deep=True)
    _drop_open(cand)

    report = program_regression_gate(base, cand)
    assert not report.passed
    assert any("dominating identity" in f for f in report.failures), report.failures
    # the lost guard is named by the step it protected
    assert any("s_open" in f for f in report.failures)
    # ... and it is still surfaced as a removed armed step (back-compat).
    assert "s_open" in report.removed and "s_open" in report.armed_removed


def test_dropped_effect_requirement_fails_gate():
    base = _base()
    cand = base.model_copy(deep=True)
    _drop_save_effect(cand)

    report = program_regression_gate(base, cand)
    assert not report.passed
    assert any("effect contract" in f for f in report.semantic_failures)


def test_new_consequential_step_without_effects_fails_gate():
    base = _base()
    cand = base.model_copy(deep=True)
    _add_unverified_write(cand)

    report = program_regression_gate(base, cand)
    assert not report.passed
    assert any("without effects" in f.lower() for f in report.semantic_failures)
    assert any("s_del" in f for f in report.semantic_failures)


def test_risk_downgrade_fails_gate():
    base = _base()
    cand = base.model_copy(deep=True)
    _downgrade_save_risk(cand)

    report = program_regression_gate(base, cand)
    assert not report.passed
    assert any("risk downgraded" in f for f in report.semantic_failures)


def test_lost_approval_requirement_fails_gate():
    base = _approval_base()
    cand = base.model_copy(deep=True)
    _drop_save_approval(cand)

    report = program_regression_gate(base, cand)
    assert not report.passed
    assert any("operator confirmation" in f for f in report.semantic_failures)


def test_write_reachable_under_broader_conditions_fails_gate():
    """Dropping a write's HALT precondition guard makes it execute in cases it
    did not before -- the execution domain broadens."""
    base = _gated_write_base()
    cand = base.model_copy(deep=True)
    _drop_save_guard(cand)

    report = program_regression_gate(base, cand)
    assert not report.passed
    assert any("execution domain broadened" in f for f in report.semantic_failures)


def test_benign_added_branch_passes():
    """Adding a handled OPTIONAL branch (a skip-guarded, reversible, unarmed
    step) that touches no consequential action does NOT trip the gate."""
    base = _base()
    cand = base.model_copy(deep=True)
    cand.states["s_survey"] = State(
        id="s_survey",
        kind=StateKind.ACTION,
        step=Step(
            id="s_survey",
            intent="Dismiss survey modal",
            action=ActionKind.CLICK,
            anchor=Anchor(template="t.png", region=(0, 0, 4, 4), click_point=(2, 2)),
            guard=Guard(
                predicate=Predicate(kind=PredicateKind.TEXT_PRESENT, text="Survey"),
                on_unmet="skip",
            ),
        ),
        transitions=[Transition(target="s_open")],
    )
    cand.states["s_login"].transitions[0].target = "s_survey"

    report = program_regression_gate(base, cand)
    assert report.passed, report.failures
    assert not report.semantic_failures


# =====================================================================
# Matching by structural ROLE, not raw step.id
# =====================================================================


def _rename_save(g: ProgramGraph, *, new_id: str) -> None:
    save = g.states.pop("s_save")
    save.id = new_id
    save.step.id = new_id  # type: ignore[union-attr]
    g.states[new_id] = save
    for st in g.states.values():
        for tr in st.transitions:
            if tr.target == "s_save":
                tr.target = new_id


def test_renamed_equivalent_consequential_step_passes():
    """A write whose step.id is renamed but whose role (action + intent),
    effects, and risk are unchanged is matched by ROLE -> no false alarm."""
    base = _base()
    cand = base.model_copy(deep=True)
    _rename_save(cand, new_id="s_commit")

    report = program_regression_gate(base, cand)
    assert report.passed, report.failures


def test_renamed_consequential_step_that_drops_effects_fails_gate():
    """Replacing a consequential step with a NEW id AND dropping its effect
    contract (the 'replace with a new id' hole) is caught by role-matching."""
    base = _base()
    cand = base.model_copy(deep=True)
    _rename_save(cand, new_id="s_commit")
    cand.states["s_commit"].step.effects = []  # type: ignore[union-attr]

    report = program_regression_gate(base, cand)
    assert not report.passed
    assert any("effect contract" in f for f in report.semantic_failures)


# =====================================================================
# Through the LOOP: a gate failure quarantines, active version unchanged
# =====================================================================


class _DamageInducer:
    """Induces the valid consent-dialog revision, then applies one damage -- the
    stand-in for a learned revision that silently weakens a safety invariant."""

    def __init__(self, damage: Callable[[ProgramGraph], None]) -> None:
        self.damage = damage
        self._ref = StructuralDiffInducer()

    def induce(self, traces, *, base=None) -> ProgramGraph:
        graph = self._ref.induce(traces, base=base)
        self.damage(graph)
        return graph


def _run_damaged_loop(tmp_path, base: ProgramGraph, damage):
    library = SkillLibrary(tmp_path / "skills")
    library.create_skill("mockmed", base)
    stream = generate_stream(n_baseline=5, n_drift=5, drift=Drift.CONSENT_DIALOG)
    out = learn_from_traces(library, "mockmed", stream, inducer=_DamageInducer(damage))
    return library, out


@pytest.mark.parametrize(
    "base_builder, damage, needle",
    [
        (_base, _drop_open, "dominating identity"),
        (_base, _drop_save_effect, "effect contract"),
        (_base, _downgrade_save_risk, "risk downgraded"),
        (_base, _add_unverified_write, "without effects"),
        (_approval_base, _drop_save_approval, "operator confirmation"),
    ],
)
def test_loop_quarantines_semantic_regression(tmp_path, base_builder, damage, needle):
    """Each semantic weakening drives the loop to QUARANTINE the candidate and
    RETAIN the active version (v1) -- no silent adoption of a weakened program."""
    library, out = _run_damaged_loop(tmp_path, base_builder(), damage)

    assert out.action == "quarantined", out.reason
    assert out.gate is not None and not out.gate.passed
    assert needle in " ".join(out.gate.failures), out.gate.failures
    # the active version was never replaced by the weakened candidate
    assert library.active_version("mockmed").version == 1
