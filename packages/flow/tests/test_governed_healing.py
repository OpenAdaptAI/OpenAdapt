"""Governed healing: the identity-never-weakened invariant, the regression
gate, the promotion pipeline, and the perturbation harness.

The headline safety property (two external reviews): a heal must NEVER
silently weaken a step's identity band. The old heal path could refresh an
armed step's context to ``None`` -- flipping it ARMED -> UNARMED and disabling
the pre-click identity gate for that step while still reporting green. These
tests reproduce that weakening end-to-end and assert it is now blocked (the
patch is quarantined and the run HALTS), plus cover the patch/gate/pipeline
units and the deterministic perturbation harness.

Vision and backend are faked; PIL builds PNG fixtures. No model calls.
"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    HealEvent,
    Step,
    Workflow,
)
from openadapt_flow.runtime.healing import (
    DriftKind,
    HealPatch,
    RegressionGate,
    effect_regression,
    govern_heal,
    identity_preserved,
    perturb,
    perturbation_set,
    replay_patch,
    risk_regression,
    run_promotion,
)
from openadapt_flow.runtime.identity_template import build_identity_template
from openadapt_flow.runtime.replayer import Replayer

VIEWPORT = (300, 200)
ARMED_BAND = "Jane Sample DOB 1980-01-01"


# --------------------------------------------------------------------------- #
# Fixtures / fakes (mirrors tests/test_heal.py)
# --------------------------------------------------------------------------- #


def make_png(size=VIEWPORT, color=(240, 240, 240)) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class Match:
    def __init__(self, point, region, confidence=0.9):
        self.point = point
        self.region = region
        self.confidence = confidence


class OcrLine:
    def __init__(self, text, region=(0, 0, 10, 10), confidence=0.9):
        self.text = text
        self.region = region
        self.confidence = confidence


class FakeVision:
    def __init__(self):
        self.template_results: list = []
        self.text_results: dict = {}
        self.ocr_lines: list = []

    def find_template(
        self,
        screen_png,
        template_png,
        *,
        search_region=None,
        prefer_near=None,
        scales=(0.85, 1.0, 1.18),
        threshold=0.82,
    ):
        if self.template_results:
            return self.template_results.pop(0)
        return None

    def find_text(
        self,
        screen_png,
        text,
        *,
        region=None,
        min_ratio=0.8,
        raise_on_ambiguity=False,
    ):
        del raise_on_ambiguity
        result = self.text_results.get(text)
        if isinstance(result, list):
            return result.pop(0) if result else None
        return result

    def ocr(self, screen_png, *, region=None):
        return self.ocr_lines

    def phash_png(self, png, region=None):
        return "aa"

    def phash_distance(self, a, b):
        return 0

    def wait_settled(self, backend, *, interval_s=0.1, stable_frames=2, timeout_s=3.0):
        return backend.screenshot()


class FakeBackend:
    def __init__(self, frame=None, viewport=VIEWPORT, structured=None):
        self._frame = frame if frame is not None else make_png(viewport)
        self._viewport = viewport
        self.actions: list = []
        # When set, the backend exposes a structured-text tier (browser DOM /
        # a11y tree) returning this value at any point -- lets the pre-click
        # identity gate VERIFY via the highest-fidelity tier.
        self._structured = structured

    @property
    def viewport(self):
        return self._viewport

    def structured_text_at(self, x, y):
        return self._structured

    def screenshot(self):
        return self._frame

    def click(self, x, y, *, double=False):
        self.actions.append(("click", x, y, double))

    def type_text(self, text):
        self.actions.append(("type", text))

    def press(self, key):
        self.actions.append(("press", key))


def armed_step(step_id="s1") -> Step:
    """A click step whose anchor carries an identity band (ARMED)."""
    return Step(
        id=step_id,
        intent="click 'Save Encounter' for Jane Sample",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template=f"templates/{step_id}.png",
            region=(100, 100, 50, 20),
            click_point=(110, 105),
            ocr_text="Save Encounter",
            context_text=ARMED_BAND,
        ),
    )


def anchor(**overrides) -> Anchor:
    base = dict(
        template="templates/s1.png",
        region=(100, 100, 50, 20),
        click_point=(110, 105),
        ocr_text="Save Encounter",
    )
    base.update(overrides)
    return Anchor(**base)


# --------------------------------------------------------------------------- #
# 1. identity_preserved -- the core invariant
# --------------------------------------------------------------------------- #


def test_invariant_blocks_armed_to_unarmed():
    """The reviewed bug: an armed anchor refreshed to no band at all."""
    old = anchor(context_text=ARMED_BAND)
    new = anchor(context_text=None, click_point=(150, 150))
    verdict = identity_preserved(old, new)
    assert verdict.preserved is False
    assert "ARMED -> UNARMED" in verdict.reason


def test_invariant_allows_resegmented_band_that_still_verifies():
    old = anchor(context_text=ARMED_BAND)
    new = anchor(context_text="DOB 1980-01-01 Jane Sample", click_point=(150, 1))
    verdict = identity_preserved(old, new)
    assert verdict.preserved is True
    assert verdict.band_status == "verified"


def test_invariant_blocks_band_that_now_names_a_different_entity():
    old = anchor(context_text=ARMED_BAND)
    new = anchor(context_text="Robert Different DOB 1999-12-31")
    verdict = identity_preserved(old, new)
    assert verdict.preserved is False
    assert verdict.band_status == "mismatch"


def test_invariant_blocks_dropped_structured_identity():
    old = anchor(context_text=ARMED_BAND, structured_identity="MRN 100200")
    new = anchor(context_text=ARMED_BAND, structured_identity=None)
    verdict = identity_preserved(old, new)
    assert verdict.preserved is False
    assert "structured_identity" in verdict.reason


def test_invariant_blocks_dropped_context_even_when_structured_survives():
    old = anchor(context_text=ARMED_BAND, structured_identity="MRN 100200")
    new = anchor(context_text=None, structured_identity="MRN 100200")
    verdict = identity_preserved(old, new)
    # Still armed via structured identity, but the independent OCR tier's band
    # was dropped -- that is a weakening of coverage, so it must be refused.
    assert verdict.preserved is False
    assert "context band" in verdict.reason


def test_invariant_noop_on_unarmed_step():
    """A locator-only heal on a step that never had identity protection is
    fine -- there is nothing to weaken."""
    old = anchor(context_text=None)
    new = anchor(context_text=None, click_point=(150, 150))
    assert identity_preserved(old, new).preserved is True


def _template_armed_anchor() -> Anchor:
    tmpl = build_identity_template(
        ARMED_BAND,
        structured_identity="MRN 100200",
        salt_hex="11" * 16,
    )
    assert tmpl is not None
    return anchor(
        context_text=None,
        structured_identity=None,
        identity_template=tmpl,
        identifier_crop="identity/s1.png",
        identifier_region=(20, 100, 60, 20),
    )


def test_invariant_allows_locator_only_change_on_identical_template_anchor():
    old = _template_armed_anchor()
    new = old.model_copy(
        update={
            "template": "templates/s1-healed.png",
            "region": (140, 100, 50, 20),
            "click_point": (150, 105),
            "ocr_text": "Save",
        },
        deep=True,
    )

    def verifier_must_not_run(recorded: str, observed: str) -> str:
        raise AssertionError((recorded, observed))

    verdict = identity_preserved(old, new, band_verifier=verifier_must_not_run)
    assert verdict.preserved is True
    assert verdict.reason == "identity evidence unchanged"


def test_invariant_blocks_removed_identity_template():
    old = _template_armed_anchor()
    new = old.model_copy(update={"identity_template": None}, deep=True)
    verdict = identity_preserved(old, new)
    assert verdict.preserved is False
    assert "identity_template" in verdict.reason


@pytest.mark.parametrize("mutation", ["band_len", "token", "structured"])
def test_invariant_blocks_changed_identity_template(mutation):
    old = _template_armed_anchor()
    changed = old.identity_template.model_copy(deep=True)
    if mutation == "band_len":
        changed.band_len += 1
    elif mutation == "token":
        changed.tokens[0].c = "00" * 32
    else:
        changed.structured = "ff" * 32
    new = old.model_copy(update={"identity_template": changed}, deep=True)

    verdict = identity_preserved(old, new)
    assert verdict.preserved is False
    assert "identity_template" in verdict.reason


def test_invariant_blocks_changed_structured_identity():
    old = anchor(structured_identity="MRN 100200")
    new = old.model_copy(update={"structured_identity": "MRN 100201"}, deep=True)
    verdict = identity_preserved(old, new)
    assert verdict.preserved is False
    assert "changed structured_identity" in verdict.reason


def test_invariant_blocks_added_structured_identity():
    old = anchor()
    new = old.model_copy(update={"structured_identity": "MRN 100200"}, deep=True)
    verdict = identity_preserved(old, new)
    assert verdict.preserved is False
    assert "added structured_identity" in verdict.reason


@pytest.mark.parametrize(
    "old",
    [
        anchor(),
        anchor(structured_identity="MRN 100200"),
    ],
    ids=["unarmed", "structured-only"],
)
def test_invariant_blocks_added_context_without_recorded_band(old):
    new = old.model_copy(update={"context_text": ARMED_BAND}, deep=True)
    verdict = identity_preserved(old, new)
    assert verdict.preserved is False
    assert "added context_text identity evidence" in verdict.reason


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("identifier_crop", "identity/other.png"),
        ("identifier_region", (21, 100, 60, 20)),
    ],
)
def test_invariant_blocks_changed_pixel_identity_evidence(field, value):
    old = _template_armed_anchor()
    new = old.model_copy(update={field: value}, deep=True)
    verdict = identity_preserved(old, new)
    assert verdict.preserved is False
    assert field in verdict.reason


# --------------------------------------------------------------------------- #
# 2. HealPatch -- reviewable diff
# --------------------------------------------------------------------------- #


def test_patch_classifies_identity_vs_locator_changes():
    old = anchor(context_text=ARMED_BAND)
    new = anchor(context_text=None, click_point=(150, 150), ocr_text="Submit")
    event = HealEvent(step_id="s1", rung_used="ocr", old_anchor=old, new_anchor=new)
    patch = HealPatch.from_event(event)
    fields = {c.field: c for c in patch.changes}
    assert fields["context_text"].identity is True
    assert fields["click_point"].identity is False
    assert fields["ocr_text"].identity is False
    assert patch.identity_before.armed is True
    assert patch.identity_after.armed is False
    # The diff calls identity changes out separately for a reviewer.
    text = patch.diff()
    assert "identity changes:" in text
    assert "locator changes:" in text


def test_patch_classifies_identity_template_change_as_identity():
    old = _template_armed_anchor()
    new = old.model_copy(update={"identity_template": None}, deep=True)
    patch = HealPatch.from_event(
        HealEvent(step_id="s1", rung_used="ocr", old_anchor=old, new_anchor=new)
    )
    fields = {change.field: change for change in patch.changes}
    assert fields["identity_template"].identity is True


# --------------------------------------------------------------------------- #
# 3. regression gate -- effect + risk regressions
# --------------------------------------------------------------------------- #


def test_effect_regression_flags_newly_unverifiable_effect():
    baseline = {"e1": True, "e2": True}
    now = {"e1": lambda: True, "e2": lambda: False}
    reg = effect_regression(baseline, now)
    assert reg.ok is False
    assert reg.newly_unverifiable == ["e2"]


def test_effect_regression_noop_without_baseline():
    assert effect_regression(None, None).ok is True
    assert effect_regression({}, {"e1": lambda: False}).ok is True


def test_risk_regression_blocks_downgrade():
    irr = Step(id="s1", intent="delete", action=ActionKind.CLICK, risk="irreversible")
    rev = irr.model_copy(update={"risk": "reversible"})
    assert risk_regression(irr, rev).ok is False
    assert risk_regression(irr, irr).ok is True


def test_gate_fails_on_identity_and_reports_all_failures():
    old = anchor(context_text=ARMED_BAND)
    new = anchor(context_text=None)
    step = armed_step()
    event = HealEvent(step_id="s1", rung_used="ocr", old_anchor=old, new_anchor=new)
    patch = HealPatch.from_event(event)
    result = RegressionGate().evaluate(
        patch,
        old,
        new,
        old_step=step,
        new_step=step,
    )
    assert result.passed is False
    assert result.identity_ok is False
    assert any("identity regression" in f for f in result.failures)


def test_unchanged_template_does_not_bypass_effect_or_risk_regressions():
    old_anchor = _template_armed_anchor()
    new_anchor = old_anchor.model_copy(
        update={"click_point": (150, 150)},
        deep=True,
    )
    old_step = Step(
        id="s1",
        intent="write",
        action=ActionKind.CLICK,
        anchor=old_anchor,
        risk="irreversible",
    )
    new_step = old_step.model_copy(
        update={"anchor": new_anchor, "risk": "reversible"},
        deep=True,
    )
    patch = HealPatch.from_event(
        HealEvent(
            step_id="s1",
            rung_used="ocr",
            old_anchor=old_anchor,
            new_anchor=new_anchor,
        )
    )
    result = RegressionGate().evaluate(
        patch,
        old_anchor,
        new_anchor,
        old_step=old_step,
        new_step=new_step,
        effect_baseline={"record-written": True},
        effect_now={"record-written": lambda: False},
    )
    assert result.identity_ok is True
    assert result.effect_ok is False
    assert result.risk_ok is False
    assert result.passed is False
    assert any("effect regression" in failure for failure in result.failures)
    assert any("risk regression" in failure for failure in result.failures)


# --------------------------------------------------------------------------- #
# 4. promotion pipeline
# --------------------------------------------------------------------------- #


def test_pipeline_promotes_a_preserving_patch():
    old = anchor(context_text=ARMED_BAND)
    new = anchor(context_text=ARMED_BAND, click_point=(150, 150))
    step = armed_step()
    patch = HealPatch.from_event(
        HealEvent(step_id="s1", rung_used="ocr", old_anchor=old, new_anchor=new)
    )
    outcome = run_promotion(patch, old, new, old_step=step, new_step=step)
    assert outcome.promoted is True
    assert outcome.patch.status == "promoted"
    assert outcome.halt_reason is None


def test_pipeline_quarantines_a_weakening_patch():
    old = anchor(context_text=ARMED_BAND)
    new = anchor(context_text=None)
    step = armed_step()
    patch = HealPatch.from_event(
        HealEvent(step_id="s1", rung_used="ocr", old_anchor=old, new_anchor=new)
    )
    outcome = run_promotion(patch, old, new, old_step=step, new_step=step)
    assert outcome.promoted is False
    assert outcome.patch.status == "quarantined"
    assert outcome.patch.reject_reason
    assert "quarantined" in outcome.halt_reason


def test_pipeline_canary_can_roll_back_a_gate_passing_patch():
    old = anchor(context_text=ARMED_BAND)
    new = anchor(context_text=ARMED_BAND, click_point=(150, 150))
    step = armed_step()
    patch = HealPatch.from_event(
        HealEvent(step_id="s1", rung_used="ocr", old_anchor=old, new_anchor=new)
    )
    outcome = run_promotion(
        patch,
        old,
        new,
        old_step=step,
        new_step=step,
        canary=lambda p: (False, "regressed on prior trace #3"),
    )
    assert outcome.promoted is False
    assert outcome.patch.status == "rolled_back"
    assert "rolled back" in outcome.halt_reason


def test_govern_heal_persists_patch_for_both_verdicts(tmp_path):
    old = anchor(context_text=ARMED_BAND)
    new = anchor(context_text=None)
    step = armed_step()
    event = HealEvent(step_id="s1", rung_used="ocr", old_anchor=old, new_anchor=new)
    outcome = govern_heal(step, event, run_dir=tmp_path)
    assert outcome.promoted is False
    assert outcome.event is None
    patch_json = tmp_path / "heals" / "s1" / "patch.json"
    assert patch_json.is_file()
    persisted = json.loads(patch_json.read_text())
    assert persisted["status"] == "quarantined"


# --------------------------------------------------------------------------- #
# 5. perturbation harness -- deterministic, reusable
# --------------------------------------------------------------------------- #


def test_perturbation_is_deterministic():
    frame = make_png()
    a = perturb(frame, (110, 105), DriftKind.SHIFT)
    b = perturb(frame, (110, 105), DriftKind.SHIFT)
    assert a.frame_png == b.frame_png
    assert a.expected_point == b.expected_point


def test_perturbation_set_tracks_target_through_each_drift():
    frame = make_png()
    cases = {c.kind: c for c in perturbation_set(frame, anchor())}
    assert set(cases) == set(DriftKind)
    # SHIFT moves the target by the shift vector; RETHEME leaves it in place.
    assert cases[DriftKind.SHIFT].expected_point == (110 + 17, 105 + 11)
    assert cases[DriftKind.RETHEME].expected_point == (110, 105)


def test_replay_patch_promotable_when_located_and_verified():
    frame = make_png()
    patch = HealPatch.from_event(
        HealEvent(
            step_id="s1",
            rung_used="ocr",
            old_anchor=anchor(context_text=ARMED_BAND),
            new_anchor=anchor(context_text=ARMED_BAND),
        )
    )
    cases = perturbation_set(frame, anchor())
    report = replay_patch(
        patch,
        cases,
        resolve=lambda png: _expected_for(png, cases),
        sample_band=lambda png, pt: ARMED_BAND,
    )
    assert report.promotable is True
    assert len(report.results) == len(DriftKind)


def test_replay_patch_fails_when_target_not_located():
    frame = make_png()
    patch = HealPatch.from_event(
        HealEvent(
            step_id="s1",
            rung_used="ocr",
            old_anchor=anchor(context_text=ARMED_BAND),
            new_anchor=anchor(context_text=ARMED_BAND),
        )
    )
    cases = perturbation_set(frame, anchor())
    report = replay_patch(
        patch,
        cases,
        resolve=lambda png: None,  # target lost under drift
        sample_band=lambda png, pt: ARMED_BAND,
    )
    assert report.promotable is False
    assert len(report.failures) == len(cases)


def _expected_for(png, cases):
    for case in cases:
        if case.frame_png == png:
            return case.expected_point
    return None


# --------------------------------------------------------------------------- #
# 6. END-TO-END through the Replayer -- the reviewed bug, blocked
# --------------------------------------------------------------------------- #


def _drift_to_ocr_rung(band_lines) -> FakeVision:
    """Template misses; the OCR rung finds the target at a NEW location.

    ``band_lines`` are the full-frame OCR lines the heal re-derives the
    identity band from.
    """
    vision = FakeVision()
    vision.text_results = {
        "Save Encounter": Match(
            point=(150, 150), region=(130, 144, 60, 14), confidence=0.8
        )
    }
    vision.ocr_lines = band_lines
    return vision


def test_e2e_weakening_heal_is_blocked_and_run_halts(tmp_path):
    """The reviewed bug, end to end. An ARMED step drifts and resolves via
    the OCR rung. The pre-click identity gate PASSES here via the structured
    tier (the live DOM/a11y text matches), so the step succeeds and a heal is
    built -- but at the resolved position the OCR band yields no usable text,
    so the OLD heal refreshed ``context_text`` to ``None``: the step's
    independent OCR identity tier would be silently dropped (the healed
    bundle carries a weaker step, still reported green). The governed heal
    must QUARANTINE the patch and HALT the run instead."""
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    run_dir = tmp_path / "run"

    structured = ARMED_BAND + " MRN 100200"
    # The only OCR line is far from the resolved point's band -> the heal's
    # re-derived OCR context is None (the weakening the review reported),
    # while the structured tier still verifies the pre-click gate.
    vision = _drift_to_ocr_rung(
        [OcrLine("Unrelated footer", region=(5, 5, 90, 12), confidence=0.95)]
    )
    step = armed_step()
    step.anchor.structured_identity = structured
    workflow = Workflow(name="wf", viewport=VIEWPORT, steps=[step])
    recorded_anchor = step.anchor.model_copy(deep=True)
    backend = FakeBackend(structured=structured)

    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )

    # The pre-click gate passed via the structured tier ...
    assert report.results[0].identity is not None
    assert report.results[0].identity.status == "verified"
    assert report.results[0].identity.mode == "structured"
    # ... but the run HALTS: the weakening repair was refused, not applied.
    assert report.success is False
    assert report.results[0].ok is False
    assert "quarantined" in report.results[0].error
    assert "context band" in report.results[0].error
    assert report.results[0].heal is None
    assert report.heal_count == 0
    # The in-memory anchor was NOT weakened (OCR identity band intact).
    assert workflow.steps[0].anchor.context_text == ARMED_BAND
    assert workflow.steps[0].anchor == recorded_anchor
    # The quarantined patch is persisted for review.
    patch_json = run_dir / "heals" / "s1" / "patch.json"
    assert patch_json.is_file()
    assert json.loads(patch_json.read_text())["status"] == "quarantined"
    # No healed crop/event was written (the heal never applied).
    assert not (run_dir / "heals" / "s1" / "heal.json").exists()


def test_e2e_benign_drift_heals_and_promotes(tmp_path):
    """A benign locator drift where the identity band is preserved: the heal
    passes the gate, promotes, applies, and the run succeeds."""
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    run_dir = tmp_path / "run"

    # Same identity evidence readable on the resolved row -> band preserved.
    vision = _drift_to_ocr_rung(
        [
            OcrLine("Submit Encounter", region=(126, 141, 48, 18)),  # in region
            OcrLine(ARMED_BAND, region=(10, 142, 110, 16)),  # same-row identity
        ]
    )
    step = armed_step()
    workflow = Workflow(name="wf", viewport=VIEWPORT, steps=[step])

    report = Replayer(FakeBackend(), vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )

    assert report.success is True
    assert report.heal_count == 1
    heal = report.results[0].heal
    assert heal is not None
    assert heal.rung_used == "ocr"
    # Identity band preserved (still armed), locator moved to the resolved pt.
    assert workflow.steps[0].anchor.context_text == ARMED_BAND
    assert workflow.steps[0].anchor.click_point == (150, 150)
    patch_json = run_dir / "heals" / "s1" / "patch.json"
    assert json.loads(patch_json.read_text())["status"] == "promoted"


def test_e2e_unarmed_step_still_heals_without_governance_halt(tmp_path):
    """A step that never had identity protection (unarmed) heals as before --
    the governance layer only blocks WEAKENING, not benign unarmed heals."""
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    run_dir = tmp_path / "run"

    vision = _drift_to_ocr_rung([OcrLine("Submit Encounter", confidence=0.95)])
    step = Step(
        id="s1",
        intent="click Save",
        action=ActionKind.CLICK,
        anchor=anchor(context_text=None),  # UNARMED
    )
    workflow = Workflow(name="wf", viewport=VIEWPORT, steps=[step])

    report = Replayer(FakeBackend(), vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert report.heal_count == 1
    assert (
        json.loads((run_dir / "heals" / "s1" / "patch.json").read_text())["status"]
        == "promoted"
    )
