"""The replayer runs the layout-drift check before each recorded click step.

On by default for remote-desktop replays, off elsewhere unless forced. A
drifted frame halts the step with the named reason before any input; a
matching frame proceeds with no advisor call.
"""

from __future__ import annotations

import pytest

from openadapt_flow.ir import Workflow
from openadapt_flow.runtime import layout_drift as drift
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.vision.ocr import OcrLine
from tests.test_replayer import FakeBackend, FakeVision, Match, click_step, make_png

VIEWPORT = (300, 200)


def _lines(height: int = 16, char_w: float = 8.0) -> list[OcrLine]:
    texts = ["Account number", "Status open", "Opened on", "Branch code"]
    return [
        OcrLine(
            text=t,
            region=(10, 10 + i * 30, int(len(t.replace(" ", "")) * char_w), height),
            confidence=0.95,
        )
        for i, t in enumerate(texts)
    ]


@pytest.fixture()
def bundle(tmp_path):
    b = tmp_path / "bundle"
    (b / "templates").mkdir(parents=True)
    (b / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    recorded = drift.fingerprint(_lines(), VIEWPORT)
    (b / drift.FINGERPRINT_ASSET).write_bytes(
        drift.dump_bundle_fingerprints({"s1": recorded})
    )
    return b


def _run(bundle, tmp_path, *, lines, frame_size=VIEWPORT, kind="rdp", **kw):
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    vision.ocr_lines = lines
    backend = FakeBackend(frame=make_png(frame_size), viewport=frame_size)
    replayer = Replayer(backend, vision=vision, poll_interval_s=0.01, **kw)
    report = replayer.run(
        Workflow(name="wf", steps=[click_step()]),
        bundle_dir=bundle,
        run_dir=tmp_path / "run",
        execution_target_kind=kind,
    )
    return report, backend


def test_matching_frame_proceeds_on_remote_desktop(bundle, tmp_path):
    calls: list = []
    report, backend = _run(
        bundle,
        tmp_path,
        lines=_lines(),
        layout_drift_advisor=lambda v: calls.append(v) or "x",
    )
    assert report.success, report.results[0].error
    assert backend.actions == [("click", 110, 105, False)]
    assert calls == []  # never called on a match
    assert report.model_calls == 0


def test_font_size_drift_halts_before_input_with_named_reason(bundle, tmp_path):
    report, backend = _run(bundle, tmp_path, lines=_lines(height=22, char_w=11.0))
    assert not report.success
    assert backend.actions == []
    result = report.results[0]
    assert result.safety_halt is True
    assert "Layout drift before step 's1'" in result.error
    assert "font_size_changed" in result.error


def test_font_change_and_resolution_change_are_named(bundle, tmp_path):
    report, _ = _run(bundle, tmp_path, lines=_lines(char_w=11.0))
    assert "font_changed" in report.results[0].error
    report, _ = _run(bundle, tmp_path, lines=_lines(), frame_size=(400, 300))
    assert "resolution_changed" in report.results[0].error


def test_advisor_annotates_the_halt_but_cannot_clear_it(bundle, tmp_path):
    report, backend = _run(
        bundle,
        tmp_path,
        lines=_lines(height=22, char_w=11.0),
        layout_drift_advisor=lambda v: "display scaling looks like 140%",
    )
    assert backend.actions == []
    assert "Advice: display scaling looks like 140%" in report.results[0].error


def test_off_by_default_outside_remote_desktop(bundle, tmp_path):
    report, backend = _run(
        bundle, tmp_path, lines=_lines(height=22, char_w=11.0), kind="web"
    )
    assert report.success
    assert backend.actions == [("click", 110, 105, False)]


def test_can_be_forced_on_or_off(bundle, tmp_path):
    report, _ = _run(
        bundle,
        tmp_path,
        lines=_lines(height=22, char_w=11.0),
        kind="web",
        layout_drift=True,
    )
    assert "font_size_changed" in report.results[0].error
    report, _ = _run(
        bundle, tmp_path, lines=_lines(height=22, char_w=11.0), layout_drift=False
    )
    assert report.success


def test_bundle_without_fingerprints_is_not_checked(tmp_path):
    b = tmp_path / "bundle"
    (b / "templates").mkdir(parents=True)
    (b / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    report, _ = _run(b, tmp_path, lines=_lines(height=22, char_w=11.0))
    assert report.success


def test_fingerprint_round_trip():
    fp = drift.fingerprint(_lines(), VIEWPORT, profile_sha256="ab" * 32)
    loaded = drift.load_bundle_fingerprints(drift.dump_bundle_fingerprints({"s": fp}))
    assert loaded["s"] == fp
    assert drift.load_bundle_fingerprints(b"not json") == {}
    assert drift.load_bundle_fingerprints(None) == {}
