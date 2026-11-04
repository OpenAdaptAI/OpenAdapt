"""The local grounding model rung for pixel surfaces.

The resolver tests script the model's look-alike list, so they pin the rule
the resolver applies to it: one look-alike resolves, several resolve only when
a landmark singles one out, a text-labelled target is left to OCR, and a
surface with structure never reaches the rung. The candidate-search tests run
the real edge search on drawn images. The model test runs the real pinned
weights when they are present locally and is skipped otherwise.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

import numpy as np
import pytest
from PIL import Image, ImageDraw

from openadapt_flow.ir import Anchor, Landmark
from openadapt_flow.runtime import local_grounder
from openadapt_flow.runtime.grounder import FallbackGrounder, build_grounder
from openadapt_flow.runtime.local_grounder import (
    WEIGHTS,
    WeightsUnavailable,
    edge_candidates,
    ensure_weights,
)
from openadapt_flow.runtime.resolver import (
    LookAlikeTargetsRefused,
    is_below_ocr,
    resolve,
)
from openadapt_flow.vision.ocr import ContradictoryOcrEvidenceError

VIEWPORT = (400, 300)


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


SCREEN = _png(Image.new("RGB", VIEWPORT, (240, 240, 240)))


class _Match:
    def __init__(self, point, region, confidence=0.95):
        self.point = point
        self.region = region
        self.confidence = confidence


class _Vision:
    """No template or target text found; landmarks answer from a table."""

    def __init__(self, landmarks=None):
        self.landmarks = landmarks or {}

    def find_template(self, *args, **kwargs):
        return None

    def find_text(self, screen_png, text, *, region=None, min_ratio=0.8, **kwargs):
        return self.landmarks.get(text)


@dataclass(frozen=True)
class _Candidate:
    region: tuple[int, int, int, int]
    scale: float
    score: float


class _Model:
    MAY_EGRESS = False

    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = 0

    def locate_anchor(self, screen_png, template_png):
        self.calls += 1
        return list(self.candidates)

    def locate(self, screen_png, intent, ocr_text=None):
        return None


def _icon_anchor(**kwargs) -> Anchor:
    values = dict(
        template="templates/icon.png",
        region=(100, 100, 40, 40),
        click_point=(120, 120),
        ocr_text=None,
        search_pad=20,
    )
    values.update(kwargs)
    return Anchor(**values)


def _resolve(anchor, model, vision=None, structural=None):
    return resolve(
        anchor,
        SCREEN,
        vision or _Vision(),
        model,
        "click the save icon",
        template_png=b"crop",
        viewport=VIEWPORT,
        structural=structural,
    )


def test_one_look_alike_resolves_on_the_grounder_rung_below_ocr():
    model = _Model([_Candidate((200, 150, 60, 60), 1.5, 0.93)])
    resolution, region = _resolve(_icon_anchor(), model)
    assert resolution.rung == "grounder"
    assert is_below_ocr(resolution.rung)  # irreversible steps still halt
    # The recorded click offset (20, 20) scales with the matched box (1.5x).
    assert resolution.point == (230, 180)
    assert region == (200, 150, 60, 60)
    assert resolution.confidence == pytest.approx(0.93)


def test_several_look_alikes_without_a_landmark_refuse():
    model = _Model(
        [
            _Candidate((50, 50, 40, 40), 1.0, 0.95),
            _Candidate((250, 50, 40, 40), 1.0, 0.94),
        ]
    )
    with pytest.raises(LookAlikeTargetsRefused):
        _resolve(_icon_anchor(), model)


def test_a_landmark_singles_out_one_look_alike_at_the_candidate_scale():
    # Landmark "Notes" sits 60 px left of the target at the recorded scale.
    anchor = _icon_anchor(
        landmarks=[
            Landmark(
                relation="left_of", ocr_text="Notes", distance_px=60, dx_px=60, dy_px=0
            )
        ]
    )
    vision = _Vision({"Notes": _Match((100, 200), (80, 190, 40, 20))})
    model = _Model(
        [
            # At 1.5x the landmark predicts the target at (190, 200).
            _Candidate((160, 170, 60, 60), 1.5, 0.95),
            _Candidate((300, 170, 60, 60), 1.5, 0.95),
        ]
    )
    resolution, _region = _resolve(anchor, model, vision)
    assert resolution.point == (190, 200)


def test_overlapping_look_alikes_mean_a_self_similar_crop_and_the_rung_abstains():
    # A crop that is mostly blank input box matches all along the box.
    model = _Model(
        [
            _Candidate((140, 180, 240, 96), 1.5, 0.94),
            _Candidate((261, 180, 240, 96), 1.5, 0.94),
            _Candidate((383, 180, 240, 96), 1.5, 0.94),
        ]
    )
    assert _resolve(_icon_anchor(), model) is None


def test_landmarks_that_disagree_refuse_every_look_alike():
    anchor = _icon_anchor(
        landmarks=[
            Landmark(
                relation="left_of", ocr_text="Notes", distance_px=60, dx_px=60, dy_px=0
            ),
            Landmark(
                relation="above", ocr_text="Visit", distance_px=60, dx_px=0, dy_px=60
            ),
        ]
    )
    vision = _Vision(
        {
            "Notes": _Match((100, 200), (80, 190, 40, 20)),
            # Moved: it now predicts the second look-alike, not the first.
            "Visit": _Match((330, 140), (310, 130, 40, 20)),
        }
    )
    model = _Model(
        [
            _Candidate((160, 170, 60, 60), 1.5, 0.95),
            _Candidate((300, 170, 60, 60), 1.5, 0.95),
        ]
    )
    with pytest.raises(LookAlikeTargetsRefused):
        _resolve(anchor, model, vision)


def test_one_look_alike_that_every_landmark_contradicts_refuses():
    anchor = _icon_anchor(
        landmarks=[
            Landmark(
                relation="left_of", ocr_text="Notes", distance_px=60, dx_px=60, dy_px=0
            )
        ]
    )
    vision = _Vision({"Notes": _Match((100, 200), (80, 190, 40, 20))})
    model = _Model([_Candidate((300, 20, 40, 40), 1.0, 0.95)])
    with pytest.raises(ContradictoryOcrEvidenceError):
        _resolve(anchor, model, vision)


def test_a_text_labelled_target_is_left_to_ocr():
    model = _Model([_Candidate((200, 150, 60, 60), 1.5, 0.93)])
    assert _resolve(_icon_anchor(ocr_text="Save note"), model) is None
    assert model.calls == 0


def test_ocr_noise_on_an_icon_is_not_a_label():
    # OCR reads two floppy-disk icons as "日 日"; that is not a text label.
    model = _Model([_Candidate((200, 150, 60, 60), 1.5, 0.93)])
    resolution, _region = _resolve(_icon_anchor(ocr_text="日 日"), model)
    assert resolution.rung == "grounder"


def test_a_surface_with_structure_never_reaches_the_rung():
    class _Structural:
        def locate_structural(self, locator):
            return None

    model = _Model([_Candidate((200, 150, 60, 60), 1.5, 0.93)])
    assert _resolve(_icon_anchor(), model, structural=_Structural()) is None
    assert model.calls == 0


def test_no_look_alike_falls_through_to_the_later_rungs():
    assert _resolve(_icon_anchor(), _Model([])) is None


def test_the_chain_passes_look_alikes_through_and_stays_local():
    model = _Model([_Candidate((200, 150, 60, 60), 1.5, 0.93)])
    chain = build_grounder(local_model=model)
    assert chain is model or isinstance(chain, FallbackGrounder)
    if isinstance(chain, FallbackGrounder):
        assert chain.locate_anchor(SCREEN, b"crop") == model.candidates
    assert FallbackGrounder([object()]).locate_anchor(SCREEN, b"crop") == []
    from openadapt_flow.runtime.grounder import component_may_egress

    assert not component_may_egress(chain)


# -- candidate search on drawn images --------------------------------------


def _draw_icon(draw: ImageDraw.ImageDraw, x: int, y: int, size: int, ink, fill) -> None:
    draw.rectangle(
        (x, y, x + size, y + size), outline=ink, width=max(1, size // 12), fill=fill
    )
    s = size / 24
    draw.rectangle((x + 7 * s, y + 3 * s, x + 16 * s, y + 9 * s), fill=ink)
    draw.rectangle(
        (x + 6 * s, y + 13 * s, x + 18 * s, y + 19 * s),
        outline=ink,
        width=max(1, size // 14),
    )


def _scene(scale: float, dark: bool) -> tuple[Image.Image, tuple[int, int, int, int]]:
    bg, ink, fill = (
        ((30, 34, 40), (220, 225, 230), (45, 50, 58))
        if dark
        else (
            (245, 245, 245),
            (30, 30, 30),
            (255, 255, 255),
        )
    )
    image = Image.new("RGB", (int(400 * scale), int(300 * scale)), bg)
    draw = ImageDraw.Draw(image)
    size = int(round(36 * scale))
    x, y = int(200 * scale), int(120 * scale)
    _draw_icon(draw, x, y, size, ink, fill)
    draw.ellipse(
        (int(60 * scale), int(60 * scale), int(100 * scale), int(100 * scale)),
        outline=ink,
        width=3,
    )
    return image, (x, y, size, size)


@pytest.mark.parametrize(
    "scale,dark", [(1.0, False), (1.5, False), (2.0, False), (1.25, True)]
)
def test_edge_search_finds_the_icon_at_another_scale_and_theme(scale, dark):
    recorded, (x, y, w, h) = _scene(1.0, False)
    template = recorded.crop((x - 4, y - 4, x + w + 5, y + h + 5))
    live, (lx, ly, lw, lh) = _scene(scale, dark)
    found = edge_candidates(live, template)
    # The edge search only proposes; here, with nothing else like the icon on
    # the frame, the icon is its strongest proposal.
    _score, (cx, cy, cw, ch), _scale = found[0]
    assert abs(cx + cw / 2 - (lx + lw / 2)) <= 0.25 * lw
    assert abs(cy + ch / 2 - (ly + lh / 2)) <= 0.25 * lh


@pytest.mark.parametrize(
    "scale,dark", [(1.0, False), (1.5, False), (2.0, False), (1.25, True)]
)
def test_blank_background_is_never_a_candidate(scale, dark):
    # On a flat window the edge map is float rounding noise, and normalised
    # correlation scales it up to a perfect score. Such windows would crowd
    # real look-alikes out of the proposals, differently on each platform.
    recorded, (x, y, w, h) = _scene(1.0, False)
    template = recorded.crop((x - 4, y - 4, x + w + 5, y + h + 5))
    live, _box = _scene(scale, dark)
    gray = np.asarray(live.convert("L"), dtype=np.int16)
    for _score, (cx, cy, cw, ch), _scale in edge_candidates(live, template):
        window = gray[cy : cy + ch, cx : cx + cw]
        assert window.max() - window.min() > 0, (cx, cy, cw, ch)


# -- weights ---------------------------------------------------------------


def test_missing_weights_refuse_without_download(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_FLOW_MODEL_DIR", str(tmp_path))
    with pytest.raises(WeightsUnavailable, match="not found"):
        ensure_weights(download=False)


def test_a_weights_file_with_another_digest_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_FLOW_MODEL_DIR", str(tmp_path))
    (tmp_path / WEIGHTS["filename"]).write_bytes(b"not the pinned weights")
    with pytest.raises(WeightsUnavailable, match="SHA-256"):
        ensure_weights(download=False)


def test_a_download_with_another_digest_is_refused_and_not_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_FLOW_MODEL_DIR", str(tmp_path))
    import httpx

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, size):
            yield b"tampered"

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Response())
    with pytest.raises(WeightsUnavailable, match="do not match"):
        ensure_weights(download=True)
    assert list(tmp_path.iterdir()) == []


def test_the_pin_is_a_full_sha256_of_a_fixed_revision():
    assert len(WEIGHTS["sha256"]) == 64
    assert "/resolve/8b1f705a3a7f6f062f6bdd21986c1583d3ef105d/" in WEIGHTS["url"]
    assert WEIGHTS["license"] == "Apache-2.0"


def _local_weights_present() -> bool:
    path = local_grounder.model_dir() / WEIGHTS["filename"]
    if not path.is_file():
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == WEIGHTS["sha256"]


@pytest.mark.skipif(
    not _local_weights_present(), reason="pinned weights not on this host"
)
def test_the_real_model_counts_two_identical_icons_as_look_alikes():
    model = local_grounder.LocalGroundingModel.load(download=False)
    recorded, (x, y, w, h) = _scene(1.0, False)
    template = _png(recorded.crop((x - 4, y - 4, x + w + 5, y + h + 5)))
    live = Image.new("RGB", (600, 450), (245, 245, 245))
    draw = ImageDraw.Draw(live)
    _draw_icon(draw, 120, 200, 54, (30, 30, 30), (255, 255, 255))
    _draw_icon(draw, 420, 200, 54, (30, 30, 30), (255, 255, 255))
    found = model.locate_anchor(_png(live), template)
    assert len(found) >= 2
    centres = sorted(c.point[0] for c in found[:2])
    assert abs(centres[0] - 147) < 30 and abs(centres[1] - 447) < 30
    # One icon alone is one look-alike.
    single = Image.new("RGB", (600, 450), (245, 245, 245))
    _draw_icon(ImageDraw.Draw(single), 420, 200, 54, (30, 30, 30), (255, 255, 255))
    found_one = model.locate_anchor(_png(single), template)
    assert len(found_one) == 1
    assert np.hypot(found_one[0].point[0] - 447, found_one[0].point[1] - 227) < 30


# -- OCR rung on a marked-row locator ---------------------------------------


class _OcrVision(_Vision):
    def __init__(self, match):
        super().__init__()
        self.match = match

    def find_text(self, screen_png, text, *, region=None, min_ratio=0.8, **kwargs):
        if text == "MRN 36428":
            return self.match
        return None


def test_ocr_on_a_row_key_locator_clicks_the_recorded_control_not_the_key():
    # The compiler keeps the row's key text as the locator (padded 4 px) and
    # the click on the row's edit icon, 291 px to its right.
    anchor = Anchor(
        template="templates/row.png",
        region=(231, 225, 97, 28),
        click_point=(522, 238),
        ocr_text="MRN 36428",
        search_pad=80,
    )
    # The row moved down three rows; OCR finds the key there at the same size.
    moved = _Match((279, 400), (234, 390, 89, 20))
    resolution, _region = resolve(
        anchor,
        SCREEN,
        _OcrVision(moved),
        None,
        "",
        template_png=None,
        viewport=(1280, 800),
    )
    assert resolution.rung == "ocr"
    assert resolution.point == (522, 399)
    # Drawn 1.5x larger, the offset grows with the text.
    larger = _Match((419, 600), (351, 585, 134, 30))
    resolution, _region = resolve(
        anchor,
        SCREEN,
        _OcrVision(larger),
        None,
        "",
        template_png=None,
        viewport=(1920, 1200),
    )
    assert abs(resolution.point[0] - (419 + 241.5 * 1.5)) <= 4


def test_ocr_on_the_controls_own_label_still_clicks_the_label():
    anchor = Anchor(
        template="templates/b.png",
        region=(100, 100, 60, 24),
        click_point=(130, 112),
        ocr_text="Save note",
        search_pad=20,
    )

    class _V(_Vision):
        def find_text(self, screen_png, text, **kwargs):
            return (
                _Match((300, 200), (270, 190, 60, 20)) if text == "Save note" else None
            )

    resolution, _region = resolve(
        anchor, SCREEN, _V(), None, "", template_png=None, viewport=VIEWPORT
    )
    assert resolution.point == (300, 200)


# -- deployment wiring -------------------------------------------------------


def _build(monkeypatch, runtime_config):
    import openadapt_flow.runtime as rt
    from openadapt_flow.deployment import build_replayer

    captured: dict = {}

    class _FakeReplayer:
        def __init__(self, backend, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(rt, "Replayer", _FakeReplayer)
    monkeypatch.delenv("OPENADAPT_FLOW_VLM_URL", raising=False)
    build_replayer(
        object(),
        allow_egress=False,
        effect_verifier=None,
        api_actuator=None,
        durable=False,
        use_structural=False,
        runtime_config=runtime_config,
    )
    return captured


def test_local_grounding_is_off_by_default_and_loads_nothing(monkeypatch):
    from openadapt_flow.deployment import RuntimeSection

    def _refuse(**kwargs):
        raise AssertionError("the model must not load when local grounding is off")

    monkeypatch.setattr(local_grounder.LocalGroundingModel, "load", _refuse)
    assert RuntimeSection().local_grounding is False
    captured = _build(monkeypatch, RuntimeSection())
    assert getattr(captured.get("grounder"), "locate_anchor", None) is None or (
        captured["grounder"].locate_anchor(SCREEN, b"crop") == []
    )


def test_local_grounding_on_wires_the_model_with_egress_off(monkeypatch):
    from openadapt_flow.deployment import RuntimeSection
    from openadapt_flow.runtime.grounder import component_may_egress

    model = _Model([_Candidate((200, 150, 60, 60), 1.5, 0.93)])
    monkeypatch.setattr(
        local_grounder.LocalGroundingModel, "load", classmethod(lambda cls, **k: model)
    )
    captured = _build(monkeypatch, RuntimeSection(local_grounding=True))
    grounder = captured["grounder"]
    assert grounder.locate_anchor(SCREEN, b"crop") == model.candidates
    assert not component_may_egress(grounder)
    assert captured["allow_model_grounding"] is False


# -- run accounting --------------------------------------------------------


@pytest.mark.parametrize("local", [True, False])
def test_a_local_model_step_is_not_counted_as_a_model_call(local):
    from openadapt_flow.ir import Resolution, RunReport, StepResult
    from openadapt_flow.runtime.replayer import Replayer

    report = RunReport(workflow_name="w", started_at="2026-10-28T00:00:00Z", ok=True)
    result = StepResult(
        step_id="s",
        intent="click",
        ok=True,
        resolution=Resolution(
            rung="grounder",
            point=(1, 2),
            confidence=0.9,
            elapsed_ms=1.0,
            grounder_model="local" if local else None,
        ),
    )
    Replayer._account_result(report, result)
    assert report.rung_counts == {"grounder": 1}
    assert (report.model_calls, report.local_model_calls) == (
        (0, 1) if local else (1, 0)
    )
    dumped = report.model_dump()
    assert ("local_model_calls" in dumped) is local
    assert ("grounder_model" in result.resolution.model_dump()) is local
