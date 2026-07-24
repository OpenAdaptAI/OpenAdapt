"""Contracts for the external-process video encoder boundary."""

from __future__ import annotations

import io
import json
import multiprocessing
import shutil
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image

from openadapt_capture import recorder as recorder_module
from openadapt_capture import utils, video


@pytest.fixture(autouse=True)
def _init_timestamp():
    utils.set_start_time(time.time())


def _provision(path: Path, codec: str = "libx264") -> video.FFmpegProvision:
    return video.FFmpegProvision(str(path), codec=codec, source="test")


def _stream(codec: str = "libx264") -> video.FFmpegVideoStream:
    return video.FFmpegVideoStream(
        width=100,
        height=80,
        average_rate=Fraction(24),
        pix_fmt="yuv444p",
        codec=codec,
        muxer="mp4",
    )


def _small_stream(codec: str = "mpeg4") -> video.FFmpegVideoStream:
    return video.FFmpegVideoStream(
        width=2,
        height=1,
        average_rate=Fraction(24),
        pix_fmt="yuv420p",
        codec=codec,
        muxer="mp4",
    )


class _FakeInput:
    def __init__(
        self,
        *,
        broken: bool = False,
        close_broken: bool = False,
        max_write: int | None = None,
    ) -> None:
        self.data = bytearray()
        self.broken = broken
        self.close_broken = close_broken
        self.max_write = max_write
        self.closed = False

    def write(self, payload: bytes) -> int:
        if self.broken:
            raise BrokenPipeError("encoder exited")
        written = len(payload)
        if self.max_write is not None:
            written = min(written, self.max_write)
        self.data.extend(payload[:written])
        return written

    def flush(self) -> None:
        if self.broken:
            raise BrokenPipeError("encoder exited")

    def close(self) -> None:
        self.closed = True
        if self.close_broken:
            raise OSError("stdin close failed")


class _FakeProcess:
    def __init__(
        self,
        command: list[str],
        *,
        returncode: int = 0,
        stderr: bytes = b"",
        broken: bool = False,
        close_broken: bool = False,
        max_write: int | None = None,
        timeout: bool = False,
    ) -> None:
        self.command = command
        self.pipe = _FakeInput(
            broken=broken,
            close_broken=close_broken,
            max_write=max_write,
        )
        self.stdin = self.pipe
        self.stderr_payload = stderr
        self.returncode: int | None = None
        self._final_returncode = returncode
        self._timeout = timeout
        self._killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self._timeout and not self._killed:
            raise subprocess.TimeoutExpired(self.command, timeout)
        self.returncode = -9 if self._killed else self._final_returncode
        if self.returncode == 0:
            Path(self.command[-1]).write_bytes(b"\x00\x00\x00\x08ftyp")
        return self.returncode

    def kill(self) -> None:
        self._killed = True


def _install_fake_popen(monkeypatch, process: _FakeProcess):
    def popen(command, **kwargs):
        process.command = list(command)
        stderr_file = kwargs["stderr"]
        stderr_file.write(process.stderr_payload)
        stderr_file.flush()
        return process

    monkeypatch.setattr(video.subprocess, "Popen", popen)


def _png_bytes(color: str = "black") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(output, format="PNG")
    return output.getvalue()


def _spawn_initialize_video_writer(
    output_path: str,
    provision: video.FFmpegProvision,
) -> None:
    """Spawn-safe target for exercising the writer's serialized preflight."""
    utils.set_start_time(time.time())
    container, _, _ = video.initialize_video_writer(
        output_path,
        2,
        2,
        timeout_seconds=1,
        preflight_provision=provision,
    )
    container.close()


def test_resolve_explicit_path_precedes_other_sources(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    ffprobe = tmp_path / "ffprobe"
    executable.write_bytes(b"fake")
    ffprobe.write_bytes(b"fake")
    monkeypatch.setenv("OPENADAPT_DESKTOP_FFMPEG_PATH", str(tmp_path / "missing"))

    provision = video.resolve_ffmpeg(executable)

    assert provision.executable == str(executable.resolve())
    assert provision.ffprobe == str(ffprobe.resolve())
    assert provision.source == "explicit path"


def test_desktop_manifest_declares_codec_pixel_format_and_muxer(tmp_path, monkeypatch):
    root = tmp_path / "desktop-data"
    binary = root / "bin" / "ffmpeg"
    ffprobe = root / "bin" / "ffprobe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fake")
    ffprobe.write_bytes(b"fake")
    (root / "ffmpeg.json").write_text(
        json.dumps(
            {
                "version": 1,
                "executable": "bin/ffmpeg",
                "ffprobe": "bin/ffprobe",
                "codec": "h264_videotoolbox",
                "pixel_format": "yuv420p",
                "muxer": "mp4",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENADAPT_FFMPEG_PATH", raising=False)
    monkeypatch.delenv("OPENADAPT_DESKTOP_FFMPEG_PATH", raising=False)
    monkeypatch.setattr(video, "_desktop_data_dirs", lambda: [root])

    provision = video.resolve_ffmpeg()

    assert provision.executable == str(binary.resolve())
    assert provision.ffprobe == str(ffprobe.resolve())
    assert provision.codec == "h264_videotoolbox"
    assert provision.pixel_format == "yuv420p"
    assert provision.muxer == "mp4"


def test_desktop_manifest_cannot_escape_user_data_root(tmp_path, monkeypatch):
    root = tmp_path / "desktop-data"
    root.mkdir()
    (root / "ffmpeg.json").write_text(
        json.dumps({"version": 1, "executable": "../ffmpeg", "codec": "mpeg4"}),
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENADAPT_FFMPEG_PATH", raising=False)
    monkeypatch.delenv("OPENADAPT_DESKTOP_FFMPEG_PATH", raising=False)
    monkeypatch.setattr(video, "_desktop_data_dirs", lambda: [root])

    with pytest.raises(video.FFmpegUnavailableError, match="escapes"):
        video.resolve_ffmpeg()


def test_desktop_manifest_rejects_pathlike_muxer_token(tmp_path, monkeypatch):
    root = tmp_path / "desktop-data"
    binary = root / "bin" / "ffmpeg"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fake")
    (root / "ffmpeg.json").write_text(
        json.dumps(
            {
                "version": 1,
                "executable": "bin/ffmpeg",
                "codec": "mpeg4",
                "muxer": "../../escape",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENADAPT_FFMPEG_PATH", raising=False)
    monkeypatch.delenv("OPENADAPT_DESKTOP_FFMPEG_PATH", raising=False)
    monkeypatch.setattr(video, "_desktop_data_dirs", lambda: [root])

    with pytest.raises(video.FFmpegUnavailableError, match="muxer token"):
        video.resolve_ffmpeg()


def test_require_video_encoder_probes_exact_selected_codec(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    monkeypatch.setattr(
        video,
        "available_encoders",
        lambda _provision: {"mpeg4", "h264_videotoolbox"},
    )
    probed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        video,
        "_probe_encoder",
        lambda _provision, codec, pixel_format, _muxer: probed.append((codec, pixel_format)),
    )

    provision = video.require_video_encoder(
        ffmpeg_path=executable,
        codec="mpeg4",
    )
    assert provision.codec == "mpeg4"
    assert provision.pixel_format == "yuv420p"
    assert probed == [("mpeg4", "yuv420p")]

    with pytest.raises(video.FFmpegUnavailableError, match="libx264"):
        video.require_video_encoder(
            ffmpeg_path=executable,
            codec="libx264",
        )


def test_automatic_encoder_uses_real_probe_then_mpeg4_fallback(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    monkeypatch.setattr(
        video,
        "_automatic_codec_candidates",
        lambda: ["h264_videotoolbox", "mpeg4"],
    )
    monkeypatch.setattr(
        video,
        "available_encoders",
        lambda _provision: {"h264_videotoolbox", "mpeg4"},
    )
    probes: list[tuple[str, str]] = []

    def probe(_provision, codec, pixel_format, _muxer):
        probes.append((codec, pixel_format))
        if codec == "h264_videotoolbox":
            raise video.FFmpegEncodingError("VideoToolbox failed: -12908")

    monkeypatch.setattr(video, "_probe_encoder", probe)

    provision = video.require_video_encoder(ffmpeg_path=executable)

    assert provision.codec == "mpeg4"
    assert provision.pixel_format == "yuv420p"
    assert probes == [
        ("h264_videotoolbox", "yuv420p"),
        ("mpeg4", "yuv420p"),
    ]


def test_explicit_encoder_probe_failure_does_not_silently_change_codec(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    monkeypatch.setattr(
        video,
        "available_encoders",
        lambda _provision: {"h264_videotoolbox", "mpeg4"},
    )

    def fail(*_args, **_kwargs):
        raise video.FFmpegEncodingError("hardware unavailable")

    monkeypatch.setattr(video, "_probe_encoder", fail)

    with pytest.raises(video.FFmpegUnavailableError, match="hardware unavailable"):
        video.require_video_encoder(
            ffmpeg_path=executable,
            codec="h264_videotoolbox",
        )


def test_run_checked_is_bounded_non_shell(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(video.subprocess, "run", fake_run)
    result = video._run_checked(["/tmp/ffmpeg", "-version"], timeout=7)

    assert result.stdout == b"ok"
    assert captured["command"] == ["/tmp/ffmpeg", "-version"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 7


def test_run_checked_timeout_is_fail_loud(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(video.subprocess, "run", timeout)
    with pytest.raises(video.FFmpegEncodingError, match="timed out"):
        video._run_checked(["/tmp/ffmpeg"], timeout=3)


def test_encoder_probe_uses_one_raw_frame_over_stdin(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        Path(command[-1]).write_bytes(b"probe-video")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(video, "_run_checked", fake_run)
    monkeypatch.setattr(video, "_decode_first_frame_png", lambda *_args, **_kwargs: _png_bytes())

    video._probe_encoder(_provision(executable), "mpeg4", "yuv420p", "mp4")

    command = captured["command"]
    kwargs = captured["kwargs"]
    assert isinstance(command, list)
    assert command[command.index("-f") + 1] == "rawvideo"
    assert "pipe:0" in command
    assert "concat" not in command
    assert isinstance(kwargs, dict)
    assert len(kwargs["input_bytes"]) == 64 * 64 * 3


def test_direct_stream_preserves_pts_timing_without_png_staging(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    output = tmp_path / "capture.mp4"
    stage = video.FFmpegFrameStage(
        output,
        _small_stream(),
        _provision(executable),
    )
    processes: list[_FakeProcess] = []

    def popen(command, **kwargs):
        assert kwargs["shell"] is False
        process = _FakeProcess(list(command))
        kwargs["stderr"].flush()
        processes.append(process)
        return process

    monkeypatch.setattr(video.subprocess, "Popen", popen)
    monkeypatch.setattr(video, "_decode_first_frame_png", lambda *_args, **_kwargs: _png_bytes())

    red = Image.new("RGB", (2, 1), color="red")
    blue = Image.new("RGB", (2, 1), color="blue")
    stage.stage_frame(red, 0)
    stage.stage_frame(blue, 3)
    stage.close()

    assert len(processes) == 1
    process = processes[0]
    red_bytes = red.tobytes()
    blue_bytes = blue.tobytes()
    assert bytes(process.pipe.data) == red_bytes * 3 + blue_bytes
    assert process.pipe.closed is True
    assert "-f" in process.command
    assert "rawvideo" in process.command
    assert "pipe:0" in process.command
    assert "-use_wallclock_as_timestamps" not in process.command
    assert "-fps_mode" not in process.command
    assert "concat" not in process.command
    assert "-nostdin" not in process.command
    assert "+faststart" not in process.command
    assert output.read_bytes().startswith(b"\x00\x00\x00\x08ftyp")
    timing = video._read_timing_box(output)
    assert timing is not None
    assert timing[0] == Fraction(24)
    assert timing[1] == [
        (0, pytest.approx(0.0)),
        (3, pytest.approx(3 / 24)),
    ]
    assert not list(tmp_path.glob("*.png"))
    assert not list(tmp_path.glob("*.ffconcat"))


def test_successful_direct_encode_is_verified_and_atomically_promoted(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    output = tmp_path / "capture.mp4"
    stage = video.FFmpegFrameStage(
        output,
        _small_stream(),
        _provision(executable),
    )
    process = _FakeProcess(stage._encode_command())
    _install_fake_popen(monkeypatch, process)
    monkeypatch.setattr(video, "_decode_first_frame_png", lambda *_args, **_kwargs: _png_bytes())

    stage.stage_frame(Image.new("RGB", (2, 1), "red"), 0)
    stage.close()

    assert output.read_bytes().startswith(b"\x00\x00\x00\x08ftyp")
    assert video._read_timing_box(output) == (Fraction(24), [(0, 0.0)])
    assert not stage.partial_path.exists()


def test_direct_stream_normalizes_nonzero_initial_pts(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    output = tmp_path / "capture.mp4"
    stage = video.FFmpegFrameStage(
        output,
        _small_stream(),
        _provision(executable),
    )
    process = _FakeProcess(stage._encode_command())
    _install_fake_popen(monkeypatch, process)
    monkeypatch.setattr(video, "_decode_first_frame_png", lambda *_args, **_kwargs: _png_bytes())
    red = Image.new("RGB", (2, 1), "red")
    blue = Image.new("RGB", (2, 1), "blue")

    stage.stage_frame(red, 100)
    stage.stage_frame(blue, 103)
    stage.close()

    assert bytes(process.pipe.data) == red.tobytes() * 3 + blue.tobytes()
    assert video._read_timing_box(output) == (
        Fraction(24),
        [(0, 0.0), (3, 3 / 24)],
    )


def test_failed_direct_encode_retains_partial_and_never_promotes_output(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    output = tmp_path / "capture.mp4"
    stage = video.FFmpegFrameStage(
        output,
        _small_stream(),
        _provision(executable),
    )
    process = _FakeProcess(
        stage._encode_command(),
        returncode=7,
        stderr=b"encoder failed",
    )
    stage.partial_path.write_bytes(b"incomplete")
    _install_fake_popen(monkeypatch, process)

    stage.stage_frame(Image.new("RGB", (2, 1), "red"), 0)
    with pytest.raises(video.FFmpegEncodingError, match="encoder failed"):
        stage.close()

    assert stage.partial_path.read_bytes() == b"incomplete"
    assert not output.exists()


def test_direct_encode_timeout_is_bounded_and_fail_loud(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    output = tmp_path / "capture.mp4"
    stage = video.FFmpegFrameStage(
        output,
        _small_stream(),
        _provision(executable),
        timeout_seconds=0.01,
    )
    process = _FakeProcess(stage._encode_command(), timeout=True)
    _install_fake_popen(monkeypatch, process)

    stage.stage_frame(Image.new("RGB", (2, 1), "red"), 0)
    with pytest.raises(video.FFmpegEncodingError, match="timed out"):
        stage.close()
    assert process._killed is True
    assert not output.exists()


def test_direct_encode_broken_pipe_is_fail_loud(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    stage = video.FFmpegFrameStage(
        tmp_path / "capture.mp4",
        _small_stream(),
        _provision(executable),
    )
    process = _FakeProcess(
        stage._encode_command(),
        returncode=9,
        stderr=b"codec crashed",
        broken=True,
    )
    _install_fake_popen(monkeypatch, process)

    stage.stage_frame(Image.new("RGB", (2, 1), "red"), 0)
    with pytest.raises(video.FFmpegEncodingError, match="codec crashed"):
        stage.close()
    assert process._killed is True


def test_direct_encode_retries_partial_pipe_writes(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    output = tmp_path / "capture.mp4"
    stage = video.FFmpegFrameStage(
        output,
        _small_stream(),
        _provision(executable),
    )
    process = _FakeProcess(
        stage._encode_command(),
        max_write=2,
    )
    _install_fake_popen(monkeypatch, process)
    monkeypatch.setattr(video, "_decode_first_frame_png", lambda *_args, **_kwargs: _png_bytes())
    frame = Image.new("RGB", (2, 1), "red")

    stage.stage_frame(frame, 0)
    stage.close()

    assert bytes(process.pipe.data) == frame.tobytes()
    assert video._read_timing_box(output) == (Fraction(24), [(0, 0.0)])


def test_direct_encode_zero_length_pipe_write_fails_loudly(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    stage = video.FFmpegFrameStage(
        tmp_path / "capture.mp4",
        _small_stream(),
        _provision(executable),
    )
    process = _FakeProcess(
        stage._encode_command(),
        max_write=0,
    )
    _install_fake_popen(monkeypatch, process)

    stage.stage_frame(Image.new("RGB", (2, 1), "red"), 0)
    with pytest.raises(video.FFmpegEncodingError, match="no write progress"):
        stage.close()
    assert process._killed is True


def test_direct_encode_stdin_close_failure_reaps_process(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    stage = video.FFmpegFrameStage(
        tmp_path / "capture.mp4",
        _small_stream(),
        _provision(executable),
    )
    process = _FakeProcess(
        stage._encode_command(),
        close_broken=True,
    )
    _install_fake_popen(monkeypatch, process)

    stage.stage_frame(Image.new("RGB", (2, 1), "red"), 0)
    with pytest.raises(video.FFmpegEncodingError, match="stdin close failed"):
        stage.close()
    assert process._killed is True


def test_direct_encode_bounded_stderr_tail_does_not_block(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    stage = video.FFmpegFrameStage(
        tmp_path / "capture.mp4",
        _small_stream(),
        _provision(executable),
    )
    process = _FakeProcess(
        stage._encode_command(),
        returncode=2,
        stderr=b"x" * (128 * 1024) + b" final encoder error",
    )
    _install_fake_popen(monkeypatch, process)

    stage.stage_frame(Image.new("RGB", (2, 1), "red"), 0)
    with pytest.raises(video.FFmpegEncodingError, match="final encoder error"):
        stage.close()


def test_empty_direct_stream_closes_without_starting_ffmpeg(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"fake")
    stage = video.FFmpegFrameStage(
        tmp_path / "capture.mp4",
        _small_stream(),
        _provision(executable),
    )
    monkeypatch.setattr(
        video.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("empty stream must not start FFmpeg"),
    )

    stage.close()
    assert stage._closed is True


def test_record_refuses_missing_encoder_before_display_or_listeners(tmp_path, monkeypatch):
    display_touched = False

    def fail_preflight(**_kwargs):
        raise video.FFmpegUnavailableError("no provisioned encoder")

    def touch_display():
        nonlocal display_touched
        display_touched = True
        raise AssertionError("display must not be touched")

    monkeypatch.setattr(video, "require_video_encoder", fail_preflight)
    monkeypatch.setattr(recorder_module.utils, "take_screenshot", touch_display)

    with pytest.raises(video.FFmpegUnavailableError, match="no provisioned"):
        recorder_module.record("test", capture_dir=str(tmp_path / "capture"))
    assert display_touched is False


def test_preflight_provision_survives_spawn_without_child_config(tmp_path, monkeypatch):
    """Spawned writers use the exact parent-selected provision, not fresh defaults."""
    executable = tmp_path / "managed-ffmpeg"
    executable.write_bytes(b"preflighted in parent")
    provision = video.FFmpegProvision(
        executable=str(executable),
        ffprobe=None,
        codec="mpeg4",
        pixel_format="yuv420p",
        muxer="mp4",
        source="parent preflight",
    )
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("OPENADAPT_PLATFORM_OVERRIDE", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "no-desktop-runtime"))
    monkeypatch.delenv("OPENADAPT_FFMPEG_PATH", raising=False)
    monkeypatch.delenv("OPENADAPT_DESKTOP_FFMPEG_PATH", raising=False)

    process = multiprocessing.get_context("spawn").Process(
        target=_spawn_initialize_video_writer,
        args=(str(tmp_path / "spawned.mp4"), provision),
    )
    process.start()
    process.join(timeout=15)

    assert not process.is_alive()
    assert process.exitcode == 0


def test_extract_frames_preserves_nearest_within_tolerance_and_order(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    ffprobe = tmp_path / "ffprobe"
    video_path = tmp_path / "capture.mp4"
    for path in (executable, ffprobe, video_path):
        path.write_bytes(b"fake")
    provision = video.FFmpegProvision(
        str(executable),
        ffprobe=str(ffprobe),
        source="test",
    )
    monkeypatch.setattr(video, "resolve_ffmpeg", lambda *_args: provision)
    monkeypatch.setattr(
        video,
        "_frame_catalog",
        lambda *_args: [(0, 0.0), (1, 0.4), (2, 1.0)],
    )
    monkeypatch.setattr(
        video,
        "_extract_frame_index_png",
        lambda _path, index, _provision: Image.new("RGB", (2, 2), (index, 0, 0)),
    )

    frames = video.extract_frames(
        video_path,
        [0.39, 0.01, 0.75],
        tolerance=0.26,
    )

    assert [frame.getpixel((0, 0))[0] for frame in frames] == [1, 0, 2]

    with pytest.raises(ValueError, match=r"0\.75"):
        video.extract_frames(video_path, [0.75], tolerance=0.24)


def test_extract_frames_tie_keeps_earlier_decoded_frame(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    ffprobe = tmp_path / "ffprobe"
    video_path = tmp_path / "capture.mp4"
    for path in (executable, ffprobe, video_path):
        path.write_bytes(b"fake")
    provision = video.FFmpegProvision(
        str(executable),
        ffprobe=str(ffprobe),
        source="test",
    )
    monkeypatch.setattr(video, "resolve_ffmpeg", lambda *_args: provision)
    monkeypatch.setattr(
        video,
        "_frame_catalog",
        lambda *_args: [(0, 0.0), (1, 1.0)],
    )
    selected: list[int] = []

    def extract(_path, index, _provision):
        selected.append(index)
        return Image.new("RGB", (1, 1))

    monkeypatch.setattr(video, "_extract_frame_index_png", extract)

    video.extract_frame(video_path, 0.5, tolerance=0.5)
    assert selected == [0]


def test_get_video_info_preserves_metadata_contract(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    ffprobe = tmp_path / "ffprobe"
    video_path = tmp_path / "capture.mp4"
    for path in (executable, ffprobe, video_path):
        path.write_bytes(b"fake")
    provision = video.FFmpegProvision(
        str(executable),
        ffprobe=str(ffprobe),
        source="test",
    )
    monkeypatch.setattr(video, "resolve_ffmpeg", lambda *_args: provision)
    monkeypatch.setattr(
        video,
        "_probe_json",
        lambda *_args: {
            "streams": [
                {
                    "width": 1920,
                    "height": 1080,
                    "codec_name": "mpeg4",
                    "avg_frame_rate": "24/1",
                    "duration": "2.5",
                    "nb_frames": "60",
                }
            ],
            "format": {"duration": "2.6"},
        },
    )

    assert video.get_video_info(video_path) == {
        "duration": 2.5,
        "width": 1920,
        "height": 1080,
        "fps": 24.0,
        "codec": "mpeg4",
        "frames": 60,
    }


def _real_ffmpeg_with_codec(codec: str) -> str | None:
    executable = shutil.which("ffmpeg")
    if not executable:
        return None
    try:
        provision = video.FFmpegProvision(executable, source="test PATH")
        if codec not in video.available_encoders(provision):
            return None
    except (video.FFmpegEncodingError, OSError):
        return None
    return executable


@pytest.mark.skipif(
    _real_ffmpeg_with_codec("libx264") is None,
    reason="opt-in real FFmpeg/libx264 executable is not available",
)
def test_real_external_ffmpeg_encode_verify_and_extract(tmp_path):
    executable = _real_ffmpeg_with_codec("libx264")
    assert executable is not None
    output = tmp_path / "real.mp4"
    container, stream, start = video.initialize_video_writer(
        str(output),
        100,
        80,
        ffmpeg_path=executable,
        timeout_seconds=60,
    )
    red = Image.new("RGB", (100, 80), "red")
    blue = Image.new("RGB", (100, 80), "blue")
    last_pts = video.write_video_frame(
        container,
        stream,
        red,
        start,
        start,
        -1,
    )
    last_pts = video.write_video_frame(
        container,
        stream,
        blue,
        start + 1,
        start,
        last_pts,
    )
    video.finalize_video_writer(
        container,
        stream,
        start,
        blue,
        start + 1,
        last_pts,
        str(output),
    )

    assert output.stat().st_size > 0
    frame = video.extract_frame(output, 0, ffmpeg_path=executable)
    assert frame.size == (100, 80)
    assert frame.getpixel((10, 10))[0] > frame.getpixel((10, 10))[2]


@pytest.mark.skipif(
    _real_ffmpeg_with_codec("mpeg4") is None,
    reason="opt-in real FFmpeg/mpeg4 executable is not available",
)
def test_real_external_mpeg4_preserves_metadata_and_nearest_frame(tmp_path):
    executable = _real_ffmpeg_with_codec("mpeg4")
    assert executable is not None
    output = tmp_path / "portable.mp4"
    container, stream, start = video.initialize_video_writer(
        str(output),
        100,
        80,
        codec="mpeg4",
        ffmpeg_path=executable,
        timeout_seconds=60,
    )
    assert stream.pix_fmt == "yuv420p"
    red = Image.new("RGB", (100, 80), "red")
    blue = Image.new("RGB", (100, 80), "blue")
    first_pts = video.write_video_frame(
        container,
        stream,
        red,
        start,
        start,
        -1,
    )
    # Simulate a writer queue that falls behind capture time. Encoded PTS must
    # follow the supplied capture timestamp, never this processing delay.
    time.sleep(1.05)
    last_pts = video.write_video_frame(
        container,
        stream,
        blue,
        start + 1,
        start,
        first_pts,
    )
    video.finalize_video_writer(
        container,
        stream,
        start,
        blue,
        start + 1,
        last_pts,
        str(output),
    )

    info = video.get_video_info(output, ffmpeg_path=executable)
    assert info["codec"] == "mpeg4"
    assert info["width"] == 100
    assert info["height"] == 80
    assert info["frames"] == 26
    provision = video.resolve_ffmpeg(executable)
    payload = video._probe_json(
        provision,
        [
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            str(output),
        ],
    )
    decoded_timestamps = [float(item["best_effort_timestamp_time"]) for item in payload["frames"]]
    assert decoded_timestamps == pytest.approx(
        [index / 24 for index in range(26)],
        abs=1e-6,
    )
    assert video._read_timing_box(output) == (
        Fraction(24),
        [(0, 0.0), (24, 1.0), (25, 25 / 24)],
    )
    frame = video.extract_frame(
        output,
        0.8,
        tolerance=0.25,
        ffmpeg_path=executable,
    )
    assert frame.getpixel((10, 10))[2] > frame.getpixel((10, 10))[0]

    video.move_moov_atom(output, ffmpeg_path=executable)
    assert video._read_timing_box(output) == (
        Fraction(24),
        [(0, 0.0), (24, 1.0), (25, 25 / 24)],
    )
