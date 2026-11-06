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

import pytest
from PIL import Image

from openadapt_flow import sealed_set
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
    assert "identifier_region" not in json.loads(
        sealed_set.read_text(out / "meta.json")
    )


def test_record_desktop_demo_stamps_operator_marked_identifier_region(
    tmp_path: Path,
) -> None:
    backend = FakeStructuralBackend()
    out = record_desktop_demo(
        backend,
        tmp_path / "recording",
        lambda rec: rec.click(20, 30),
        identifier_region=(10, 11, 40, 20),
        settle_interval_s=0.01,
        settle_stable_frames=1,
        settle_timeout_s=0.5,
    )

    assert json.loads(sealed_set.read_text(out / "meta.json"))["identifier_region"] == [
        10,
        11,
        40,
        20,
    ]


@pytest.mark.parametrize(
    "region",
    [
        [10, 11, 40, 20],
        (10, 11, 40, True),
        (-1, 11, 40, 20),
        (10, 11, 0, 20),
        (190, 11, 20, 20),
        (10, 111, 20, 20),
    ],
)
def test_invalid_identifier_region_refuses_before_recording(
    tmp_path: Path, region: object
) -> None:
    backend = FakeStructuralBackend()
    out = tmp_path / "recording"
    driver_called = False

    def driver(_rec) -> None:
        nonlocal driver_called
        driver_called = True

    with pytest.raises(ValueError, match="identifier_region"):
        record_desktop_demo(
            backend,
            out,
            driver,
            identifier_region=region,  # type: ignore[arg-type]
        )

    assert not driver_called
    assert not out.exists()


def test_identifier_region_stamp_uses_enclosing_sealed_writer(tmp_path: Path) -> None:
    backend = FakeStructuralBackend()
    out = tmp_path / "recording"
    passphrase = "synthetic desktop recorder test key"

    with sealed_set.writing(out, "recording", passphrase):
        record_desktop_demo(
            backend,
            out,
            lambda rec: rec.click(20, 30),
            identifier_region=(10, 11, 40, 20),
            settle_interval_s=0.01,
            settle_stable_frames=1,
            settle_timeout_s=0.5,
        )

    assert not (out / "meta.json").exists()
    opened = sealed_set.SealedSet.open(out, passphrase, expect="recording")
    assert json.loads(opened.read_bytes("meta.json"))["identifier_region"] == [
        10,
        11,
        40,
        20,
    ]
    opened.verify_all()


def test_marked_recording_failure_preserves_enclosing_sealed_writer(
    tmp_path: Path,
) -> None:
    backend = FakeStructuralBackend()
    out = tmp_path / "recording"
    passphrase = "synthetic desktop recorder failure key"

    def fail_after_recording_action(rec) -> None:
        rec.click(20, 30)
        raise RuntimeError("synthetic driver failure")

    with sealed_set.writing(out, "recording", passphrase):
        with pytest.raises(RuntimeError, match="synthetic driver failure"):
            record_desktop_demo(
                backend,
                out,
                fail_after_recording_action,
                identifier_region=(10, 11, 40, 20),
                settle_interval_s=0.01,
                settle_stable_frames=1,
                settle_timeout_s=0.5,
            )
        # The failed Recorder must unregister only a writer it owns. The outer
        # context still owns this set and can seal subsequent writes.
        sealed_set.write_text(out / "meta.json", "{}")

    opened = sealed_set.SealedSet.open(out, passphrase, expect="recording")
    assert opened.read_bytes("meta.json") == b"{}"
    opened.verify_all()


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
