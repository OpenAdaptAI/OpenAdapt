"""Induction HARDENING: the compiler must refuse to over-certify.

Both external reviews flagged that multi-trace induction is a useful PROTOTYPE
whose output can over-claim. These tests pin the safety posture the hardening
adds on top of ``tests/test_induction.py`` (which pins the happy paths):

1. A flagged Proposal over a CONSEQUENTIAL action does NOT auto-certify -- it
   also emits an :class:`Uncertainty`, so ``certified`` is False until an
   operator confirms. "Absent in some traces" is NOT a silent skip for a
   consequential step -- it becomes a question.
2. The trace-shape validator is named honestly (``structural_trace_coverage``),
   the misleading old name still works but is DEPRECATED, and neither is treated
   as behavioral / certification evidence.
3. A varying selection (which-entity) target becomes a parameter OR an
   Uncertainty -- NEVER a silently frozen demo entity.
4. An ambiguous / consequential loop yields an Uncertainty, not a bogus loop.

All deterministic and model-free (no backend, no VLM).
"""

from __future__ import annotations

import warnings

import pytest

from openadapt_flow.compiler.induction import (
    induce_program,
    reproduction_score,
    structural_trace_coverage,
)
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Effect,
    Step,
    StructuralLocator,
    Workflow,
)

# ===========================================================================
# Helpers
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


def _select(step_id: str, entity: str, *, risk: str = "reversible") -> Step:
    """A CLICK/selection step whose PURPOSE (intent) is value-free but whose
    TARGET (the selected entity) is carried on the anchor -- the 'which patient'
    shape. The intent is deliberately value-free so the selection ALIGNS across
    traces and the varying target is visible as a selection dimension."""
    return Step(
        id=step_id,
        intent="select patient row",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/row.png",
            region=(0, 0, 200, 20),
            click_point=(10, 10),
            ocr_text=entity,
        ),
        risk=risk,
    )


# ===========================================================================
# 1. A flagged proposal over a CONSEQUENTIAL action blocks certification
# ===========================================================================


def test_optional_consequential_step_is_a_question_not_a_silent_skip():
    """A step present in some traces but not others that performs a
    CONSEQUENTIAL (irreversible) action must NOT silently become optional/skip;
    it becomes an Uncertainty and ``certified`` is False."""

    def trace(with_submit: bool) -> Workflow:
        steps = [_type("s_patient", "patient", "Alice")]
        if with_submit:
            steps.append(
                Step(
                    id="s_submit",
                    intent="submit the prior authorization",
                    action=ActionKind.CLICK,
                    anchor=Anchor(
                        template="templates/submit.png",
                        region=(0, 0, 80, 20),
                        click_point=(10, 10),
                        ocr_text="Submit",
                    ),
                    risk="irreversible",
                )
            )
        steps.append(_key("s_done", "Enter"))
        return Workflow(name="prior-auth", steps=steps)

    result = induce_program([trace(True), trace(False)])

    assert result.certified is False
    assert result.program is None  # quarantined, not emitted
    u = next(u for u in result.uncertainties if u.kind == "unconfirmed_optional")
    assert u.consequential is True
    assert u.question is not None  # routed to the disambiguation flow


def test_optional_reversible_step_still_auto_certifies():
    """The hardening is SCOPED to consequential steps -- a reversible optional
    step still compiles to a guarded skip and certifies (no false alarms)."""

    def trace(with_note: bool) -> Workflow:
        steps = [_type("s_patient", "patient", "Alice")]
        if with_note:
            steps.append(
                Step(
                    id="s_note",
                    intent="click the notes field",
                    action=ActionKind.CLICK,
                    anchor=Anchor(
                        template="templates/note.png",
                        region=(0, 0, 80, 20),
                        click_point=(10, 10),
                        ocr_text="Notes",
                    ),
                    risk="reversible",
                )
            )
        steps.append(_key("s_done", "Enter"))
        return Workflow(name="note", steps=steps)

    result = induce_program([trace(True), trace(False)])
    assert result.certified is True
    assert result.program is not None
    assert any(d.kind == "optional" for d in result.column_decisions)


def test_optional_consequential_dialog_branch_needs_confirmation():
    """An optional-DIALOG branch whose handling performs a consequential action
    is PROPOSED (guard flagged) AND emits an Uncertainty -- the proposal alone
    must not flip it to certified."""

    def trace(with_dialog: bool) -> Workflow:
        steps = [_type("s_patient", "patient", "Alice")]
        if with_dialog:
            steps.append(
                _key(
                    "s_confirm",
                    "Y",
                    intent="accept the confirmation dialog and approve the write",
                    risk="irreversible",
                )
            )
        steps.append(_key("s_done", "Enter"))
        return Workflow(name="confirm-write", steps=steps)

    result = induce_program([trace(True), trace(False)])
    # The guard was proposed and flagged (never trusted)...
    assert any(p.kind == "guard" and p.trusted is False for p in result.proposed)
    # ...but a consequential guarded action leaves it underdetermined.
    assert result.certified is False
    u = next(u for u in result.uncertainties if u.kind == "unconfirmed_branch")
    assert u.consequential is True
    assert u.proposal is not None and u.proposal.trusted is False


def test_effect_bearing_step_counts_as_consequential():
    """Consequential is not only ``risk='irreversible'`` -- a step declaring a
    system-of-record EFFECT is consequential too, so an optional effect-bearing
    step is a question, not a silent skip."""

    def trace(with_write: bool) -> Workflow:
        steps = [_type("s_patient", "patient", "Alice")]
        if with_write:
            steps.append(
                Step(
                    id="s_write",
                    intent="save the order",
                    action=ActionKind.CLICK,
                    anchor=Anchor(
                        template="templates/save.png",
                        region=(0, 0, 80, 20),
                        click_point=(10, 10),
                        ocr_text="Save",
                    ),
                    effects=[Effect(kind="record_written")],
                )
            )
        steps.append(_key("s_done", "Enter"))
        return Workflow(name="order", steps=steps)

    result = induce_program([trace(True), trace(False)])
    assert result.certified is False
    assert any(u.kind == "unconfirmed_optional" for u in result.uncertainties)


# ===========================================================================
# 2. Honest validator naming
# ===========================================================================


def test_structural_trace_coverage_exists_and_scores():
    traces = [
        Workflow(name="w", steps=[_type("a", "patient", "Alice"), _key("b", "Enter")]),
        Workflow(name="w", steps=[_type("a", "patient", "Bob"), _key("b", "Enter")]),
    ]
    result = induce_program(traces)
    assert result.certified is True
    # The renamed function is the real implementation.
    score = structural_trace_coverage(result, traces[0])
    assert 0.0 <= score <= 1.0
    assert score == pytest.approx(1.0)


def test_reproduction_score_still_works_but_is_deprecated():
    traces = [
        Workflow(name="w", steps=[_type("a", "patient", "Alice"), _key("b", "Enter")]),
        Workflow(name="w", steps=[_type("a", "patient", "Bob"), _key("b", "Enter")]),
    ]
    result = induce_program(traces)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        old = reproduction_score(result, traces[0])
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)
    # The alias returns exactly what the renamed function returns.
    assert old == pytest.approx(structural_trace_coverage(result, traces[0]))


def test_structural_coverage_is_not_certification():
    """A high structural score is a trace-SHAPE signal only; it is not, alone,
    the certification verdict. A frozen-literal induction can still score < 1.0
    on a held trace with a different value -- coverage and certification are
    distinct axes."""
    a = Workflow(name="w", steps=[_type("a", "patient", "Alice"), _key("b", "Enter")])
    identical = [a, a.model_copy(deep=True)]
    frozen = induce_program(identical)
    # It certifies (a program was emitted with no uncertainty)...
    assert frozen.certified is True
    # ...yet structural coverage against a DIFFERENT value is < 1.0: coverage is
    # not what certifies, and does not claim behavioral correctness.
    other = Workflow(name="w", steps=[_type("a", "patient", "Zoe"), _key("b", "Enter")])
    assert structural_trace_coverage(frozen, other) < 1.0


# ===========================================================================
# 3. Varying selection (which-entity) -> param OR uncertainty, never frozen
# ===========================================================================


def test_varying_selection_target_is_never_a_frozen_demo_entity():
    """A CLICK/selection whose target VARIES across traces must not be baked as a
    literal that silently re-selects the demo entity. It becomes a parameter OR
    an Uncertainty -- never a frozen ``literal`` of the demo's entity."""
    traces = [
        Workflow(
            name="chart", steps=[_select("s_row", "Alice"), _key("s_ok", "Enter")]
        ),
        Workflow(name="chart", steps=[_select("s_row", "Bob"), _key("s_ok", "Enter")]),
    ]
    result = induce_program(traces)

    sel_decs = [d for d in result.column_decisions if d.field == "patient row"]
    assert sel_decs, "the selection column should be recognized (value-free key)"
    dec = sel_decs[0]

    # Acceptable outcomes: a param, OR a flagged selection uncertainty.
    became_param = dec.kind == "param"
    became_uncertain = dec.kind == "ambiguous_selection" or any(
        u.kind == "ambiguous_selection" for u in result.uncertainties
    )
    assert became_param or became_uncertain

    # The critical invariant: NEVER a frozen literal of a demo entity.
    assert dec.kind != "literal"
    assert dec.literal_value not in ("Alice", "Bob")


def test_varying_selection_quarantines_rather_than_freezing():
    """With the current runtime (a click targets its resolved ANCHOR, not a
    param), a varying selection is quarantined with a consequential Uncertainty
    and an advisory entity_ref proposal -- so no program silently ships the demo
    entity."""
    traces = [
        Workflow(
            name="chart", steps=[_select("s_row", "Alice"), _key("s_ok", "Enter")]
        ),
        Workflow(
            name="chart", steps=[_select("s_row", "Carol"), _key("s_ok", "Enter")]
        ),
    ]
    result = induce_program(traces)
    assert result.certified is False
    assert result.program is None
    u = next(u for u in result.uncertainties if u.kind == "ambiguous_selection")
    assert u.consequential is True
    assert u.proposal is not None and u.proposal.kind == "param"


def test_constant_selection_target_still_certifies():
    """A selection whose target is CONSTANT across traces is a genuine fixed
    target (a stable button) and still certifies -- the hardening only fires on
    a VARYING selection."""
    traces = [
        Workflow(
            name="chart", steps=[_select("s_row", "Alice"), _key("s_ok", "Enter")]
        ),
        Workflow(
            name="chart", steps=[_select("s_row", "Alice"), _key("s_ok", "Enter")]
        ),
    ]
    result = induce_program(traces)
    assert result.certified is True
    assert not any(u.kind == "ambiguous_selection" for u in result.uncertainties)


def test_varying_selection_aligns_via_structural_role():
    """Even when the intent embeds the entity name, a value-free STRUCTURAL role
    lets the varying selection align into one column and be caught as a
    selection ambiguity (not a frozen literal)."""

    def sel(entity: str) -> Step:
        return Step(
            id="s_row",
            intent=f"click {entity}",  # value IN the intent
            action=ActionKind.CLICK,
            anchor=Anchor(
                template="templates/row.png",
                region=(0, 0, 200, 20),
                click_point=(10, 10),
                ocr_text=entity,
                structural=StructuralLocator(role="row", name=entity),
            ),
        )

    traces = [
        Workflow(name="chart", steps=[sel("Alice"), _key("s_ok", "Enter")]),
        Workflow(name="chart", steps=[sel("Bob"), _key("s_ok", "Enter")]),
    ]
    result = induce_program(traces)
    assert any(u.kind == "ambiguous_selection" for u in result.uncertainties)
    assert not any(
        d.kind == "literal" and d.literal_value in ("Alice", "Bob")
        for d in result.column_decisions
    )


# ===========================================================================
# 4. Ambiguous / consequential loop -> uncertainty, not a bogus loop
# ===========================================================================


def test_consequential_loop_body_is_refused_not_guessed():
    """A repeated body that performs a CONSEQUENTIAL (irreversible) action is a
    loop-vs-fixed-sequence ambiguity that trace shape cannot resolve; induction
    emits an Uncertainty rather than a possibly-wrong loop over an irreversible
    step."""

    def trace(n_approvals: int) -> Workflow:
        steps: list[Step] = [_type("s_login", "clinic", "MockMed")]
        for i in range(n_approvals):
            steps.append(
                _key(
                    f"s_approve{i}",
                    "A",
                    intent="approve the claim",
                    risk="irreversible",
                )
            )
        steps.append(_key("s_done", "Enter"))
        return Workflow(name="approve-batch", steps=steps)

    result = induce_program([trace(2), trace(3)])

    assert result.certified is False
    assert result.program is None  # no bogus loop emitted
    u = next(u for u in result.uncertainties if u.kind == "ambiguous_loop")
    assert u.consequential is True
    assert u.question is not None
    # And no loop column was emitted as if certified.
    assert not any(d.kind == "loop" for d in result.column_decisions)


def test_reversible_loop_still_induces_a_loop():
    """The loop-honesty gate is scoped to CONSEQUENTIAL bodies: a reversible
    worklist (differing counts) still induces a real loop and certifies."""

    def trace(patients: list[str]) -> Workflow:
        steps: list[Step] = [_type("s_login", "clinic", "MockMed")]
        for i, p in enumerate(patients):
            steps.append(_type(f"s_row{i}", "patient", p))
        steps.append(_key("s_done", "Enter"))
        return Workflow(name="worklist", steps=steps)

    result = induce_program([trace(["A", "B"]), trace(["C", "D", "E"])])
    assert result.certified is True
    assert any(d.kind == "loop" for d in result.column_decisions)
