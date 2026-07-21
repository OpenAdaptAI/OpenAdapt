"""Unit tests for healing (openadapt_flow.runtime.heal + Replayer wiring).

Vision and backend are faked; PIL is used only to build/verify PNG fixtures.
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
    Resolution,
    Step,
    Workflow,
)
from openadapt_flow.runtime.heal import build_heal_event, write_healed_bundle
from openadapt_flow.runtime.replayer import Replayer

VIEWPORT = (300, 200)


def make_png(size=VIEWPORT, color=(240, 240, 240)) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def png_dims(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as image:
        return image.size


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
        self.ocr_calls: list = []
        self.settle_count = 0

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
        self.ocr_calls.append(region)
        return self.ocr_lines

    def phash_png(self, png, region=None):
        return "aa"

    def phash_distance(self, a, b):
        return 0

    def wait_settled(self, backend, *, interval_s=0.1, stable_frames=2, timeout_s=3.0):
        self.settle_count += 1
        return backend.screenshot()


class FakeBackend:
    def __init__(self, frame=None, viewport=VIEWPORT):
        self._frame = frame if frame is not None else make_png(viewport)
        self._viewport = viewport
        self.actions: list = []

    @property
    def viewport(self):
        return self._viewport

    def screenshot(self):
        return self._frame

    def click(self, x, y, *, double=False):
        self.actions.append(("click", x, y, double))

    def type_text(self, text):
        self.actions.append(("type", text))

    def press(self, key):
        self.actions.append(("press", key))


def ocr_anchored_step(step_id="s1", template="templates/s1.png") -> Step:
    return Step(
        id=step_id,
        intent="click 'Save Encounter'",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template=template,
            region=(100, 100, 50, 20),
            click_point=(110, 105),
            ocr_text="Save Encounter",
        ),
    )


@pytest.fixture()
def bundle(tmp_path):
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "templates").mkdir(parents=True)
    return bundle_dir


@pytest.fixture()
def run_dir(tmp_path):
    return tmp_path / "run"


def _drifted_vision() -> FakeVision:
    """Template missing/missed; OCR finds the button at a new location."""
    vision = FakeVision()
    vision.text_results = {
        "Save Encounter": Match(
            point=(150, 150), region=(130, 144, 60, 14), confidence=0.8
        )
    }
    vision.ocr_lines = [OcrLine("Submit Encounter", confidence=0.95)]
    return vision


def test_heal_event_created_and_applied_on_ocr_rung(bundle, run_dir):
    vision = _drifted_vision()
    backend = FakeBackend()
    workflow = Workflow(name="wf", steps=[ocr_anchored_step()])

    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )

    assert report.success is True
    assert report.heal_count == 1
    assert report.rung_counts == {"ocr": 1}
    heal = report.results[0].heal
    assert isinstance(heal, HealEvent)
    assert heal.rung_used == "ocr"
    assert heal.applied is True
    assert heal.kind == "anchor_refresh"
    # Old anchor preserved for the reviewable diff.
    assert heal.old_anchor.click_point == (110, 105)
    # New anchor: click point at the resolved location, template-sized region
    # centered on it, ocr_text re-OCRed from the live frame.
    assert heal.new_anchor.click_point == (150, 150)
    assert heal.new_anchor.region == (125, 140, 50, 20)
    assert heal.new_anchor.ocr_text == "Submit Encounter"
    # Applied to the in-memory workflow.
    assert workflow.steps[0].anchor.click_point == (150, 150)
    assert workflow.steps[0].anchor.ocr_text == "Submit Encounter"
    # The click went to the resolved point.
    assert ("click", 150, 150, False) in backend.actions


def test_heal_artifacts_persisted_under_run_dir(bundle, run_dir):
    vision = _drifted_vision()
    workflow = Workflow(name="wf", steps=[ocr_anchored_step()])
    Replayer(FakeBackend(), vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )

    heal_dir = run_dir / "heals" / "s1"
    assert (heal_dir / "heal.json").is_file()
    assert (heal_dir / "template.png").is_file()
    assert (heal_dir / "screen.png").is_file()
    event = HealEvent.model_validate(json.loads((heal_dir / "heal.json").read_text()))
    assert event.step_id == "s1"
    assert event.rung_used == "ocr"
    assert event.applied is True
    assert event.screenshot == "heals/s1/screen.png"
    # The new crop is template-region-sized.
    assert png_dims((heal_dir / "template.png").read_bytes()) == (50, 20)
    # The frame the heal was derived from is the full screenshot.
    assert png_dims((heal_dir / "screen.png").read_bytes()) == VIEWPORT


def test_healed_bundle_written_with_new_and_unchanged_crops(bundle, run_dir, tmp_path):
    # s1 heals via ocr (its template file is absent from the bundle);
    # s2 resolves via template (its crop must be copied unchanged).
    original_s2 = make_png((50, 20), color=(10, 20, 30))
    (bundle / "templates" / "s2.png").write_bytes(original_s2)

    vision = _drifted_vision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    s2 = Step(
        id="s2",
        intent="click 'Open'",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/s2.png",
            region=(100, 100, 50, 20),
            click_point=(110, 105),
            ocr_text="Open",
        ),
    )
    workflow = Workflow(name="wf", steps=[ocr_anchored_step(), s2])
    healed_dir = tmp_path / "healed"

    report = Replayer(FakeBackend(), vision=vision).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        save_healed_to=healed_dir,
    )

    assert report.success is True
    assert report.heal_count == 1
    # Healed workflow.json reflects the refreshed anchor.
    healed_wf = Workflow.load(healed_dir)
    assert healed_wf.steps[0].anchor.click_point == (150, 150)
    assert healed_wf.steps[0].anchor.ocr_text == "Submit Encounter"
    # New crop written at the anchor's template path.
    new_crop = healed_dir / "templates" / "s1.png"
    assert new_crop.is_file()
    assert png_dims(new_crop.read_bytes()) == (50, 20)
    # Unchanged template copied byte-for-byte.
    assert (healed_dir / "templates" / "s2.png").read_bytes() == original_s2


def test_no_heal_when_template_rung_resolves(bundle, run_dir):
    (bundle / "templates" / "s1.png").write_bytes(make_png((50, 20)))
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    workflow = Workflow(name="wf", steps=[ocr_anchored_step()])
    report = Replayer(FakeBackend(), vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert report.heal_count == 0
    assert report.results[0].heal is None
    assert not (run_dir / "heals").exists()


def test_heal_on_template_global_uses_matched_region(bundle, run_dir):
    (bundle / "templates" / "s1.png").write_bytes(make_png((50, 20)))
    vision = FakeVision()
    # Local search misses; global match at a relocated position.
    vision.template_results = [
        None,
        Match(point=(210, 130), region=(200, 120, 50, 20), confidence=0.9),
    ]
    workflow = Workflow(name="wf", steps=[ocr_anchored_step()])
    report = Replayer(FakeBackend(), vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    heal = report.results[0].heal
    assert heal is not None
    assert heal.rung_used == "template_global"
    # For template-based rungs the matched region itself becomes the anchor.
    assert heal.new_anchor.region == (200, 120, 50, 20)
    assert heal.new_anchor.click_point == (210, 125)  # offset-mapped point


def test_build_heal_event_clamps_region_at_frame_edge():
    step = ocr_anchored_step()
    frame = make_png(VIEWPORT)
    vision = FakeVision()  # ocr returns nothing -> old text preserved
    resolution = Resolution(rung="ocr", point=(5, 5), confidence=0.8, elapsed_ms=1.0)
    event, crop = build_heal_event(step, resolution, (0, 0, 12, 8), frame, vision)
    assert event.new_anchor.region == (0, 0, 50, 20)  # clamped in-bounds
    assert event.new_anchor.click_point == (5, 5)
    assert event.new_anchor.ocr_text == "Save Encounter"  # old text kept
    assert png_dims(crop) == (50, 20)
    assert event.applied is False  # build does not apply
    # Re-OCR over the new region, then a full-frame pass for the identity
    # context refresh (region=None).
    assert vision.ocr_calls == [(0, 0, 50, 20), None]
    # Nothing recognized on the frame -> the healed anchor carries no
    # identity context (the check is honestly disabled, never stale).
    assert event.new_anchor.context_text is None


def test_write_healed_bundle_direct(tmp_path):
    src = tmp_path / "src"
    (src / "templates").mkdir(parents=True)
    (src / "templates" / "a.png").write_bytes(make_png((10, 10)))
    workflow = Workflow(name="wf", steps=[ocr_anchored_step("s1")])
    new_crop = make_png((50, 20), color=(1, 2, 3))
    dest = write_healed_bundle(workflow, src, tmp_path / "dest", {"s1": new_crop})
    assert (dest / "workflow.json").is_file()
    assert (dest / "templates" / "a.png").is_file()
    assert (dest / "templates" / "s1.png").read_bytes() == new_crop


def test_heal_refreshes_identity_context_from_live_band(bundle, run_dir):
    """A healed anchor's context_text is re-derived from the live frame at
    the NEW position (its old band text may describe the old surroundings)."""
    vision = _drifted_vision()
    # One line on the resolved row (outside the healed region), one far away.
    vision.ocr_lines = [
        OcrLine("Submit Encounter", region=(126, 141, 48, 18)),  # in region
        OcrLine("Jane Sample chart details", region=(10, 142, 90, 16)),
        OcrLine("Unrelated header far away", region=(10, 10, 90, 16)),
    ]
    step = ocr_anchored_step()
    # Same identity evidence, differently segmented at record time — the
    # pre-click identity check passes, and the heal must refresh the field
    # to what the band reads NOW.
    step.anchor.context_text = "Sample chart details Jane"
    workflow = Workflow(name="wf", steps=[step])
    report = Replayer(FakeBackend(), vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    heal = report.results[0].heal
    assert heal is not None
    # Band at the resolved point (150,150) x region height 20: keeps the
    # same-row line, drops the in-region label and the far-away header.
    assert heal.new_anchor.context_text == "Jane Sample chart details"


def test_heal_recontext_keeps_dob_lines_and_drops_clocks(bundle, run_dir):
    """The heal-time band refresh anchors volatility on TODAY: a DOB line
    (far from any heal date) is identity evidence and must survive the
    refreshed band, while a clock line is dropped. Before the 2026-07-09
    review fix, _recontext passed no reference_date, so EVERY date-bearing
    line — including the DOB, the band's most discriminative evidence —
    was conservatively dropped from healed anchors."""
    vision = _drifted_vision()
    vision.ocr_lines = [
        OcrLine("Submit Encounter", region=(126, 141, 48, 18)),  # in region
        OcrLine("Jane Sample DOB 1980-01-01", region=(10, 142, 90, 16)),
        OcrLine("Updated 14:32", region=(200, 142, 60, 16)),  # clock: drop
    ]
    step = ocr_anchored_step()
    step.anchor.context_text = "Jane Sample DOB 1980-01-01"
    workflow = Workflow(name="wf", steps=[step])
    report = Replayer(FakeBackend(), vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    heal = report.results[0].heal
    assert heal is not None
    assert heal.new_anchor.context_text == "Jane Sample DOB 1980-01-01"
