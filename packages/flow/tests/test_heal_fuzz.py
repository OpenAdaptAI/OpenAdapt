"""Property-based (fuzz) testing of the healing INVARIANTS.

Healing rewrites a step's anchor from the live frame after a non-``template``
resolution and folds the change into a healed bundle. The unit tests in
:mod:`tests.test_heal` pin specific drifts; this module SEARCHES the space of
anchor geometry, drift locations, and viewport sizes with Hypothesis and asserts
the invariants that must hold for EVERY input. Vision and backend are faked (the
:mod:`tests.test_heal` pattern); PIL only builds/reads PNG fixtures.

Invariants encoded:

1. **A heal never corrupts the workflow.** For an arbitrary anchor drift, the
   healed bundle still LOADS as a valid ``Workflow``, round-trips through JSON,
   and the refreshed anchor is geometrically sound — its region lies inside the
   frame, its click point lies inside its region, and the written template crop
   is a valid PNG of the region's size.

2. **A healed anchor re-resolves and healing is idempotent.** Replaying the
   healed bundle (whose template crop now matches the frame at the healed
   location) resolves via the ``template`` rung and is NOT healed again —
   ``heal_count == 0`` on the second run. This exercises the real replayer
   heal-gate (``resolution.rung != "template"``): a healed anchor is a fixed
   point of the heal transform.
"""

from __future__ import annotations

import io
import shutil
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from PIL import Image

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Step,
    Workflow,
)
from openadapt_flow.runtime.replayer import Replayer

pytestmark = pytest.mark.timeout(600)

_MAX = 200

_COMMON_SETTINGS = dict(
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


def _png(size, color=(240, 240, 240)) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _png_size(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as image:
        return image.size


class Match:
    def __init__(self, point, region, confidence=0.9):
        self.point = point
        self.region = region
        self.confidence = confidence


class FakeVision:
    def __init__(self, frame):
        self._frame = frame
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
    def __init__(self, frame, viewport):
        self._frame = frame
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


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #
@st.composite
def _drift_case(draw):
    """(viewport, anchor, resolved_point) — an anchored click step that drifts:
    its template is absent (so template rungs are skipped) and OCR relocates it
    to ``resolved_point``."""
    vw = draw(st.integers(40, 1600))
    vh = draw(st.integers(40, 1200))
    aw = draw(st.integers(1, vw))
    ah = draw(st.integers(1, vh))
    ax = draw(st.integers(0, vw - aw))
    ay = draw(st.integers(0, vh - ah))
    cx = draw(st.integers(ax, ax + aw))
    cy = draw(st.integers(ay, ay + ah))
    rx = draw(st.integers(0, vw))
    ry = draw(st.integers(0, vh))
    anchor = Anchor(
        template="templates/s1.png",
        region=(ax, ay, aw, ah),
        click_point=(cx, cy),
        ocr_text="Save",
        # No context_text: keeps the identity gate out of the heal path.
    )
    return (vw, vh), anchor, (rx, ry)


def _region_in_frame(region, frame) -> bool:
    x, y, w, h = region
    fw, fh = frame
    return 0 <= x and 0 <= y and w >= 1 and h >= 1 and x + w <= fw and y + h <= fh


def _point_in_region(point, region) -> bool:
    px, py = point
    x, y, w, h = region
    return x <= px <= x + w and y <= py <= y + h


@settings(max_examples=_MAX, **_COMMON_SETTINGS)
@given(case=_drift_case())
def test_heal_produces_valid_bundle_and_is_idempotent(case):
    viewport, anchor, resolved = case
    frame = _png(viewport)

    work = Path(tempfile.mkdtemp(prefix="heal_fuzz_"))
    try:
        bundle = work / "bundle"
        (bundle / "templates").mkdir(parents=True)
        run1 = work / "run1"
        healed = work / "healed"

        # --- Run 1: OCR relocates the target -> heal via the ocr rung. ---
        vision = FakeVision(frame)
        vision.text_results = {
            "Save": Match(resolved, (resolved[0], resolved[1], 1, 1), 0.8)
        }
        backend = FakeBackend(frame, viewport)
        step = Step(
            id="s1",
            intent="click 'Save'",
            action=ActionKind.CLICK,
            anchor=anchor.model_copy(),
        )
        workflow = Workflow(name="wf", viewport=viewport, steps=[step])

        report1 = Replayer(backend, vision=vision).run(
            workflow, bundle_dir=bundle, run_dir=run1, save_healed_to=healed
        )
        assert report1.success is True, report1.results[0].error
        assert report1.heal_count == 1
        assert report1.rung_counts == {"ocr": 1}

        # --- Invariant 1: the healed bundle is a valid, sound artifact. ---
        healed_wf = Workflow.load(healed)
        # Round-trips through JSON unchanged (never corrupts the workflow).
        assert (
            Workflow.model_validate_json(healed_wf.model_dump_json()).model_dump()
            == healed_wf.model_dump()
        )

        new_anchor = healed_wf.steps[0].anchor
        assert new_anchor is not None
        assert new_anchor.click_point == resolved
        assert _region_in_frame(new_anchor.region, viewport), (
            f"healed region {new_anchor.region} escapes frame {viewport}"
        )
        assert _point_in_region(new_anchor.click_point, new_anchor.region), (
            f"healed click {new_anchor.click_point} outside its region "
            f"{new_anchor.region}"
        )
        crop_path = healed / new_anchor.template
        assert crop_path.is_file(), "healed template crop missing"
        _, _, rw, rh = new_anchor.region
        assert _png_size(crop_path.read_bytes()) == (rw, rh), (
            "healed crop size does not match the healed region"
        )

        # --- Invariant 2: replaying the healed bundle template-resolves and is
        # NOT healed again (heal transform fixed point). ---
        vision2 = FakeVision(frame)
        vision2.template_results = [
            Match(new_anchor.click_point, new_anchor.region, 0.99)
        ]
        backend2 = FakeBackend(frame, viewport)
        run2 = work / "run2"
        report2 = Replayer(backend2, vision=vision2).run(
            healed_wf, bundle_dir=healed, run_dir=run2
        )
        assert report2.success is True, report2.results[0].error
        assert report2.rung_counts == {"template": 1}
        assert report2.heal_count == 0, (
            "RE-HEAL: a healed, template-resolving anchor was healed again"
        )
        assert report2.results[0].heal is None
    finally:
        shutil.rmtree(work, ignore_errors=True)
