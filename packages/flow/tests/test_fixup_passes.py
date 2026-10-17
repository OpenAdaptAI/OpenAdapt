"""The four default compiler fix-up passes, on web and pixel surfaces."""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image, ImageDraw

from openadapt_flow.compiler.fixup_passes import (
    COMPILE_NOTES_FILE,
    apply_fixup_passes,
    region_unchanged,
    write_compile_notes,
)
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    PostconditionKind,
    Step,
    StructuralLocator,
)


def _png(highlight: tuple[int, int, int, int] | None = None) -> bytes:
    image = Image.new("RGB", (200, 100), "white")
    if highlight is not None:
        ImageDraw.Draw(image).rectangle(highlight, fill=(30, 90, 200))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _anchor(role: str | None = None, region=(10, 10, 80, 20)) -> Anchor:
    return Anchor(
        template="templates/x.png",
        region=region,
        click_point=(region[0] + 5, region[1] + 5),
        structural=StructuralLocator(selector=f"#{role}", role=role) if role else None,
    )


def _click(sid: str, role: str | None = None, **kw) -> Step:
    return Step(id=sid, intent=sid, action=ActionKind.CLICK, anchor=_anchor(role), **kw)


def _type(sid: str, text: str = "Mr.") -> Step:
    return Step(id=sid, intent=sid, action=ActionKind.TYPE, text=text)


def _key(sid: str, key: str) -> Step:
    return Step(id=sid, intent=sid, action=ActionKind.KEY, key=key)


def _run(steps, frames, tmp_path: Path, surface=None, **kw):
    return apply_fixup_passes(
        steps,
        frames,
        tmp_path,
        crop_png=lambda png, region: png,
        phash_png=lambda png, region=None: "0" * 16,
        surface=surface,
        **kw,
    )


def _changes(notes):
    return [(n["change"], n["step"]) for n in notes]


def test_select_commit_enter_dropped_on_web_only(tmp_path):
    steps = [_click("s1", "combobox"), _type("s2"), _key("s3", "Enter")]
    out, notes = _run(steps, {}, tmp_path)
    assert [s.id for s in out] == ["s1", "s2"]
    assert ("drop_select_commit_enter", "s3") in _changes(notes)

    out, notes = _run(steps, {}, tmp_path, surface="rdp")
    assert [s.id for s in out] == ["s1", "s2", "s3"]
    assert "drop_select_commit_enter" not in {c for c, _ in _changes(notes)}


def test_select_all_on_empty_field_needs_unchanged_pixels(tmp_path):
    steps = [_click("s1", "textbox"), _key("s2", "ctrl+a"), _type("s3")]
    blank = _png()
    out, notes = _run(steps, {"s2": (blank, blank), "s1": (blank, blank)}, tmp_path)
    assert [s.id for s in out] == ["s1", "s3"]
    assert ("drop_select_all_on_empty_field", "s2") in _changes(notes)

    # The field had text: Ctrl+A highlighted it, so it is kept.
    selected = _png((12, 12, 85, 28))
    out, _ = _run(steps, {"s2": (blank, selected)}, tmp_path)
    assert [s.id for s in out] == ["s1", "s2", "s3"]

    # No frames, no evidence: kept.
    out, _ = _run(steps, {}, tmp_path)
    assert [s.id for s in out] == ["s1", "s2", "s3"]


def test_region_unchanged_tolerates_a_caret():
    before = _png()
    caret = _png((40, 12, 40, 26))
    assert region_unchanged(before, caret, (10, 5, 180, 30))
    assert not region_unchanged(before, _png((10, 10, 90, 30)), (10, 10, 80, 20))
    assert not region_unchanged(None, before, (10, 10, 80, 20))


def test_field_click_gets_region_stable(tmp_path):
    frame = _png()
    steps = [_click("s1", "textbox"), _type("s2")]
    out, notes = _run(steps, {"s1": (frame, frame)}, tmp_path)
    assert out[0].expect[0].kind is PostconditionKind.REGION_STABLE
    assert out[0].expect[0].template == "templates/s1_expect.png"
    assert (tmp_path / "templates" / "s1_expect.png").is_file()
    assert steps[0].expect == []  # input not modified
    assert ("focused_field_region_stable", "s1") in _changes(notes)


def test_pixel_surface_field_click_detected_by_following_type(tmp_path):
    frame = _png()
    steps = [_click("s1"), _type("s2"), _click("s3")]
    out, notes = _run(
        steps, {"s1": (frame, frame), "s3": (frame, frame)}, tmp_path, "rdp"
    )
    assert out[0].expect and not out[2].expect
    assert _changes(notes) == [("focused_field_region_stable", "s1")]


def test_unlabeled_button_gets_identifier_crop(tmp_path):
    frame = _png()
    steps = [_click("s1", "button")]
    out, notes = _run(
        steps, {"s1": (frame, frame)}, tmp_path, pixel_verify_enabled=True
    )
    assert out[0].anchor.identifier_crop == "templates/identifiers/s1.png"
    assert out[0].identity_armed
    assert ("static_control_identifier_crop", "s1") in _changes(notes)

    labeled = [_click("s1", "button")]
    labeled[0].anchor.context_text = "Create New Patient"
    out, notes = _run(
        labeled, {"s1": (frame, frame)}, tmp_path, pixel_verify_enabled=True
    )
    assert out[0].anchor.identifier_crop is None and notes == []


def test_button_crop_only_where_the_runtime_can_verify_it(tmp_path):
    # With the runtime's pixel VERIFY off (the default), the pixel tier can
    # reject a crop but never accept one, so a crop-only identity check would
    # halt an irreversible step on every run. The pass leaves the step as
    # compiled instead, on every surface.
    frame = _png()
    for surface in (None, "web", "windows", "rdp", "citrix"):
        steps = [_click("s1", "button")]
        out, notes = _run(steps, {"s1": (frame, frame)}, tmp_path, surface=surface)
        assert out[0].anchor.identifier_crop is None, surface
        assert not out[0].identity_armed, surface
        assert notes == [], surface


def test_button_crop_follows_each_steps_own_surface(tmp_path):
    # A bundle whose steps run on several surfaces asks per step. The
    # capability here depends only on the runtime flag, so both steps agree;
    # the hook is what a multi-surface compile passes.
    frame = _png()
    steps = [_click("s1", "button"), _click("s2", "button")]
    kinds = {"s1": "web", "s2": "windows"}
    seen: list[str] = []

    def kind_for(step):
        seen.append(step.id)
        return kinds[step.id]

    out, _notes = _run(
        steps,
        {"s1": (frame, frame), "s2": (frame, frame)},
        tmp_path,
        pixel_verify_enabled=True,
        surface_kind_for=kind_for,
    )
    assert seen == ["s1", "s2"]
    assert all(step.identity_armed for step in out)


def test_compile_notes_written_and_cleared(tmp_path):
    write_compile_notes(tmp_path, [{"change": "x", "step": "s1"}])
    data = json.loads((tmp_path / COMPILE_NOTES_FILE).read_text())
    assert data["changes"][0]["change"] == "x"
    write_compile_notes(tmp_path, [])
    assert not (tmp_path / COMPILE_NOTES_FILE).exists()
