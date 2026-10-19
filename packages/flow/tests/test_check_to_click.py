"""Check-to-click guard on the pixel path (``Replayer._pixel_recheck``).

A pixel step is resolved and its identity checked on one frame, and clicked a
moment later. If the screen changes in between (a dialog opens over the
target, the list re-sorts), the click must not land on whatever is there now.
Right before the click the runtime compares the target and identity regions on
a fresh frame with the checked frame. On a change it re-resolves and re-checks
on the fresh frame, and clicks only when that holds; otherwise it refuses.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw

import openadapt_flow.vision as vision
from openadapt_flow.ir import Anchor, Resolution, Step, StepResult, Workflow
from openadapt_flow.runtime.replayer import Replayer

SIZE = (640, 400)
BUTTON = (100, 120, 140, 44)  # x, y, w, h of the recorded target


def _frame(
    *,
    button_y: int = BUTTON[1],
    dialog: bool = False,
    clock: str = "10:00",
    noise: int = 0,
) -> bytes:
    img = Image.new("RGB", SIZE, (244, 246, 248))
    d = ImageDraw.Draw(img)
    x, _, w, h = BUTTON
    d.rectangle((x, button_y, x + w, button_y + h), fill=(45, 111, 216))
    d.text((x + 22, button_y + 15), "Open chart", fill="white")
    d.rectangle((x, button_y + 70, x + w, button_y + 70 + h), fill=(180, 60, 60))
    d.text((x + 22, button_y + 85), "Delete", fill="white")
    d.text((560, 380), clock, fill="black")  # a clock far from the target
    if dialog:
        d.rectangle((60, 90, 420, 260), fill="white", outline="black")
        d.text((80, 110), "Session expires soon. Continue?", fill="black")
        d.rectangle((150, 125, 260, 165), fill=(220, 220, 220), outline="black")
        d.text((175, 138), "Continue", fill="black")
    if noise:
        px = img.load()
        for i in range(0, SIZE[0], 7):
            r, g, b = px[i, 5]
            px[i, 5] = (r - noise, g, b)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


class _Backend:
    """Serves a scripted frame sequence; records clicks."""

    viewport = SIZE

    def __init__(self, frames: list[bytes]):
        self.frames = list(frames)
        self.shots = 0
        self.clicks: list[tuple[int, int]] = []

    def screenshot(self) -> bytes:
        frame = self.frames[min(self.shots, len(self.frames) - 1)]
        self.shots += 1
        return frame

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self.clicks.append((x, y))

    def type_text(self, text: str) -> None:  # pragma: no cover - unused
        pass

    def press(self, key: str) -> None:  # pragma: no cover - unused
        pass

    def scroll(self, dx: int, dy: int) -> None:  # pragma: no cover - unused
        pass


def _bundle(tmp_path: Path) -> tuple[Path, Step, Workflow]:
    checked = Image.open(io.BytesIO(_frame()))
    x, y, w, h = BUTTON
    (tmp_path / "templates").mkdir()
    checked.crop((x, y, x + w + 1, y + h + 1)).save(tmp_path / "templates" / "t.png")
    step = Step(
        id="step_000",
        intent="click 'Open chart'",
        action="click",
        anchor=Anchor(
            template="templates/t.png",
            region=(x, y, w + 1, h + 1),
            click_point=(x + w // 2, y + h // 2),
        ),
    )
    return tmp_path, step, Workflow(name="c2c", params={}, steps=[step])


def _click(tmp_path: Path, frames: list[bytes], rung: str = "template"):
    bundle, step, wf = _bundle(tmp_path)
    backend = _Backend(frames)
    replayer = Replayer(backend, vision=vision)
    x, y, w, h = BUTTON
    res = Resolution(
        rung=rung, point=(x + w // 2, y + h // 2), confidence=0.99, elapsed_ms=1.0
    )
    replayer._active_delivery_region = (x, y, w + 1, h + 1)
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)
    error = replayer._act(
        step,
        res,
        {},
        workflow=wf,
        step_index=0,
        bundle_dir=bundle,
        before_png=_frame(),
        result=result,
        run_dir=tmp_path / "run",
    )
    return error, result, backend


def test_unchanged_screen_clicks_once(tmp_path):
    error, result, backend = _click(tmp_path, [_frame()])
    assert error is None
    assert backend.clicks == [(170, 142)]
    assert result.check_to_click == []
    assert "check_to_click" not in result.model_dump()


def test_change_away_from_the_target_does_not_stop_the_click(tmp_path):
    error, result, backend = _click(tmp_path, [_frame(clock="10:01")])
    assert error is None and len(backend.clicks) == 1
    assert result.check_to_click == []


def test_encoder_noise_is_not_a_change(tmp_path):
    error, _, backend = _click(tmp_path, [_frame(noise=6)])
    assert error is None and len(backend.clicks) == 1


def test_dialog_between_check_and_click_aborts_the_click(tmp_path):
    error, result, backend = _click(tmp_path, [_frame(dialog=True)])
    assert backend.clicks == []  # nothing was delivered under the dialog
    assert error is not None and "click not delivered" in error
    assert result.safety_halt is True
    assert [e.outcome for e in result.check_to_click] == ["aborted"]
    assert "target" in result.check_to_click[0].changed


def test_moved_target_is_reobserved_and_clicked_where_it_is_now(tmp_path):
    moved = _frame(button_y=BUTTON[1] + 30)
    error, result, backend = _click(tmp_path, [moved, moved])
    assert error is None
    assert backend.clicks == [(170, 172)]
    assert [e.outcome for e in result.check_to_click] == ["reobserved"]


def test_a_screen_that_keeps_changing_is_refused(tmp_path):
    frames = [_frame(button_y=BUTTON[1] + d) for d in (10, 20, 30, 40)]
    error, result, backend = _click(tmp_path, frames)
    assert backend.clicks == []
    assert error is not None and "kept changing" in error
    assert [e.outcome for e in result.check_to_click] == [
        "reobserved",
        "reobserved",
        "aborted",
    ]


def test_structural_targets_are_not_rechecked(tmp_path):
    backend = _Backend([_frame(dialog=True)])
    replayer = Replayer(backend, vision=vision)
    bundle, step, wf = _bundle(tmp_path)
    res = Resolution(rung="structural", point=(170, 142), confidence=1.0, elapsed_ms=1)
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)
    got = replayer._pixel_recheck(
        step, res, _frame(), {}, wf, bundle, result, tmp_path / "run"
    )
    assert got is res and backend.shots == 0


def test_identity_region_change_is_detected():
    replayer = Replayer(_Backend([_frame()]), vision=vision)
    step = Step(
        id="s",
        intent="open",
        action="click",
        anchor=Anchor(
            template="t.png",
            region=(100, 120, 141, 45),
            click_point=(170, 142),
            context_text="Open chart row identity text",
        ),
    )
    res = Resolution(rung="template", point=(170, 142), confidence=1.0, elapsed_ms=1)
    regions = replayer._check_regions(step, res, None)
    assert set(regions) == {"target", "identity"}
    assert regions["identity"][2] == SIZE[0]  # the whole row
    changed = Replayer._changed_regions(
        _frame(), _frame(button_y=BUTTON[1] + 30), regions
    )
    assert "target" in changed and "identity" in changed


def test_a_hover_look_is_the_baseline_for_the_click(tmp_path):
    # The hover look moved the pointer and the target now shows its hover
    # state; the click is checked against that frame, not the earlier one.
    hovered = _frame(clock="10:00", noise=0)
    img = Image.open(io.BytesIO(hovered)).convert("RGB")
    ImageDraw.Draw(img).rectangle((100, 120, 240, 164), outline="yellow", width=3)
    out = io.BytesIO()
    img.save(out, format="PNG")
    hovered = out.getvalue()
    bundle, step, wf = _bundle(tmp_path)
    backend = _Backend([hovered])
    replayer = Replayer(backend, vision=vision)
    replayer.__dict__["_hover_frames"] = {step.id: hovered}
    res = Resolution(rung="template", point=(170, 142), confidence=1.0, elapsed_ms=1)
    replayer._active_delivery_region = (100, 120, 141, 45)
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)
    got = replayer._pixel_recheck(
        step, res, _frame(), {}, wf, bundle, result, tmp_path / "run"
    )
    assert got is res and result.check_to_click == []
