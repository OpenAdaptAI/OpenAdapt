"""Property-based (fuzz) testing of the resolution ladder's INVARIANTS.

The unit tests in :mod:`tests.test_resolver` pin hand-picked ladder scenarios.
This module SEARCHES the space of vision outputs (match points, regions,
confidences) and anchor/step shapes with Hypothesis and asserts the invariants
that must hold for EVERY generated input. Only ``vision`` is faked — the real
resolver math (``_scaled_click_point``, ``_clamp_region_of_size``,
``pad_region``, rung ordering, and the ``is_below_ocr`` risk gate as composed in
``Replayer._resolve_step``) is exercised, so a counterexample is a real bug.

Invariants encoded:

1. **In-viewport point (match rungs).** A point resolved via a rung whose
   evidence is a located vision match — ``template``, ``template_global``,
   ``ocr``, ``grounder`` — always lies within the closed viewport rectangle
   (never off-screen). The ``geometry`` rung is DELIBERATELY excluded: its point
   is an unclamped landmark-offset estimate that is off-screen exactly when the
   true target is off-screen (only its region is clamped) — see
   ``test_geometry_region_within_viewport`` for the invariant that DOES hold
   there.

2. **In-viewport matched region (all rungs).** The matched region a resolution
   returns (used by healing to re-crop) is always within the viewport, for every
   rung including ``geometry`` (whose region is clamped by
   ``_clamp_region_of_size``).

3. **Risk gate holds under fuzzed confidence.** An ``irreversible`` step is
   blocked (no action; an error is returned) by ``Replayer._resolve_step`` if
   and ONLY if it resolved via a below-``ocr`` rung (``geometry``/``grounder``),
   for ANY confidence. The gate keys on the evidence RUNG, never on the
   confidence number — a high-confidence geometry match is still blocked, and a
   low-confidence ocr match is not blocked by this gate.

4. **All rungs abstain -> None (halt).** When every configured evidence source
   abstains (vision returns None everywhere, grounder abstains), the ladder
   returns None — it never fabricates a point.
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from PIL import Image

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Landmark,
    Step,
    StructuralHandle,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.runtime.resolver import (
    RUNG_ORDER,
    is_below_ocr,
    resolve,
)

# Heavy fuzz search; match the repo convention of a generous timeout marker.
pytestmark = pytest.mark.timeout(600)

_MAX = 300

_COMMON_SETTINGS = dict(
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

# Screen bytes are irrelevant when ``viewport`` is passed explicitly and vision
# is faked (FakeVision ignores the frame), so a placeholder avoids per-example
# PNG encoding.
_DUMMY_PNG = b"screen-bytes-ignored-by-fake-vision"


# --------------------------------------------------------------------------- #
# Fakes (mirror tests.test_resolver conventions)
# --------------------------------------------------------------------------- #
class Match:
    def __init__(self, point, region, confidence=0.9):
        self.point = point
        self.region = region
        self.confidence = confidence


class FakeVision:
    """Scripted find_template / find_text namespace (openadapt_flow.vision
    is never imported)."""

    def __init__(self):
        self.template_results: list = []
        self.text_results: dict = {}

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


class FakeGrounder:
    def __init__(self, result=None):
        self.result = result

    def locate(self, screen_png, intent, ocr_text=None):
        return self.result


class FakeBackend:
    """Minimal backend exposing only what _resolve_step reads (viewport)."""

    def __init__(self, viewport):
        self._viewport = viewport

    @property
    def viewport(self):
        return self._viewport

    def locate_structural(self, locator):
        # Only invoked by _resolve_step when anchor.structural is set (the
        # structural-rung gate case); returns a deterministic in-viewport point.
        vw, vh = self._viewport
        return StructuralHandle(
            point=(min(110, vw - 1), min(105, vh - 1)), confidence=1.0
        )


def _png(size) -> bytes:
    image = Image.new("RGB", size, (240, 240, 240))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #
@st.composite
def _viewport(draw) -> tuple[int, int]:
    return draw(st.integers(20, 2000)), draw(st.integers(20, 2000))


@st.composite
def _region_within(draw, viewport) -> tuple[int, int, int, int]:
    """An (x, y, w, h) region wholly inside ``viewport`` (w, h >= 1)."""
    vw, vh = viewport
    w = draw(st.integers(1, vw))
    h = draw(st.integers(1, vh))
    x = draw(st.integers(0, vw - w))
    y = draw(st.integers(0, vh - h))
    return (x, y, w, h)


@st.composite
def _point_within_region(draw, region) -> tuple[int, int]:
    x, y, w, h = region
    return (draw(st.integers(x, x + w)), draw(st.integers(y, y + h)))


_CONFIDENCE = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
)


@st.composite
def _match_rung_case(draw):
    """A (rung, anchor, vision, template_png, viewport, matched_region) tuple
    that resolves EXACTLY at one of the four vision-match rungs, with all vision
    outputs in-frame. ``geometry`` is not among them (its point is unclamped)."""
    viewport = draw(_viewport())
    region = draw(_region_within(viewport))
    click_point = draw(_point_within_region(region))
    matched = draw(_region_within(viewport))
    match_point = draw(_point_within_region(matched))
    conf = draw(_CONFIDENCE)
    rung = draw(st.sampled_from(("template", "template_global", "ocr", "grounder")))

    # Labeled anchor: an ocr_text lets template_global skip the landmark guard,
    # and drives the ocr rung.
    anchor = Anchor(
        template="templates/a.png",
        region=region,
        click_point=click_point,
        ocr_text="Target",
    )
    vision = FakeVision()
    grounder = None
    template_png = None
    m = Match(match_point, matched, conf)

    if rung == "template":
        template_png = b"tpl"
        vision.template_results = [m]
    elif rung == "template_global":
        template_png = b"tpl"
        vision.template_results = [None, m]
    elif rung == "ocr":
        # Skip template rungs entirely; ocr hits.
        vision.text_results = {"Target": m}
    elif rung == "grounder":
        grounder = FakeGrounder(m)  # everything else abstains

    return rung, anchor, vision, grounder, template_png, viewport, matched


# --------------------------------------------------------------------------- #
# Invariant 1 — resolved point within the viewport (match rungs)
# --------------------------------------------------------------------------- #
@settings(max_examples=_MAX, **_COMMON_SETTINGS)
@given(case=_match_rung_case())
def test_match_rung_point_within_viewport(case):
    rung, anchor, vision, grounder, template_png, viewport, _matched = case
    resolved = resolve(
        anchor,
        _DUMMY_PNG,
        vision,
        grounder,
        "intent",
        template_png=template_png,
        viewport=viewport,
    )
    assert resolved is not None, f"expected a {rung} resolution"
    resolution, _region = resolved
    assert resolution.rung == rung
    vw, vh = viewport
    x, y = resolution.point
    assert 0 <= x <= vw and 0 <= y <= vh, (
        f"OFF-SCREEN point {resolution.point} for rung {rung!r} in "
        f"viewport {viewport} (anchor.region={anchor.region}, "
        f"click_point={anchor.click_point})"
    )


# --------------------------------------------------------------------------- #
# Invariant 2 — matched region within the viewport (ALL rungs incl. geometry)
# --------------------------------------------------------------------------- #
@st.composite
def _geometry_case(draw):
    """A case that resolves via the geometry rung (template misses, ocr misses,
    a single landmark is located in-frame with a fuzzed offset)."""
    viewport = draw(_viewport())
    region = draw(_region_within(viewport))
    click_point = draw(_point_within_region(region))
    lm_region = draw(_region_within(viewport))
    lm_point = draw(_point_within_region(lm_region))
    dx = draw(st.integers(-3000, 3000))
    dy = draw(st.integers(-3000, 3000))
    conf = draw(_CONFIDENCE)
    anchor = Anchor(
        template="templates/a.png",
        region=region,
        click_point=click_point,
        ocr_text="Target",
        landmarks=[
            Landmark(
                relation="left_of", ocr_text="Land", distance_px=50, dx_px=dx, dy_px=dy
            )
        ],
    )
    vision = FakeVision()
    vision.text_results = {
        "Target": None,  # ocr rung misses
        "Land": Match(lm_point, lm_region, conf),
    }
    return anchor, vision, viewport


def _region_within_viewport(region, viewport) -> bool:
    x, y, w, h = region
    vw, vh = viewport
    return 0 <= x and 0 <= y and w >= 0 and h >= 0 and x + w <= vw and y + h <= vh


@settings(max_examples=_MAX, **_COMMON_SETTINGS)
@given(case=_match_rung_case())
def test_matched_region_within_viewport_match_rungs(case):
    _rung, anchor, vision, grounder, template_png, viewport, _matched = case
    resolved = resolve(
        anchor,
        _DUMMY_PNG,
        vision,
        grounder,
        "intent",
        template_png=template_png,
        viewport=viewport,
    )
    assert resolved is not None
    _resolution, region = resolved
    assert _region_within_viewport(region, viewport), (
        f"matched region {region} escapes viewport {viewport}"
    )


@settings(max_examples=_MAX, **_COMMON_SETTINGS)
@given(case=_geometry_case())
def test_geometry_region_within_viewport(case):
    """The geometry rung's POINT may be an off-screen estimate (documented), but
    its matched region is clamped by ``_clamp_region_of_size`` and must stay in
    the viewport."""
    anchor, vision, viewport = case
    resolved = resolve(
        anchor,
        _DUMMY_PNG,
        vision,
        None,
        "intent",
        template_png=None,
        viewport=viewport,
    )
    assert resolved is not None and resolved[0].rung == "geometry"
    _resolution, region = resolved
    assert _region_within_viewport(region, viewport), (
        f"clamped geometry region {region} escapes viewport {viewport}"
    )


# --------------------------------------------------------------------------- #
# Invariant 3 — the irreversible risk gate holds under fuzzed confidence
# --------------------------------------------------------------------------- #
@st.composite
def _gate_case(draw):
    """A (rung, risk, confidence) scenario resolving at a chosen rung, driven
    through the REAL ``Replayer._resolve_step`` risk gate."""
    viewport = (400, 300)
    region = (100, 100, 50, 20)
    click_point = (110, 105)
    rung = draw(st.sampled_from(RUNG_ORDER))
    risk = draw(st.sampled_from(("reversible", "irreversible")))
    conf = draw(_CONFIDENCE)
    m = Match((120, 110), (110, 100, 50, 20), conf)

    anchor = Anchor(
        template="templates/a.png",
        region=region,
        click_point=click_point,
        ocr_text="Target",
        landmarks=[
            Landmark(
                relation="left_of", ocr_text="Land", distance_px=40, dx_px=40, dy_px=0
            )
        ],
    )
    vision = FakeVision()
    grounder = None
    if rung == "structural":
        # Structural is the deterministic TOP rung: a recorded locator +
        # backend.locate_structural resolve it (FakeBackend supplies the point).
        anchor.structural = StructuralLocator(selector="#x")
    elif rung == "template":
        vision.template_results = [m]
    elif rung == "template_global":
        vision.template_results = [None, m]
    elif rung == "ocr":
        vision.text_results = {"Target": m}
    elif rung == "geometry":
        vision.text_results = {
            "Target": None,
            "Land": Match((70, 105), (50, 95, 40, 20), conf),
        }
    elif rung == "grounder":
        grounder = FakeGrounder(m)
    return rung, risk, conf, anchor, vision, grounder, viewport


# A constant read-only bundle (one template crop) shared across generated
# inputs — built once so no per-example function-scoped fixture is needed.
_GATE_BUNDLE = Path(tempfile.mkdtemp(prefix="resolver_fuzz_bundle_"))
(_GATE_BUNDLE / "templates").mkdir(parents=True, exist_ok=True)
(_GATE_BUNDLE / "templates" / "a.png").write_bytes(_png((50, 20)))


@settings(max_examples=_MAX, **_COMMON_SETTINGS)
@given(case=_gate_case())
def test_irreversible_gate_blocks_below_ocr_for_any_confidence(case):
    rung, risk, conf, anchor, vision, grounder, viewport = case
    bundle = _GATE_BUNDLE

    backend = FakeBackend(viewport)
    replayer = Replayer(backend, vision=vision, grounder=grounder)
    step = Step(
        id="s1", intent="act", action=ActionKind.CLICK, anchor=anchor, risk=risk
    )

    resolution, _region, error = replayer._resolve_step(
        step, _png(viewport), bundle, Workflow(name="wf", steps=[step])
    )
    assert resolution is not None and resolution.rung == rung, (
        f"expected resolution at rung {rung!r}, got "
        f"{resolution.rung if resolution else None!r}"
    )

    should_block = risk == "irreversible" and is_below_ocr(rung)
    if should_block:
        assert error is not None, (
            f"RISK-GATE BREACH: irreversible step acted on below-ocr rung "
            f"{rung!r} at confidence {conf!r} (no error returned)"
        )
        assert "irreversible" in error
    else:
        assert error is None, (
            f"spurious block for rung {rung!r} risk {risk!r} conf {conf!r}: {error!r}"
        )


# --------------------------------------------------------------------------- #
# Invariant 4 — every source abstains -> None (never a fabricated point)
# --------------------------------------------------------------------------- #
@st.composite
def _abstaining_anchor(draw):
    """An anchor with a fuzzed selection of evidence sources present, plus flags
    for whether template bytes / a grounder are supplied — but the vision layer
    and grounder are wired to ABSTAIN on all of them."""
    viewport = draw(_viewport())
    region = draw(_region_within(viewport))
    click_point = draw(_point_within_region(region))
    has_template_bytes = draw(st.booleans())
    has_ocr = draw(st.booleans())
    n_landmarks = draw(st.integers(0, 3))
    has_grounder = draw(st.booleans())

    anchor = Anchor(
        template="templates/a.png",
        region=region,
        click_point=click_point,
        ocr_text="Target" if has_ocr else None,
        landmarks=[
            Landmark(
                relation="left_of", ocr_text=f"L{i}", distance_px=30, dx_px=10, dy_px=0
            )
            for i in range(n_landmarks)
        ],
    )
    template_png = b"tpl" if has_template_bytes else None
    grounder = FakeGrounder(None) if has_grounder else None
    return anchor, template_png, grounder, viewport


@settings(max_examples=_MAX, **_COMMON_SETTINGS)
@given(case=_abstaining_anchor())
def test_all_sources_abstain_returns_none(case):
    anchor, template_png, grounder, viewport = case
    vision = FakeVision()  # find_template / find_text both return None
    resolved = resolve(
        anchor,
        _DUMMY_PNG,
        vision,
        grounder,
        "intent",
        template_png=template_png,
        viewport=viewport,
    )
    assert resolved is None, (
        f"FABRICATED resolution {resolved!r} when every source abstained "
        f"(anchor.ocr_text={anchor.ocr_text!r}, "
        f"{len(anchor.landmarks)} landmarks, template_bytes="
        f"{template_png is not None}, grounder={grounder is not None})"
    )
