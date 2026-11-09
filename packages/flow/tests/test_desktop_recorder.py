"""Tests for the desktop recording path + parity metric (no live VM).

A fake structural backend stands in for WindowsBackend: it screenshots a stable
frame and returns a UIA-style locator per click point. Proves that recording
LIVE over a structural backend arms every click with a structural locator (the
web-parity property capture-convert cannot provide), and pins the
``structural_armed_coverage`` metric.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image

from openadapt_flow.adapters.desktop_recorder import (
    record_desktop_demo,
    structural_armed_coverage,
)
from openadapt_flow.ir import StructuralLocator

VIEWPORT = (200, 120)


def _png(color: int = 230) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", VIEWPORT, (color, color, color)).save(buf, format="PNG")
    return buf.getvalue()


class FakeStructuralBackend:
    """Minimal Backend + StructuralActionBackend for record-time arming."""

    def __init__(self) -> None:
        self._frame = _png()
        self.clicks: list[tuple[int, int]] = []

    @property
    def viewport(self) -> tuple[int, int]:
        return VIEWPORT

    def screenshot(self) -> bytes:
        return self._frame

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self.clicks.append((x, y))

    def type_text(self, text: str) -> None:  # pragma: no cover - not exercised
        pass

    def press(self, key: str) -> None:  # pragma: no cover - not exercised
        pass

    def scroll(self, dx: int, dy: int) -> None:  # pragma: no cover - not exercised
        pass

    # -- structural capability --
    def structural_locator_at(self, x: int, y: int) -> StructuralLocator:
        return StructuralLocator(automation_id=f"btn_{x}_{y}", role="button", name="OK")


def test_record_desktop_demo_arms_structural_per_click(tmp_path: Path) -> None:
    backend = FakeStructuralBackend()

    def driver(rec) -> None:
        rec.click(20, 30)
        rec.click(60, 90)

    out = record_desktop_demo(
        backend,
        tmp_path / "recording",
        driver,
        settle_interval_s=0.01,
        settle_stable_frames=1,
        settle_timeout_s=0.5,
    )

    assert backend.clicks == [(20, 30), (60, 90)]
    lines = [
        json.loads(x)
        for x in (out / "events.jsonl").read_text().splitlines()
        if x.strip()
    ]
    clicks = [e for e in lines if e["kind"] == "click"]
    assert len(clicks) == 2
    # Every click carries the UIA locator captured at record time (parity with
    # a DOM-armed web bundle).
    assert clicks[0]["structural"]["automation_id"] == "btn_20_30"
    assert clicks[1]["structural"]["automation_id"] == "btn_60_90"
    for c in clicks:
        assert c["structural"]["role"] == "button"


# -- structural_armed_coverage metric ----------------------------------------


def _wf(steps):
    """Build a throwaway Workflow-like object exposing ``.steps``."""

    class _WF:
        pass

    wf = _WF()
    wf.steps = steps
    return wf


def _step(action_value: str, *, structural: bool):
    from openadapt_flow.ir import ActionKind, Anchor

    class _Step:
        pass

    s = _Step()
    s.action = ActionKind(action_value)
    if action_value in ("click", "double_click"):
        s.anchor = Anchor(
            template="frames/x.png",
            region=(0, 0, 10, 10),
            click_point=(5, 5),
            structural=StructuralLocator(automation_id="a") if structural else None,
        )
    else:
        s.anchor = None
    return s


def test_structural_armed_coverage_full():
    wf = _wf([_step("click", structural=True), _step("double_click", structural=True)])
    cov = structural_armed_coverage(wf)
    assert cov == {"click_steps": 2, "armed_clicks": 2, "armed_coverage": 1.0}


def test_structural_armed_coverage_partial_ignores_non_clicks():
    wf = _wf(
        [
            _step("click", structural=True),
            _step("click", structural=False),
            _step("key", structural=False),  # not a click: excluded
        ]
    )
    cov = structural_armed_coverage(wf)
    assert cov == {"click_steps": 2, "armed_clicks": 1, "armed_coverage": 0.5}


def test_structural_armed_coverage_no_clicks():
    wf = _wf([_step("key", structural=False)])
    assert structural_armed_coverage(wf) == {
        "click_steps": 0,
        "armed_clicks": 0,
        "armed_coverage": 0.0,
    }
