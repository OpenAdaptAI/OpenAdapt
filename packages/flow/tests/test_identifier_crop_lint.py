"""Lint coverage surfacing for pixel identifier crops (identity-on-pixels).

Mirrors the effect-coverage pattern: `lint` reports how many IDENTITY-ARMED
steps carry a compiler-emitted identifier crop (`anchor.identifier_crop`) and
flags each armed step without one (`missing_identifier_crop`):

- WARN when the step's identity rests only on the OCR band (a pixel
  recording compiled without a crop — the under-armed Citrix/RDP case);
- INFO when structured identity covers the step (the crop only matters for
  a cross-substrate pixel replay);
- the finding carries the compiler's explicit degrade reason
  (`Step.identifier_crop_missing_reason`) when the bundle recorded one.
"""

from __future__ import annotations

from typing import Optional

from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow
from openadapt_flow.policy import lint_workflow


def _step(
    step_id: str,
    *,
    context_text: Optional[str] = "Row 42 Jane Doe DOB 1974-08-21",
    structured_identity: Optional[str] = None,
    identifier_crop: Optional[str] = None,
    missing_reason: Optional[str] = None,
) -> Step:
    armed = bool(context_text or structured_identity)
    return Step(
        id=step_id,
        intent=f"click '{step_id}'",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template=f"templates/{step_id}.png",
            region=(0, 0, 10, 10),
            click_point=(5, 5),
            ocr_text="Open",
            context_text=context_text,
            structured_identity=structured_identity,
            identifier_crop=identifier_crop,
            identifier_region=(0, 0, 10, 10) if identifier_crop else None,
        ),
        identity_armed=armed,
        identifier_crop_missing_reason=missing_reason,
    )


def _wf(*steps: Step) -> Workflow:
    return Workflow(name="idcrop-lint", steps=list(steps))


class TestIdentifierCropCoverage:
    def test_band_only_armed_step_without_crop_warns(self):
        report = lint_workflow(_wf(_step("open")))
        findings = {(f.code, f.severity) for f in report.findings}
        assert ("missing_identifier_crop", "warn") in findings
        assert report.identity_armed_steps == 1
        assert report.identifier_crop_armed_steps == 0
        assert report.identifier_crop_coverage == 0.0

    def test_structured_armed_step_without_crop_is_info(self):
        report = lint_workflow(
            _wf(_step("open", structured_identity="Jane Doe MRN AC50061"))
        )
        findings = {(f.code, f.severity) for f in report.findings}
        assert ("missing_identifier_crop", "info") in findings

    def test_crop_armed_step_has_no_finding_and_full_coverage(self):
        report = lint_workflow(
            _wf(_step("open", identifier_crop="templates/identifiers/open.png"))
        )
        assert not any(f.code == "missing_identifier_crop" for f in report.findings)
        assert report.identity_armed_steps == 1
        assert report.identifier_crop_armed_steps == 1
        assert report.identifier_crop_coverage == 1.0

    def test_unarmed_step_not_counted(self):
        report = lint_workflow(_wf(_step("open", context_text=None)))
        assert report.identity_armed_steps == 0
        assert report.identifier_crop_coverage is None
        assert not any(f.code == "missing_identifier_crop" for f in report.findings)

    def test_finding_carries_compiler_degrade_reason(self):
        report = lint_workflow(
            _wf(_step("open", missing_reason="the marked region was invalid"))
        )
        finding = next(
            f for f in report.findings if f.code == "missing_identifier_crop"
        )
        assert "the marked region was invalid" in finding.message

    def test_mixed_coverage_fraction_and_render_line(self):
        report = lint_workflow(
            _wf(
                _step("a", identifier_crop="templates/identifiers/a.png"),
                _step("b"),
            )
        )
        assert report.identity_armed_steps == 2
        assert report.identifier_crop_armed_steps == 1
        assert report.identifier_crop_coverage == 0.5
        rendered = report.render()
        assert "pixel identity coverage: 1/2 identity-armed step(s)" in rendered

    def test_render_na_when_no_armed_steps(self):
        report = lint_workflow(_wf(_step("open", context_text=None)))
        assert "pixel identity coverage: n/a" in report.render()
