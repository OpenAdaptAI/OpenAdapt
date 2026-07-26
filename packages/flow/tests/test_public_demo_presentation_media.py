"""Characterization of exact presentation-media timing."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts.export_public_demo_evidence import (
    _PresentationCapture,
    _probe_video_sample_end_us,
    _write_presentation_clip,
)


def _png(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(output, format="PNG")
    return output.getvalue()


def test_sample_end_includes_the_final_packet_duration(
    monkeypatch, tmp_path: Path
) -> None:
    payload = {
        "packets": [
            {"pts_time": "0.000000", "duration_time": "0.250000"},
            {"pts_time": "0.250000", "duration_time": "1.750000"},
            {"pts_time": "2.000000", "duration_time": "0.001000"},
        ]
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(payload)),
    )

    assert _probe_video_sample_end_us("ffprobe", tmp_path / "clip.mp4") == 2_001_000


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="requires separately provisioned ffmpeg and ffprobe",
)
def test_exported_mp4_preserves_exact_pts_and_full_terminal_sample(
    tmp_path: Path,
) -> None:
    capture = _PresentationCapture(mode="replay")
    capture.emitter.begin(profile="standard")
    capture.emitter.emit_phase("observing", observation_png=_png((0, 0, 0)))
    capture.emitter.emit_terminal("HALTED", observation_png=_png((255, 255, 255)))
    capture.frames = [
        frame.model_copy(update={"observed_at_monotonic_ms": 1_000.0 + offset})
        for frame, offset in zip(capture.frames, (0, 250), strict=True)
    ]

    _write_presentation_clip(
        capture=capture,
        pack_id="exact-timing-fixture",
        clip_id="halted",
        presentation_dir=tmp_path,
    )

    pts = json.loads((tmp_path / "halted.frame-pts-us.json").read_text())
    timeline = json.loads((tmp_path / "halted.control-overlay.v2.json").read_text())
    media = tmp_path / "halted.mp4"
    assert pts["presentation_times_us"] == [0, 250_000, 2_000_000]
    assert _probe_video_sample_end_us(str(shutil.which("ffprobe")), media) == 2_001_000
    assert timeline["duration_ms"] == 2_001

    probe = subprocess.run(
        [
            str(shutil.which("ffprobe")),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=time_base:packet=pts,dts",
            "-of",
            "json",
            str(media),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    timing = json.loads(probe.stdout)
    assert timing["streams"] == [{"time_base": "1/1000000"}]
    assert all(packet["pts"] == packet["dts"] for packet in timing["packets"])
