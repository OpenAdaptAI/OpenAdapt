"""Certification-lint integration for the effect-verifier kit.

- `lint` WARNS per consequential (irreversible) step with no declared
  system-of-record effect (`missing_effect_contract`) and reports the
  bundle's per-consequential-step effect coverage %.
- `certify` FAILS the same gap when the policy opts in
  (`require_effects_for_irreversible: true`) -- warn-vs-fail is the
  policy's choice.
"""

from __future__ import annotations

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.policy import Policy, evaluate_policy, lint_workflow
from openadapt_flow.runtime.effects import Effect, EffectKind

_PC = [Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved OK")]
_EFFECT = Effect(
    kind=EffectKind.RECORD_WRITTEN,
    match={"patient_id": "p1"},
    expected_count=1,
)


def _step(step_id: str, *, risk: str = "reversible", effects=None) -> Step:
    return Step(
        id=step_id,
        intent=f"click '{step_id}'",
        action=ActionKind.CLICK,
        risk=risk,
        anchor=Anchor(
            template=f"{step_id}.png",
            region=(0, 0, 10, 10),
            click_point=(5, 5),
            ocr_text="Save",
            context_text="Row 42 Jane Doe",
        ),
        identity_armed=True,
        expect=list(_PC),
        effects=list(effects or []),
    )


def _wf(*steps: Step) -> Workflow:
    return Workflow(name="kit", steps=list(steps))


class TestLintEffectCoverage:
    def test_missing_contract_on_consequential_step_warns(self):
        report = lint_workflow(_wf(_step("save", risk="irreversible")))
        findings = {(f.code, f.severity) for f in report.findings}
        assert ("missing_effect_contract", "warn") in findings
        assert report.consequential_steps == 1
        assert report.effect_covered_consequential_steps == 0
        assert report.effect_coverage == 0.0

    def test_covered_consequential_step_no_finding(self):
        report = lint_workflow(
            _wf(_step("save", risk="irreversible", effects=[_EFFECT]))
        )
        assert not any(f.code == "missing_effect_contract" for f in report.findings)
        assert report.consequential_steps == 1
        assert report.effect_covered_consequential_steps == 1
        assert report.effect_coverage == 1.0

    def test_reversible_steps_not_counted(self):
        report = lint_workflow(_wf(_step("nav")))
        assert report.consequential_steps == 0
        assert report.effect_coverage is None
        assert not any(f.code == "missing_effect_contract" for f in report.findings)

    def test_mixed_coverage_fraction(self):
        report = lint_workflow(
            _wf(
                _step("nav"),
                _step("save_a", risk="irreversible", effects=[_EFFECT]),
                _step("save_b", risk="irreversible"),
            )
        )
        assert report.consequential_steps == 2
        assert report.effect_covered_consequential_steps == 1
        assert report.effect_coverage == 0.5

    def test_render_shows_coverage_percent(self):
        text = lint_workflow(
            _wf(
                _step("save_a", risk="irreversible", effects=[_EFFECT]),
                _step("save_b", risk="irreversible"),
            )
        ).render()
        assert "effect coverage: 1/2 consequential step(s)" in text
        assert "50%" in text

    def test_render_na_without_consequential_steps(self):
        assert "effect coverage: n/a" in lint_workflow(_wf(_step("nav"))).render()


class TestCertifyEscalation:
    def test_policy_off_gap_does_not_fail_certification(self):
        wf = _wf(_step("save", risk="irreversible"))
        report = evaluate_policy(wf, Policy(name="lenient"))
        assert report.passed

    def test_policy_on_gap_fails_certification(self):
        wf = _wf(_step("save", risk="irreversible"))
        policy = Policy(name="strict", require_effects_for_irreversible=True)
        report = evaluate_policy(wf, policy)
        assert not report.passed
        assert any(
            v.rule == "require_effects_for_irreversible" and v.step_id == "save"
            for v in report.violations
        )

    def test_policy_on_covered_step_passes(self):
        wf = _wf(_step("save", risk="irreversible", effects=[_EFFECT]))
        policy = Policy(name="strict", require_effects_for_irreversible=True)
        assert evaluate_policy(wf, policy).passed

    def test_policy_on_ignores_reversible_steps(self):
        wf = _wf(_step("nav"))
        policy = Policy(name="strict", require_effects_for_irreversible=True)
        assert evaluate_policy(wf, policy).passed

    def test_policy_yaml_round_trip(self, tmp_path):
        (tmp_path / "p.yaml").write_text(
            "name: kit-strict\nrequire_effects_for_irreversible: true\n"
        )
        from openadapt_flow.policy import load_policy

        policy = load_policy(tmp_path / "p.yaml")
        assert policy.require_effects_for_irreversible is True
