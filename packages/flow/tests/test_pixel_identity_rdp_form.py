"""The identifier crop is re-cut where the target moved, whatever the rung."""

from types import SimpleNamespace

import openadapt_flow.runtime.replayer as replayer_mod
from openadapt_flow.runtime.replayer import Replayer, Resolution


def _replayer(vision, monkeypatch):
    monkeypatch.setattr(
        replayer_mod.identity_mod, "crop_region", lambda png, region: b"live"
    )
    r = Replayer.__new__(Replayer)
    r.vision = vision
    r._asset_bytes = lambda bundle_dir, rel, workflow=None: b"template"
    return r


ANCHOR = SimpleNamespace(
    click_point=(462, 317),
    region=(382, 285, 160, 64),
    ocr_text="FirstName",
    template="templates/step_003.png",
)


def test_ocr_rung_measures_from_the_label_not_the_click(tmp_path, monkeypatch):
    # The label sits at (44, 33) in the recorded template and in the live crop:
    # the screen did not move, even though the OCR rung resolved 35 px left of
    # the recorded click.
    vision = SimpleNamespace(
        find_text=lambda png, text, **k: SimpleNamespace(point=(44, 33))
    )
    res = Resolution(rung="ocr", point=(427, 319), confidence=0.95, elapsed_ms=0)
    r = _replayer(vision, monkeypatch)
    assert r._identifier_displacement(ANCHOR, res, b"frame", tmp_path) == (1, 1)


def test_ocr_rung_cancels_centroid_jitter(tmp_path, monkeypatch):
    points = {b"template": (44, 33), b"live": (45, 34)}
    vision = SimpleNamespace(
        find_text=lambda png, text, **k: SimpleNamespace(point=points[png])
    )
    res = Resolution(rung="ocr", point=(425, 317), confidence=0.95, elapsed_ms=0)
    r = _replayer(vision, monkeypatch)
    assert r._identifier_displacement(ANCHOR, res, b"frame", tmp_path) == (0, 0)


def test_template_rung_keeps_click_displacement(tmp_path, monkeypatch):
    vision = SimpleNamespace(find_text=lambda *a, **k: 1 / 0)
    res = Resolution(rung="template", point=(470, 320), confidence=0.99, elapsed_ms=0)
    r = _replayer(vision, monkeypatch)
    assert r._identifier_displacement(ANCHOR, res, b"frame", tmp_path) == (8, 3)


def test_ocr_label_not_found_falls_back(tmp_path, monkeypatch):
    vision = SimpleNamespace(find_text=lambda *a, **k: None)
    res = Resolution(rung="ocr", point=(427, 319), confidence=0.95, elapsed_ms=0)
    r = _replayer(vision, monkeypatch)
    assert r._identifier_displacement(ANCHOR, res, b"frame", tmp_path) == (-35, 2)


def test_caret_glued_to_a_param_value_is_dropped():
    from openadapt_flow.runtime.identity import strip_param_caret

    ex = {"fname": "Nadia"}
    assert strip_param_caret("Name: Nadial Middle Ne", ex) == "Name: Nadia Middle Ne"
    assert strip_param_caret("Name: Nadia| Middle", ex) == "Name: Nadia Middle"
    # Two extra glyphs, or a caret glyph inside a longer word, are not a caret.
    assert strip_param_caret("Nadiall", ex) == "Nadiall"
    assert strip_param_caret("xNadial", ex) == "xNadial"


def test_template_reanchors_param_token_read_with_caret():
    from openadapt_flow.runtime.identity_template import (
        build_identity_template,
        verify_template_identity,
    )

    ex = {"fname": "Nadia"}
    tmpl = build_identity_template(
        "Name: Nadial Middle Ne Last Name Name Su", param_examples=ex
    )
    assert tmpl.param_token_indices == {"fname": [1]}
    live = "Name: Marcus Middle Ne Last Name Name Su"
    check = verify_template_identity(
        tmpl, live, params={"fname": "Marcus"}, param_examples=ex
    )
    assert check.status == "verified"
    wrong = verify_template_identity(
        tmpl, live, params={"fname": "Grace"}, param_examples=ex
    )
    assert wrong.status == "mismatch"


def test_click_template_is_clipped_away_from_a_typed_value():
    from openadapt_flow.compiler.compile import _clip_away_param_lines
    from openadapt_flow.vision.ocr import OcrLine

    street = OcrLine(
        text="27 WILLOW BEND ROAD", region=(370, 537, 160, 20), confidence=0.9
    )
    label = OcrLine(text="City:", region=(111, 570, 40, 18), confidence=0.9)
    region = (382, 548, 160, 64)
    clipped = _clip_away_param_lines(
        region, (462, 580), [street, label], ("27 WILLOW BEND ROAD",)
    )
    assert clipped == (382, 559, 160, 53)
    # No parameter text in the crop: unchanged.
    assert (
        _clip_away_param_lines(region, (462, 580), [label], ("27 WILLOW BEND ROAD",))
        == region
    )


def test_live_caret_after_run_value_is_not_a_different_name():
    from openadapt_flow.runtime.identity import verify_target_identity
    from openadapt_flow.runtime.identity_template import (
        build_identity_template,
        verify_template_identity,
    )

    ex = {"fname": "Nadia"}
    band = "Name: Nadial Middle Ne Last Name Name Su"
    live = "Name: Gracel Middle Ne Last Name Name Su"
    tmpl = build_identity_template(band, param_examples=ex, salt_hex="00" * 16)
    run = {"fname": "Grace"}
    assert (
        verify_template_identity(tmpl, live, params=run, param_examples=ex).status
        == "verified"
    )
    assert (
        verify_target_identity(band, live, params=run, param_examples=ex).status
        == "verified"
    )
    # A different typed name is still a mismatch.
    other = "Name: Gracie Middle Ne Last Name Name Su"
    assert (
        verify_template_identity(tmpl, other, params=run, param_examples=ex).status
        != "verified"
    )
