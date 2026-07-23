"""Policy engine: lint reports coverage gaps, certify enforces a policy
(fails a bundle with unarmed clicks under a strict policy, passes a clean one),
and the shipped example policies parse.
"""

from __future__ import annotations

import pytest

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.policy import (
    Policy,
    builtin_policy_names,
    evaluate_policy,
    is_identity_armed,
    lint_workflow,
    load_policy,
    step_confidence,
)

_PC = [Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved OK")]


def _click(
    step_id: str,
    intent: str,
    *,
    ocr: str | None = "Button",
    armed: bool = True,
    risk: str = "reversible",
    expect=None,
) -> Step:
    return Step(
        id=step_id,
        intent=intent,
        action=ActionKind.CLICK,
        risk=risk,
        anchor=Anchor(
            template=f"{step_id}.png",
            region=(0, 0, 10, 10),
            click_point=(5, 5),
            ocr_text=ocr,
            context_text="Row 42 Jane Doe" if armed else None,
        ),
        identity_armed=armed,
        identity_unarmed_reason=None if armed else "no readable row band",
        expect=list(expect) if expect is not None else list(_PC),
    )


def _clean_workflow() -> Workflow:
    """Every click armed and verified; the save step irreversible + verified."""
    return Workflow(
        name="clean",
        steps=[
            _click("step_000", "click 'Open chart'", ocr="Open"),
            _click(
                "step_001",
                "click 'Save'",
                ocr="Save",
                risk="irreversible",
                expect=_PC,
            ),
        ],
    )


def _gappy_workflow() -> Workflow:
    """An unarmed navigation click and an unarmed, vacuous irreversible save."""
    return Workflow(
        name="gappy",
        steps=[
            _click("step_000", "click 'Belford, Phil'", ocr="Belford", armed=False),
            _click(
                "step_001",
                "click 'Save as new message'",
                ocr="Save",
                armed=False,
                risk="irreversible",
                expect=[],  # vacuous
            ),
        ],
    )


# --- lint -------------------------------------------------------------------


class TestLint:
    def test_clean_workflow_has_no_errors(self):
        report = lint_workflow(_clean_workflow())
        assert report.counts()["error"] == 0
        assert report.max_severity in ("info", "warn")

    def test_reports_unarmed_and_vacuous_gaps(self):
        report = lint_workflow(_gappy_workflow())
        codes = {(f.code, f.step_id) for f in report.findings}
        # unarmed navigation click (reversible -> warn)
        assert ("unarmed_click", "step_000") in codes
        # unarmed IRREVERSIBLE save -> error
        assert ("unarmed_click", "step_001") in codes
        # vacuous IRREVERSIBLE save -> error
        assert ("vacuous_postcondition", "step_001") in codes
        # a gap on an irreversible step escalates to error
        assert report.counts()["error"] >= 1
        assert report.max_severity == "error"

    def test_under_classified_risk_flagged(self):
        # A write-shaped label left reversible (e.g. a pre-auto-classify bundle).
        wf = Workflow(
            name="old",
            steps=[
                _click("step_000", "click 'Submit'", ocr="Submit", risk="reversible")
            ],
        )
        report = lint_workflow(wf)
        assert any(f.code == "under_classified_risk" for f in report.findings)

    def test_render_is_stringy(self):
        assert "lint" in lint_workflow(_gappy_workflow()).render()


# --- certify ----------------------------------------------------------------


STRICT = Policy(
    name="strict",
    prohibit_unarmed_clicks=True,
    prohibit_vacuous_postconditions=True,
    require_identity_for=["entity_navigation", "write"],
    require_effect_verification_for=["write", "save", "submit"],
    max_unverified_steps=0,
    require_human_approval_below_confidence=0.8,
)


class TestCertify:
    def test_legacy_identifier_crop_is_identity_armed(self):
        step = _click("step_000", "click 'Remote row'", armed=False)
        step.identity_armed = None
        assert step.anchor is not None
        step.anchor.identifier_crop = "templates/identifiers/step_000.png"
        assert is_identity_armed(step)

    def test_strict_fails_bundle_with_unarmed_clicks(self):
        report = evaluate_policy(_gappy_workflow(), STRICT)
        assert not report.passed
        rules = {v.rule for v in report.violations}
        assert "prohibit_unarmed_clicks" in rules
        assert "prohibit_vacuous_postconditions" in rules
        assert "require_effect_verification_for" in rules
        assert "max_unverified_steps" in rules
        # every violation names the (or a) offending step where applicable
        assert any(v.step_id == "step_001" for v in report.violations)

    def test_strict_passes_clean_bundle(self):
        report = evaluate_policy(_clean_workflow(), STRICT)
        assert report.passed, report.render()
        assert report.violations == []

    def test_bare_policy_asserts_nothing(self):
        # A rule-less policy passes even a gappy bundle (all rules opt-in).
        report = evaluate_policy(_gappy_workflow(), Policy(name="empty"))
        assert report.passed

    def test_prohibit_unarmed_only_flags_unarmed(self):
        pol = Policy(name="p", prohibit_unarmed_clicks=True)
        clean = evaluate_policy(_clean_workflow(), pol)
        gappy = evaluate_policy(_gappy_workflow(), pol)
        assert clean.passed
        assert not gappy.passed

    def test_human_approval_rule_flags_low_confidence(self):
        # A template-only click (no ocr, no identity) scores 0.5 < 0.8.
        thin = Step(
            id="step_000",
            intent="click at (5, 5)",
            action=ActionKind.CLICK,
            anchor=Anchor(template="t.png", region=(0, 0, 10, 10), click_point=(5, 5)),
        )
        assert step_confidence(thin) == pytest.approx(0.5)
        wf = Workflow(name="thin", steps=[thin])
        pol = Policy(name="p", require_human_approval_below_confidence=0.8)
        report = evaluate_policy(wf, pol)
        assert not report.passed
        assert report.violations[0].rule == "require_human_approval_below_confidence"


# --- policy loading / example policies --------------------------------------


class TestPolicyLoading:
    def test_builtins_present(self):
        names = builtin_policy_names()
        assert "permissive" in names
        assert "clinical-write" in names

    @pytest.mark.parametrize("name", ["permissive", "clinical-write"])
    def test_example_policies_parse(self, name):
        pol = load_policy(name)
        assert isinstance(pol, Policy)
        assert pol.name == name

    def test_load_by_path(self, tmp_path):
        p = tmp_path / "custom.yaml"
        p.write_text("name: custom\nprohibit_unarmed_clicks: true\n")
        pol = load_policy(p)
        assert pol.name == "custom"
        assert pol.prohibit_unarmed_clicks is True

    def test_unknown_policy_raises(self):
        with pytest.raises(FileNotFoundError):
            load_policy("does-not-exist")

    def test_typo_rule_key_is_rejected(self, tmp_path):
        # extra="forbid": a mistyped rule must fail loudly, never silently no-op.
        p = tmp_path / "typo.yaml"
        p.write_text("name: typo\nprohibit_unarmed_click: true\n")  # missing 's'
        with pytest.raises(ValueError):
            load_policy(p)

    def test_clinical_write_fails_a_gappy_bundle(self):
        # Integration: the shipped strict policy refuses a gappy bundle.
        report = evaluate_policy(_gappy_workflow(), load_policy("clinical-write"))
        assert not report.passed

    def test_permissive_passes_clean_and_flags_unverified_write(self):
        assert evaluate_policy(_clean_workflow(), load_policy("permissive")).passed
        # An irreversible write with no postcondition trips the permissive floor.
        wf = Workflow(
            name="w",
            steps=[
                _click(
                    "step_000",
                    "click 'Save'",
                    ocr="Save",
                    risk="irreversible",
                    expect=[],
                )
            ],
        )
        assert not evaluate_policy(wf, load_policy("permissive")).passed
