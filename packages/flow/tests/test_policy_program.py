"""P0 certification-safety regressions for the policy engine.

Two holes let ``clinical-write`` certify an unsafe bundle:

- **P0-1** — ``evaluate_policy`` / ``lint_workflow`` iterated only
  ``Workflow.steps``. A PROGRAM-mode bundle keeps its actions in
  ``program`` / ``subflows`` ACTION states, usually with an EMPTY ``steps`` —
  so a state-machine bundle full of unsafe writes certified as "zero steps"
  (nothing inspected). The checks now traverse the program graph via
  ``traversal.iter_workflow_steps``.
- **P0-2** — the effect requirement checked ``step.expect`` (a SCREEN
  assertion), not ``step.effects`` (the system of record). A clinical write
  certified merely because it carried a ``TEXT_PRESENT`` assertion — the exact
  weak oracle the effect layer replaced. ``clinical-write`` now requires a real
  system-of-record effect (plus an idempotency key, no unconfirmed bindings),
  keeping screen postconditions as an ADDITIONAL requirement.
"""

from __future__ import annotations

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    ProgramGraph,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
)
from openadapt_flow.policy import (
    Policy,
    evaluate_policy,
    lint_workflow,
    load_policy,
)
from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.traversal import iter_workflow_steps

_PC = [Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved OK")]


def _effect(
    *, key: str | None = "idem-123", needs_confirmation: bool = False
) -> Effect:
    """A system-of-record RECORD_WRITTEN effect (idempotency key on by default)."""
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient_id": "p1", "type": "Triage"},
        idempotency_key=key,
        needs_operator_confirmation=needs_confirmation,
        risk="irreversible",
    )


def _write_click(
    step_id: str = "w0",
    *,
    armed: bool = True,
    expect: bool = True,
    effects: list[Effect] | None = None,
) -> Step:
    """An irreversible ('write') click. Fully evidenced by default (template +
    OCR + identity band => confidence 1.0)."""
    return Step(
        id=step_id,
        intent="click 'Save note'",
        action=ActionKind.CLICK,
        risk="irreversible",
        anchor=Anchor(
            template=f"{step_id}.png",
            region=(0, 0, 10, 10),
            click_point=(5, 5),
            ocr_text="Save",
            context_text="Row 42 Jane Doe" if armed else None,
        ),
        identity_armed=armed,
        identity_unarmed_reason=None if armed else "no readable row band",
        expect=list(_PC) if expect else [],
        effects=list(effects) if effects is not None else [],
    )


def _program(*steps: Step, subflow_steps: tuple[Step, ...] = ()) -> Workflow:
    """A PROGRAM-mode workflow whose linear ``steps`` is EMPTY — the actions
    live only in the program graph (and an optional 'body' subflow)."""
    states: dict[str, State] = {}
    ordered: list[str] = []
    for s in steps:
        sid = f"st::{s.id}"
        ordered.append(sid)
        states[sid] = State(
            id=sid,
            kind=StateKind.ACTION,
            step=s,
            transitions=[Transition(target="__end__", label="")],
        )
    states["__end__"] = State(id="__end__", kind=StateKind.TERMINAL, outcome="success")
    program = ProgramGraph(entry=ordered[0] if ordered else "__end__", states=states)

    subflows: dict[str, ProgramGraph] = {}
    if subflow_steps:
        sub: dict[str, State] = {}
        first: str | None = None
        for s in subflow_steps:
            sid = f"sub::{s.id}"
            first = first or sid
            sub[sid] = State(
                id=sid,
                kind=StateKind.ACTION,
                step=s,
                transitions=[Transition(target="__subend__", label="")],
            )
        sub["__subend__"] = State(
            id="__subend__", kind=StateKind.TERMINAL, outcome="success"
        )
        subflows["body"] = ProgramGraph(entry=first or "__subend__", states=sub)

    return Workflow(name="prog", steps=[], program=program, subflows=subflows)


# --- P0-1: program + subflow traversal --------------------------------------


class TestProgramTraversal:
    def test_iter_covers_program_and_subflows(self):
        wf = _program(_write_click("w0"), subflow_steps=(_write_click("s0"),))
        assert wf.steps == []  # actions live ONLY in the graph
        assert {s.id for s in iter_workflow_steps(wf)} == {"w0", "s0"}

    def test_linear_bundle_unchanged(self):
        wf = Workflow(name="lin", steps=[_write_click("w0")])
        assert [s.id for s in iter_workflow_steps(wf)] == ["w0"]

    def test_program_only_unarmed_click_is_refused(self):
        # Previously certified as "zero steps"; now REFUSED (P0-1).
        wf = _program(_write_click("w0", armed=False))
        report = evaluate_policy(wf, load_policy("clinical-write"))
        assert not report.passed
        assert report.n_steps == 1  # the program ACTION state WAS inspected
        assert "prohibit_unarmed_clicks" in {v.rule for v in report.violations}

    def test_unsafe_write_in_subflow_is_refused(self):
        # A safe top-level action but an unarmed write hidden in a subflow.
        wf = _program(
            _write_click("w0", effects=[_effect()]),
            subflow_steps=(_write_click("s0", armed=False),),
        )
        report = evaluate_policy(wf, load_policy("clinical-write"))
        assert not report.passed
        assert any(v.step_id == "s0" for v in report.violations)

    def test_program_only_lint_sees_gaps(self):
        wf = _program(_write_click("w0", armed=False, expect=False))
        report = lint_workflow(wf)
        assert report.n_steps == 1
        codes = {f.code for f in report.findings}
        assert "unarmed_click" in codes
        assert "vacuous_postcondition" in codes

    def test_certifier_reports_true_action_count(self):
        # The bug surface: with steps empty the naive loop saw nothing.
        wf = _program(_write_click("w0", armed=False))
        assert wf.steps == []
        assert evaluate_policy(wf, Policy(name="empty")).n_steps == 1


# --- P0-2: system-of-record effects, not the screen -------------------------


class TestSystemEffects:
    def test_clinical_refuses_screen_only_write(self):
        # Screen postcondition present, but NO system-of-record effect.
        wf = _program(_write_click("w0", expect=True, effects=[]))
        report = evaluate_policy(wf, load_policy("clinical-write"))
        assert not report.passed
        rules = {v.rule for v in report.violations}
        assert "require_system_effects_for" in rules
        assert "require_idempotency_key_for" in rules

    def test_clinical_accepts_write_with_real_effects_and_key(self):
        wf = _program(_write_click("w0", expect=True, effects=[_effect()]))
        report = evaluate_policy(wf, load_policy("clinical-write"))
        assert report.passed, report.render()

    def test_require_idempotency_key_flags_missing_key(self):
        # Effect present (satisfies require_system_effects_for) but no key.
        wf = _program(_write_click("w0", effects=[_effect(key=None)]))
        report = evaluate_policy(wf, load_policy("clinical-write"))
        rules = {v.rule for v in report.violations}
        assert "require_idempotency_key_for" in rules
        assert "require_system_effects_for" not in rules

    def test_prohibit_unconfirmed_effect_bindings(self):
        wf = _program(_write_click("w0", effects=[_effect(needs_confirmation=True)]))
        report = evaluate_policy(wf, load_policy("clinical-write"))
        assert "prohibit_unconfirmed_effect_bindings" in {
            v.rule for v in report.violations
        }

    def test_screen_postcondition_still_required(self):
        # A write with a real effect but NO screen postcondition still fails —
        # screen postconditions are additional, not substituted.
        wf = _program(_write_click("w0", expect=False, effects=[_effect()]))
        report = evaluate_policy(wf, load_policy("clinical-write"))
        rules = {v.rule for v in report.violations}
        assert "require_screen_postconditions_for" in rules


# --- backward compatibility of the deprecated rule --------------------------


class TestDeprecatedEffectVerification:
    def test_alias_checks_screen_postcondition(self):
        pol = Policy(name="old", require_effect_verification_for=["write"])
        ok = _program(_write_click("w0", expect=True, effects=[]))
        assert evaluate_policy(ok, pol).passed

        bad = _program(_write_click("w0", expect=False, effects=[]))
        report = evaluate_policy(bad, pol)
        assert not report.passed
        assert "require_effect_verification_for" in {v.rule for v in report.violations}
