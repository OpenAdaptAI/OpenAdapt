"""Video capture through a separately provisioned FFmpeg executable.

Capture itself never downloads or bundles FFmpeg. In-memory RGB frames stream
directly into the encoder process and become compact video while recording.
The finished output is verified and atomically promoted; an encoder failure is
reported and never leaves a false-success public MP4.
"""

from __future__ import annotations

import io
import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Sequence

from loguru import logger
from PIL import Image

from openadapt_capture import utils
from openadapt_capture.config import config

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

DEFAULT_FPS = 24
DEFAULT_CODEC = "libx264"
DEFAULT_PIXEL_FORMAT = "yuv444p"
DEFAULT_CRF = 0
DEFAULT_PRESET = "veryslow"
DEFAULT_PROCESS_TIMEOUT_SECONDS = 900.0
PROBE_TIMEOUT_SECONDS = 10.0
EXTRACT_TIMEOUT_SECONDS = 120.0
FRAME_WRITE_TIMEOUT_SECONDS = 30.0
_ENCODER_LINE = re.compile(r"^\s*V\S*\s+(?P<name>\S+)\s", re.MULTILINE)
_OPTION_TOKEN = re.compile(r"^[A-Za-z0-9_.-]+$")
_TIMING_BOX_UUID = uuid.UUID("d8e90f06-20b4-4e0c-b449-4f70656e4164").bytes
_TIMING_SCHEMA = "openadapt.capture-video-timing/v1"
_MAX_TIMING_PAYLOAD_BYTES = 16 * 1024 * 1024


class FFmpegUnavailableError(RuntimeError):
    """No separately provisioned, usable FFmpeg executable was found."""


class FFmpegEncodingError(RuntimeError):
    """FFmpeg failed to encode or verify a recording."""


@dataclass(frozen=True)
class FFmpegProvision:
    """Resolved executable and optional Desktop-declared codec."""

    executable: str
    ffprobe: str | None = None
    codec: str | None = None
    pixel_format: str | None = None
    muxer: str | None = None
    source: str = "unknown"


@dataclass
class FFmpegVideoStream:
    """Small compatibility surface used by the recorder's writer pipeline."""

    width: int
    height: int
    average_rate: Fraction
    pix_fmt: str
    codec: str
    muxer: str


def _append_timing_box(
    path: Path,
    *,
    fps: Fraction,
    frames: list[tuple[int, float]],
) -> None:
    """Append logical capture-frame timestamps in an ignored MP4 UUID box."""
    payload = json.dumps(
        {
            "schema": _TIMING_SCHEMA,
            "fps": f"{fps.numerator}/{fps.denominator}",
            "frames": [[index, timestamp] for index, timestamp in frames],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    box_size = 24 + len(payload)
    if len(payload) > _MAX_TIMING_PAYLOAD_BYTES or box_size >= 2**32:
        raise FFmpegEncodingError("Video timing metadata exceeds its bounded MP4 box")
    with path.open("ab") as output:
        output.write(struct.pack(">I4s16s", box_size, b"uuid", _TIMING_BOX_UUID))
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def _read_timing_box(
    path: Path,
) -> tuple[Fraction, list[tuple[int, float]]] | None:
    """Read OpenAdapt logical timestamps from top-level MP4 boxes, if present."""
    file_size = path.stat().st_size
    with path.open("rb") as source:
        offset = 0
        while offset + 8 <= file_size:
            source.seek(offset)
            header = source.read(8)
            size32, box_type = struct.unpack(">I4s", header)
            header_size = 8
            if size32 == 1:
                extended = source.read(8)
                if len(extended) != 8:
                    return None
                box_size = struct.unpack(">Q", extended)[0]
                header_size = 16
            elif size32 == 0:
                box_size = file_size - offset
            else:
                box_size = size32
            if box_size < header_size or offset + box_size > file_size:
                return None
            if box_type == b"uuid" and box_size >= header_size + 16:
                user_type = source.read(16)
                if user_type == _TIMING_BOX_UUID:
                    payload_size = box_size - header_size - 16
                    if payload_size > _MAX_TIMING_PAYLOAD_BYTES:
                        raise FFmpegEncodingError("Video timing metadata exceeds its read bound")
                    try:
                        payload = json.loads(source.read(payload_size).decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise FFmpegEncodingError("Video timing metadata is invalid") from exc
                    if not isinstance(payload, dict) or payload.get("schema") != _TIMING_SCHEMA:
                        raise FFmpegEncodingError("Video timing metadata has an unknown schema")
                    try:
                        fps = Fraction(payload["fps"])
                    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
                        raise FFmpegEncodingError(
                            "Video timing metadata has an invalid frame rate"
                        ) from exc
                    if fps <= 0:
                        raise FFmpegEncodingError(
                            "Video timing metadata has a non-positive frame rate"
                        )
                    result: list[tuple[int, float]] = []
                    previous_index = -1
                    previous_timestamp = -1.0
                    for entry in payload.get("frames", []):
                        if (
                            not isinstance(entry, list)
                            or len(entry) != 2
                            or not isinstance(entry[0], int)
                        ):
                            raise FFmpegEncodingError("Video timing metadata has an invalid frame")
                        try:
                            timestamp = float(entry[1])
                        except (TypeError, ValueError) as exc:
                            raise FFmpegEncodingError(
                                "Video timing metadata has an invalid timestamp"
                            ) from exc
                        if (
                            entry[0] <= previous_index
                            or not math.isfinite(timestamp)
                            or timestamp < previous_timestamp
                        ):
                            raise FFmpegEncodingError(
                                "Video timing metadata is not strictly ordered"
                            )
                        result.append((entry[0], timestamp))
                        previous_index = entry[0]
                        previous_timestamp = timestamp
                    return fps, result
            offset += box_size
    return None


def _validate_option_token(label: str, value: str) -> str:
    if not _OPTION_TOKEN.fullmatch(value):
        raise FFmpegUnavailableError(
            f"Invalid FFmpeg {label} token {value!r}; expected a codec-style name"
        )
    return value


def _desktop_data_dirs() -> list[Path]:
    """Return platform user-data roots where Desktop may provision FFmpeg."""
    home = Path.home()
    candidates: list[Path] = []
    if sys_platform := os.environ.get("OPENADAPT_PLATFORM_OVERRIDE"):
        platform_name = sys_platform
    else:
        import sys

        platform_name = sys.platform

    if platform_name == "darwin":
        candidates.append(home / "Library" / "Application Support" / "ai.openadapt.desktop")
    elif platform_name == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "ai.openadapt.desktop")
    else:
        xdg_data_home = os.environ.get("XDG_DATA_HOME")
        candidates.append(Path(xdg_data_home) if xdg_data_home else home / ".local" / "share")
        candidates[-1] = candidates[-1] / "ai.openadapt.desktop"
    return candidates


def _load_desktop_manifest(root: Path) -> FFmpegProvision | None:
    """Load a narrow Desktop-owned encoder manifest, if present.

    Schema::

        {
          "version": 1,
          "executable": "bin/ffmpeg",
          "ffprobe": "bin/ffprobe",
          "codec": "h264_videotoolbox",
          "pixel_format": "yuv420p",
          "muxer": "mp4"
        }

    The executable must remain within the manifest's user-data root. Capture
    does not create, download, or update this file.
    """
    manifest_path = root / "ffmpeg.json"
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FFmpegUnavailableError(
            f"Invalid Desktop FFmpeg manifest at {manifest_path}: {exc}"
        ) from exc
    if payload.get("version") != 1:
        raise FFmpegUnavailableError(
            f"Unsupported Desktop FFmpeg manifest version at {manifest_path}"
        )
    executable_value = payload.get("executable")
    ffprobe_value = payload.get("ffprobe")
    codec = payload.get("codec")
    pixel_format = payload.get("pixel_format")
    muxer = payload.get("muxer")
    if not isinstance(executable_value, str) or not executable_value:
        raise FFmpegUnavailableError(f"Desktop FFmpeg manifest has no executable: {manifest_path}")
    if ffprobe_value is not None and (not isinstance(ffprobe_value, str) or not ffprobe_value):
        raise FFmpegUnavailableError(
            f"Desktop FFmpeg manifest has an invalid ffprobe: {manifest_path}"
        )
    if codec is not None and (not isinstance(codec, str) or not codec):
        raise FFmpegUnavailableError(
            f"Desktop FFmpeg manifest has an invalid codec: {manifest_path}"
        )
    for key, value in (("pixel_format", pixel_format), ("muxer", muxer)):
        if value is not None and (not isinstance(value, str) or not value):
            raise FFmpegUnavailableError(
                f"Desktop FFmpeg manifest has an invalid {key}: {manifest_path}"
            )
    for key, value in (
        ("codec", codec),
        ("pixel_format", pixel_format),
        ("muxer", muxer),
    ):
        if value is not None:
            _validate_option_token(key, value)
    root_resolved = root.resolve()
    executable = (root / executable_value).resolve()
    try:
        executable.relative_to(root_resolved)
    except ValueError as exc:
        raise FFmpegUnavailableError(
            f"Desktop FFmpeg manifest executable escapes {root_resolved}"
        ) from exc
    ffprobe: Path | None = None
    if ffprobe_value is not None:
        ffprobe = (root / ffprobe_value).resolve()
        try:
            ffprobe.relative_to(root_resolved)
        except ValueError as exc:
            raise FFmpegUnavailableError(
                f"Desktop FFmpeg manifest ffprobe escapes {root_resolved}"
            ) from exc
    return FFmpegProvision(
        executable=str(executable),
        ffprobe=str(ffprobe) if ffprobe is not None else None,
        codec=codec,
        pixel_format=pixel_format,
        muxer=muxer,
        source=f"desktop manifest {manifest_path}",
    )


def _candidate_is_file(value: str | os.PathLike[str]) -> str | None:
    path = Path(value).expanduser()
    if path.is_file():
        return str(path.resolve())
    return None


def _sibling_ffprobe(ffmpeg: str) -> str | None:
    """Find an ffprobe installed beside FFmpeg without searching or downloading."""
    executable = Path(ffmpeg)
    suffix = ".exe" if executable.suffix.lower() == ".exe" else ""
    return _candidate_is_file(executable.with_name(f"ffprobe{suffix}"))


def _resolve_ffprobe(
    ffmpeg: str,
    explicit: str | os.PathLike[str] | None = None,
) -> str | None:
    requested = explicit or os.environ.get("OPENADAPT_FFPROBE_PATH")
    if requested:
        resolved = _candidate_is_file(requested)
        if resolved is None:
            raise FFmpegUnavailableError(
                f"Configured ffprobe executable does not exist: {requested}"
            )
        return resolved
    return _sibling_ffprobe(ffmpeg) or shutil.which("ffprobe")


def resolve_ffmpeg(
    ffmpeg_path: str | os.PathLike[str] | None = None,
    ffprobe_path: str | os.PathLike[str] | None = None,
) -> FFmpegProvision:
    """Resolve FFmpeg without downloading or modifying external state.

    Order:
    1. Explicit API/config path.
    2. ``OPENADAPT_FFMPEG_PATH`` / ``OPENADAPT_DESKTOP_FFMPEG_PATH``.
    3. Desktop user-data ``ffmpeg.json`` manifest.
    4. System ``PATH``.

    ``ffprobe`` is resolved from an explicit path, the Desktop manifest, beside
    FFmpeg, or from ``PATH``. Encoding does not require it; exact frame lookup
    and metadata inspection do.
    """
    explicit = ffmpeg_path or os.environ.get("OPENADAPT_FFMPEG_PATH")
    if explicit:
        resolved = _candidate_is_file(explicit)
        if resolved is None:
            raise FFmpegUnavailableError(f"Configured FFmpeg executable does not exist: {explicit}")
        return FFmpegProvision(
            resolved,
            ffprobe=_resolve_ffprobe(resolved, ffprobe_path),
            source="explicit path",
        )

    desktop_env = os.environ.get("OPENADAPT_DESKTOP_FFMPEG_PATH")
    if desktop_env:
        resolved = _candidate_is_file(desktop_env)
        if resolved is None:
            raise FFmpegUnavailableError(
                f"OPENADAPT_DESKTOP_FFMPEG_PATH does not name a file: {desktop_env}"
            )
        return FFmpegProvision(
            resolved,
            ffprobe=_resolve_ffprobe(resolved, ffprobe_path),
            source="Desktop environment",
        )

    for root in _desktop_data_dirs():
        manifest = _load_desktop_manifest(root)
        if manifest is not None:
            if not Path(manifest.executable).is_file():
                raise FFmpegUnavailableError(
                    f"Desktop-provisioned FFmpeg is missing: {manifest.executable}"
                )
            manifest_probe = manifest.ffprobe
            if manifest_probe is not None and not Path(manifest_probe).is_file():
                raise FFmpegUnavailableError(
                    f"Desktop-provisioned ffprobe is missing: {manifest_probe}"
                )
            return FFmpegProvision(
                executable=manifest.executable,
                ffprobe=(
                    _resolve_ffprobe(manifest.executable, ffprobe_path)
                    if ffprobe_path or os.environ.get("OPENADAPT_FFPROBE_PATH")
                    else manifest_probe or _resolve_ffprobe(manifest.executable)
                ),
                codec=manifest.codec,
                pixel_format=manifest.pixel_format,
                muxer=manifest.muxer,
                source=manifest.source,
            )

    path_value = shutil.which("ffmpeg")
    if path_value:
        resolved = str(Path(path_value).resolve())
        return FFmpegProvision(
            resolved,
            ffprobe=_resolve_ffprobe(resolved, ffprobe_path),
            source="PATH",
        )

    raise FFmpegUnavailableError(
        "Video capture requires a separately provisioned FFmpeg executable. "
        "Set OPENADAPT_FFMPEG_PATH, pass Recorder(ffmpeg_path=...), provision "
        "Desktop's ffmpeg.json user-data manifest, or put ffmpeg on PATH. "
        "OpenAdapt Capture does not download or bundle FFmpeg."
    )


def _run_checked(
    command: Sequence[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run an argument-vector command with bounded, non-shell execution."""
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegEncodingError(f"FFmpeg timed out after {timeout:g}s: {command[0]}") from exc
    except OSError as exc:
        raise FFmpegEncodingError(f"Could not execute FFmpeg: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise FFmpegEncodingError(
            f"FFmpeg exited with code {result.returncode}: {stderr or '(no stderr)'}"
        )
    return result


def available_encoders(
    provision: FFmpegProvision,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> set[str]:
    """Return video encoder names advertised by the resolved executable."""
    result = _run_checked(
        [provision.executable, "-hide_banner", "-encoders"],
        timeout=timeout,
    )
    output = (
        result.stdout.decode("utf-8", errors="replace")
        + "\n"
        + result.stderr.decode("utf-8", errors="replace")
    )
    return {match.group("name") for match in _ENCODER_LINE.finditer(output)}


def _codec_arguments(
    codec: str,
    *,
    crf: int = DEFAULT_CRF,
    preset: str = DEFAULT_PRESET,
) -> list[str]:
    if codec.startswith(("libx264", "libx265")):
        return ["-crf", str(crf), "-preset", preset]
    if codec.startswith("libvpx"):
        return ["-crf", str(crf), "-b:v", "0"]
    if codec == "mpeg4":
        return ["-q:v", "2"]
    return []


def _decode_first_frame_png(
    provision: FFmpegProvision,
    video_path: Path,
    *,
    timeout: float,
) -> bytes:
    """Decode one frame to PNG, proving both the video and extraction path."""
    result = _run_checked(
        [
            provision.executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-vf",
            "select=eq(n\\,0)",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        timeout=timeout,
    )
    if not result.stdout:
        raise FFmpegEncodingError("FFmpeg decoded no PNG verification frame")
    try:
        with Image.open(io.BytesIO(result.stdout)) as image:
            image.verify()
    except Exception as exc:
        raise FFmpegEncodingError("FFmpeg returned an invalid PNG verification frame") from exc
    return result.stdout


def _probe_encoder(
    provision: FFmpegProvision,
    codec: str,
    pixel_format: str,
    muxer: str,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> None:
    """Perform a real one-frame encode and decode, not a name-only probe."""
    with tempfile.TemporaryDirectory(prefix="openadapt-ffmpeg-probe-") as temp:
        root = Path(temp)
        output = root / f"probe.{muxer}"
        frame = Image.new("RGB", (64, 64), "black").tobytes()
        command = [
            provision.executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pixel_format",
            "rgb24",
            "-video_size",
            "64x64",
            "-framerate",
            "1",
            "-i",
            "pipe:0",
            "-frames:v",
            "1",
            "-an",
            "-c:v",
            codec,
            "-pix_fmt",
            pixel_format,
            *_codec_arguments(codec),
            "-f",
            muxer,
            str(output),
        ]
        _run_checked(command, timeout=timeout, input_bytes=frame)
        if not output.is_file() or output.stat().st_size == 0:
            raise FFmpegEncodingError(f"Encoder {codec!r} returned no non-empty {muxer} output")
        _decode_first_frame_png(
            provision,
            output,
            timeout=timeout,
        )


def _automatic_codec_candidates() -> list[str]:
    candidates: list[str] = []
    if sys.platform == "darwin":
        candidates.append("h264_videotoolbox")
    elif sys.platform == "win32":
        candidates.append("h264_mf")
    candidates.extend([DEFAULT_CODEC, "mpeg4"])
    return candidates


def require_video_encoder(
    *,
    ffmpeg_path: str | os.PathLike[str] | None = None,
    ffprobe_path: str | os.PathLike[str] | None = None,
    codec: str | None = None,
    pixel_format: str | None = None,
    muxer: str | None = None,
) -> FFmpegProvision:
    """Resolve and prove a usable codec before recording starts.

    A caller- or Desktop-declared codec is an exact contract and fails loudly.
    With no declared codec, usable platform encoders are tried in order and the
    portable LGPL ``mpeg4`` encoder is the final fallback.
    """
    provision = resolve_ffmpeg(ffmpeg_path, ffprobe_path)
    exact_codec = codec or provision.codec
    candidates = [exact_codec] if exact_codec else _automatic_codec_candidates()
    candidates = list(dict.fromkeys(candidate for candidate in candidates if candidate))
    encoders = available_encoders(provision)
    failures: list[str] = []
    selected_muxer = _validate_option_token(
        "muxer",
        muxer or provision.muxer or "mp4",
    )
    for candidate in candidates:
        candidate = _validate_option_token("codec", candidate)
        if candidate not in encoders:
            failures.append(f"{candidate}: not advertised")
            continue
        selected_pixel_format = (
            "yuv420p"
            if candidate == "mpeg4" or candidate.startswith(("h264_", "hevc_"))
            else pixel_format or provision.pixel_format or DEFAULT_PIXEL_FORMAT
        )
        selected_pixel_format = _validate_option_token(
            "pixel format",
            selected_pixel_format,
        )
        try:
            _probe_encoder(
                provision,
                candidate,
                selected_pixel_format,
                selected_muxer,
            )
        except FFmpegEncodingError as exc:
            failures.append(f"{candidate}: {exc}")
            continue
        if not exact_codec and candidate != candidates[0]:
            logger.info(
                f"Selected verified video encoder {candidate!r} after earlier "
                "automatic candidates were unavailable"
            )
        return FFmpegProvision(
            executable=provision.executable,
            ffprobe=provision.ffprobe,
            codec=candidate,
            pixel_format=selected_pixel_format,
            muxer=selected_muxer,
            source=provision.source,
        )

    requested = f"requested encoder {exact_codec!r}" if exact_codec else "any encoder"
    detail = "; ".join(failures) or "no candidates"
    raise FFmpegUnavailableError(
        f"FFmpeg from {provision.source} could not actually encode and decode "
        f"with {requested}: {provision.executable}. {detail}"
    )


class FFmpegFrameStage:
    """Compatibility writer that streams timestamped frames directly to FFmpeg."""

    def __init__(
        self,
        output_path: str | Path,
        stream: FFmpegVideoStream,
        provision: FFmpegProvision,
        *,
        crf: int = DEFAULT_CRF,
        preset: str = DEFAULT_PRESET,
        timeout_seconds: float = DEFAULT_PROCESS_TIMEOUT_SECONDS,
    ) -> None:
        self.output_path = Path(output_path).resolve()
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = stream
        self.provision = provision
        self.crf = crf
        self.preset = preset
        self.timeout_seconds = timeout_seconds
        self.partial_path = self.output_path.with_name(
            f".{self.output_path.name}.{uuid.uuid4().hex}.partial.{self.stream.muxer}"
        )
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr_file: BinaryIO | None = None
        self._input_queue: queue.Queue[tuple[bytes, int] | None] = queue.Queue(maxsize=4)
        self._input_thread: threading.Thread | None = None
        self._input_error: BaseException | None = None
        self._last_frame: bytes | None = None
        self._first_pts: int | None = None
        self._last_pts = -1
        self._emitted_frames = 0
        self._logical_frames: list[tuple[int, float]] = []
        self._closed = False
        self._lock = threading.Lock()

    def _encode_command(self) -> list[str]:
        rate = self.stream.average_rate
        command = [
            self.provision.executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pixel_format",
            "rgb24",
            "-video_size",
            f"{self.stream.width}x{self.stream.height}",
            "-framerate",
            f"{rate.numerator}/{rate.denominator}",
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            self.stream.codec,
            "-pix_fmt",
            self.stream.pix_fmt,
        ]
        command.extend(
            _codec_arguments(
                self.stream.codec,
                crf=self.crf,
                preset=self.preset,
            )
        )
        command.extend(["-f", self.stream.muxer])
        command.append(str(self.partial_path))
        return command

    def _start(self) -> subprocess.Popen[bytes]:
        stderr_file = tempfile.TemporaryFile(mode="w+b")
        try:
            process = subprocess.Popen(
                self._encode_command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                bufsize=0,
                shell=False,
            )
        except OSError as exc:
            stderr_file.close()
            raise FFmpegEncodingError(f"Could not execute FFmpeg: {exc}") from exc
        if process.stdin is None:
            process.kill()
            process.wait()
            stderr_file.close()
            raise FFmpegEncodingError("FFmpeg did not expose its raw-video input pipe")
        self._stderr_file = stderr_file
        self._process = process
        self._input_thread = threading.Thread(
            target=self._write_input,
            name="openadapt-ffmpeg-input",
            daemon=True,
        )
        try:
            self._input_thread.start()
        except BaseException:
            process.kill()
            process.wait()
            stderr_file.close()
            self._process = None
            self._stderr_file = None
            self._input_thread = None
            raise
        return process

    def _stderr_detail(self) -> str:
        stderr_file = self._stderr_file
        if stderr_file is None:
            return ""
        stderr_file.flush()
        stderr_file.seek(0, os.SEEK_END)
        size = stderr_file.tell()
        stderr_file.seek(max(0, size - 64 * 1024))
        return stderr_file.read().decode("utf-8", errors="replace").strip()

    def _write_input(self) -> None:
        process = self._process
        assert process is not None and process.stdin is not None
        try:
            while True:
                item = self._input_queue.get()
                try:
                    if item is None:
                        return
                    frame, repetitions = item
                    for _ in range(repetitions):
                        remaining = memoryview(frame)
                        while remaining:
                            written = process.stdin.write(remaining)
                            if written is None or written <= 0:
                                raise OSError("FFmpeg input pipe made no write progress")
                            remaining = remaining[written:]
                    process.stdin.flush()
                finally:
                    self._input_queue.task_done()
        except BaseException as exc:
            self._input_error = exc
        finally:
            try:
                process.stdin.close()
            except BaseException as exc:
                if self._input_error is None:
                    self._input_error = exc

    def _abort_and_reap(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        input_thread = self._input_thread
        if input_thread is not None and input_thread is not threading.current_thread():
            input_thread.join(timeout=5)

    def _enqueue_frames(self, frame: bytes, repetitions: int) -> None:
        if repetitions <= 0:
            return
        process = self._process or self._start()
        if process.poll() is not None:
            detail = self._stderr_detail()
            raise FFmpegEncodingError(
                f"FFmpeg exited with code {process.returncode}: {detail or '(no stderr)'}"
            )
        if self._input_error is not None:
            self._abort_and_reap()
            detail = self._stderr_detail()
            raise FFmpegEncodingError(
                f"FFmpeg input pipe failed: {detail or self._input_error}"
            ) from self._input_error
        try:
            self._input_queue.put(
                (frame, repetitions),
                timeout=min(self.timeout_seconds, FRAME_WRITE_TIMEOUT_SECONDS),
            )
        except queue.Full as exc:
            self._abort_and_reap()
            detail = self._stderr_detail()
            raise FFmpegEncodingError(
                "FFmpeg input pipe stopped accepting frames" + (f": {detail}" if detail else "")
            ) from exc

    def stage_frame(self, image: "PILImage", pts: int) -> None:
        """Stream one frame, filling PTS gaps deterministically without disk."""
        with self._lock:
            if self._closed:
                raise FFmpegEncodingError("Video stream is already closed")
            if pts <= self._last_pts:
                raise FFmpegEncodingError(f"Video PTS must increase ({pts} <= {self._last_pts})")
            if image.size != (self.stream.width, self.stream.height):
                raise FFmpegEncodingError(
                    f"Video frame size {image.size} does not match "
                    f"{self.stream.width}x{self.stream.height}"
                )
            frame = image.convert("RGB").tobytes()
            fps = float(self.stream.average_rate)
            if self._first_pts is None:
                self._enqueue_frames(frame, 1)
                self._first_pts = pts
                encoded_index = 0
                emitted = 1
            else:
                assert self._last_frame is not None
                gap = pts - self._last_pts
                self._enqueue_frames(self._last_frame, gap - 1)
                self._enqueue_frames(frame, 1)
                encoded_index = self._emitted_frames + gap - 1
                emitted = gap
            assert self._first_pts is not None
            self._logical_frames.append((encoded_index, (pts - self._first_pts) / fps))
            self._emitted_frames += emitted
            self._last_frame = frame
            self._last_pts = pts

    def _finish_input(self) -> None:
        process = self._process
        input_thread = self._input_thread
        assert process is not None and input_thread is not None
        try:
            self._input_queue.put(
                None,
                timeout=min(self.timeout_seconds, FRAME_WRITE_TIMEOUT_SECONDS),
            )
        except queue.Full as exc:
            self._abort_and_reap()
            raise FFmpegEncodingError("FFmpeg input queue did not finish") from exc
        input_thread.join(timeout=min(self.timeout_seconds, FRAME_WRITE_TIMEOUT_SECONDS))
        if input_thread.is_alive():
            self._abort_and_reap()
            detail = self._stderr_detail()
            raise FFmpegEncodingError(
                f"FFmpeg input pipe timed out after "
                f"{min(self.timeout_seconds, FRAME_WRITE_TIMEOUT_SECONDS):g}s"
                + (f": {detail}" if detail else "")
            )
        if self._input_error is not None:
            self._abort_and_reap()
            detail = self._stderr_detail()
            raise FFmpegEncodingError(
                f"FFmpeg input pipe failed: {detail or self._input_error}"
            ) from self._input_error

    def _wait_for_process(self, process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self._abort_and_reap()
            detail = self._stderr_detail()
            raise FFmpegEncodingError(
                f"FFmpeg timed out after {self.timeout_seconds:g}s"
                + (f": {detail}" if detail else "")
            ) from exc

    def close(self) -> None:
        """Finalize, verify, and atomically promote the directly encoded video."""
        with self._lock:
            if self._closed:
                return
            if self._process is None:
                self._closed = True
                return
            process = self._process
            try:
                self._finish_input()
                self._wait_for_process(process)
                if process.returncode != 0:
                    detail = self._stderr_detail()
                    raise FFmpegEncodingError(
                        f"FFmpeg exited with code {process.returncode}: {detail or '(no stderr)'}"
                    )
                if not self.partial_path.is_file() or self.partial_path.stat().st_size == 0:
                    raise FFmpegEncodingError("FFmpeg returned success without a non-empty output")
                _append_timing_box(
                    self.partial_path,
                    fps=self.stream.average_rate,
                    frames=self._logical_frames,
                )
                _decode_first_frame_png(
                    self.provision,
                    self.partial_path,
                    timeout=min(self.timeout_seconds, EXTRACT_TIMEOUT_SECONDS),
                )
                os.replace(self.partial_path, self.output_path)
            except BaseException:
                self._abort_and_reap()
                logger.error(
                    f"Video encoding failed; retained incomplete output at {self.partial_path}"
                )
                raise
            finally:
                if self._stderr_file is not None:
                    self._stderr_file.close()
                self._closed = True


class VideoWriter:
    """MP4 writer backed by a separately provisioned FFmpeg executable."""

    def __init__(
        self,
        output_path: str | Path,
        width: int,
        height: int,
        fps: int = DEFAULT_FPS,
        codec: str | None = None,
        pix_fmt: str | None = None,
        muxer: str | None = None,
        crf: int = DEFAULT_CRF,
        preset: str = DEFAULT_PRESET,
        *,
        ffmpeg_path: str | os.PathLike[str] | None = None,
        ffprobe_path: str | os.PathLike[str] | None = None,
        timeout_seconds: float = DEFAULT_PROCESS_TIMEOUT_SECONDS,
    ) -> None:
        provision = require_video_encoder(
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            codec=codec,
            pixel_format=pix_fmt,
            muxer=muxer,
        )
        self.output_path = Path(output_path)
        self.width = width
        self.height = height
        self.fps = fps
        self.codec = provision.codec or codec or DEFAULT_CODEC
        self.pix_fmt = provision.pixel_format or pix_fmt or DEFAULT_PIXEL_FORMAT
        self.muxer = provision.muxer or muxer or "mp4"
        self.crf = crf
        self.preset = preset
        self._stream = FFmpegVideoStream(
            width=width,
            height=height,
            average_rate=Fraction(fps),
            pix_fmt=self.pix_fmt,
            codec=self.codec,
            muxer=self.muxer,
        )
        self._container = FFmpegFrameStage(
            output_path,
            self._stream,
            provision,
            crf=crf,
            preset=preset,
            timeout_seconds=timeout_seconds,
        )
        self._start_time: float | None = None
        self._last_pts = -1
        self._lock = threading.Lock()

    @property
    def start_time(self) -> float | None:
        return self._start_time

    @property
    def is_open(self) -> bool:
        return self._start_time is not None and not self._container._closed

    def write_frame(
        self,
        image: "PILImage",
        timestamp: float,
        force_key_frame: bool = False,
    ) -> None:
        del force_key_frame  # FFmpeg makes the first encoded frame a key frame.
        with self._lock:
            if self._start_time is None:
                self._start_time = timestamp
            self._last_pts = write_video_frame(
                self._container,
                self._stream,
                image,
                timestamp,
                self._start_time,
                self._last_pts,
            )

    def close(self) -> None:
        with self._lock:
            self._container.close()

    def __enter__(self) -> "VideoWriter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


def get_video_file_path(recording_timestamp: float, video_dir: str = None) -> str:
    if video_dir is None:
        video_dir = os.path.join(os.getcwd(), "video")
    os.makedirs(video_dir, exist_ok=True)
    return os.path.join(video_dir, f"oa_recording-{recording_timestamp}.mp4")


def initialize_video_writer(
    output_path: str,
    width: int,
    height: int,
    fps: int = DEFAULT_FPS,
    codec: str | None = None,
    pix_fmt: str | None = None,
    muxer: str | None = None,
    crf: int = DEFAULT_CRF,
    preset: str = DEFAULT_PRESET,
    *,
    ffmpeg_path: str | os.PathLike[str] | None = None,
    ffprobe_path: str | os.PathLike[str] | None = None,
    timeout_seconds: float | None = None,
    preflight_provision: FFmpegProvision | None = None,
) -> tuple[FFmpegFrameStage, FFmpegVideoStream, float]:
    if preflight_provision is None:
        selected_path = ffmpeg_path or config.VIDEO_FFMPEG_PATH
        provision = require_video_encoder(
            ffmpeg_path=selected_path,
            ffprobe_path=ffprobe_path or config.VIDEO_FFPROBE_PATH,
            codec=codec or config.VIDEO_ENCODING,
            pixel_format=pix_fmt or config.VIDEO_PIXEL_FORMAT,
            muxer=muxer or config.VIDEO_MUXER,
        )
    else:
        provision = preflight_provision
        if not provision.codec or not provision.pixel_format or not provision.muxer:
            raise FFmpegUnavailableError(
                "Preflight FFmpeg provision must include codec, pixel format, and muxer"
            )
    selected_codec = provision.codec or codec or config.VIDEO_ENCODING or DEFAULT_CODEC
    selected_pixel_format = (
        provision.pixel_format or pix_fmt or config.VIDEO_PIXEL_FORMAT or DEFAULT_PIXEL_FORMAT
    )
    selected_muxer = provision.muxer or muxer or config.VIDEO_MUXER or "mp4"
    stream = FFmpegVideoStream(
        width=width,
        height=height,
        average_rate=Fraction(fps),
        pix_fmt=selected_pixel_format,
        codec=selected_codec,
        muxer=selected_muxer,
    )
    container = FFmpegFrameStage(
        output_path,
        stream,
        provision,
        crf=crf,
        preset=preset,
        timeout_seconds=timeout_seconds or config.VIDEO_FFMPEG_TIMEOUT_SECONDS,
    )
    return container, stream, utils.get_timestamp()


def write_video_frame(
    video_container: FFmpegFrameStage,
    video_stream: FFmpegVideoStream,
    screenshot: "PILImage",
    timestamp: float,
    video_start_timestamp: float,
    last_pts: int,
    force_key_frame: bool = False,
) -> int:
    del force_key_frame
    time_diff = max(timestamp - video_start_timestamp, 0.0)
    pts = int(time_diff * float(video_stream.average_rate))
    if pts <= last_pts:
        pts = last_pts + 1
    video_container.stage_frame(screenshot, pts)
    return pts


def finalize_video_writer(
    video_container: FFmpegFrameStage,
    video_stream: FFmpegVideoStream,
    video_start_timestamp: float,
    last_frame: "PILImage",
    last_frame_timestamp: float,
    last_pts: int,
    video_file_path: str,
    fix_moov: bool = False,
) -> None:
    write_video_frame(
        video_container,
        video_stream,
        last_frame,
        last_frame_timestamp,
        video_start_timestamp,
        last_pts,
        force_key_frame=True,
    )
    video_container.close()
    if fix_moov:
        move_moov_atom(video_file_path)


def move_moov_atom(
    input_file: str,
    output_file: str = None,
    *,
    ffmpeg_path: str | os.PathLike[str] | None = None,
) -> None:
    provision = resolve_ffmpeg(ffmpeg_path or config.VIDEO_FFMPEG_PATH)
    input_path = Path(input_file)
    timing = _read_timing_box(input_path)
    temp_file: Path | None = None
    if output_file is None:
        temp_file = input_path.with_name(f".{input_path.name}.{uuid.uuid4().hex}.mp4")
        output_path = temp_file
    else:
        output_path = Path(output_file)
    _run_checked(
        [
            provision.executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(input_path),
            "-codec",
            "copy",
            "-movflags",
            "faststart",
            str(output_path),
        ],
        timeout=DEFAULT_PROCESS_TIMEOUT_SECONDS,
    )
    if timing is not None:
        fps, logical_frames = timing
        _append_timing_box(output_path, fps=fps, frames=logical_frames)
    if temp_file is not None:
        os.replace(temp_file, input_path)


def _require_ffprobe(provision: FFmpegProvision) -> str:
    if provision.ffprobe is None:
        raise FFmpegUnavailableError(
            "Exact frame lookup and video metadata require a separately "
            "provisioned ffprobe executable. Put ffprobe beside FFmpeg, set "
            "OPENADAPT_FFPROBE_PATH, pass ffprobe_path=..., or declare it in "
            "Desktop's ffmpeg.json manifest."
        )
    return provision.ffprobe


def _probe_json(
    provision: FFmpegProvision,
    command: Sequence[str],
    *,
    timeout: float = EXTRACT_TIMEOUT_SECONDS,
) -> dict:
    result = _run_checked(
        [
            _require_ffprobe(provision),
            "-v",
            "error",
            *command,
            "-of",
            "json",
        ],
        timeout=timeout,
    )
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FFmpegEncodingError("ffprobe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise FFmpegEncodingError("ffprobe returned a non-object JSON payload")
    return payload


def _frame_catalog(
    video_path: Path,
    provision: FFmpegProvision,
) -> list[tuple[int, float]]:
    """Return decoded-frame indexes and timestamps in presentation order."""
    timing = _read_timing_box(video_path)
    if timing is not None:
        return timing[1]
    payload = _probe_json(
        provision,
        [
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time,pts_time",
            str(video_path),
        ],
    )
    catalog: list[tuple[int, float]] = []
    for index, frame in enumerate(payload.get("frames", [])):
        if not isinstance(frame, dict):
            continue
        raw = frame.get("best_effort_timestamp_time", frame.get("pts_time"))
        try:
            timestamp = float(raw)
        except (TypeError, ValueError):
            continue
        catalog.append((index, timestamp))
    return catalog


def _extract_frame_index_png(
    video_path: Path,
    frame_index: int,
    provision: FFmpegProvision,
) -> "PILImage":
    result = _run_checked(
        [
            provision.executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(video_path),
            "-vf",
            f"select=eq(n\\,{frame_index})",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    if not result.stdout:
        raise ValueError(f"No decoded video frame at index {frame_index}")

    with Image.open(io.BytesIO(result.stdout)) as image:
        return image.convert("RGB").copy()


def extract_frames(
    video_path: str | Path,
    timestamps: list[float],
    tolerance: float = 0.1,
    *,
    ffmpeg_path: str | os.PathLike[str] | None = None,
    ffprobe_path: str | os.PathLike[str] | None = None,
) -> list["PILImage"]:
    provision = resolve_ffmpeg(
        ffmpeg_path or config.VIDEO_FFMPEG_PATH,
        ffprobe_path or config.VIDEO_FFPROBE_PATH,
    )
    path = Path(video_path)
    catalog = _frame_catalog(path, provision)
    selected: list[int] = []
    missing: list[float] = []
    for target in timestamps:
        nearest = min(
            catalog,
            key=lambda item: abs(item[1] - target),
            default=None,
        )
        if nearest is None or abs(nearest[1] - target) > tolerance:
            missing.append(target)
        else:
            selected.append(nearest[0])
    if missing:
        raise ValueError(f"No frame within tolerance for timestamps: {missing}")

    images_by_index = {
        index: _extract_frame_index_png(path, index, provision) for index in dict.fromkeys(selected)
    }
    return [images_by_index[index].copy() for index in selected]


def extract_frame(
    video_path: str | Path,
    timestamp: float,
    tolerance: float = 0.1,
    *,
    ffmpeg_path: str | os.PathLike[str] | None = None,
    ffprobe_path: str | os.PathLike[str] | None = None,
) -> "PILImage":
    return extract_frames(
        video_path,
        [timestamp],
        tolerance,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )[0]


def get_video_info(
    video_path: str | Path,
    *,
    ffmpeg_path: str | os.PathLike[str] | None = None,
    ffprobe_path: str | os.PathLike[str] | None = None,
) -> dict:
    provision = resolve_ffmpeg(
        ffmpeg_path or config.VIDEO_FFMPEG_PATH,
        ffprobe_path or config.VIDEO_FFPROBE_PATH,
    )
    payload = _probe_json(
        provision,
        [
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_streams",
            "-show_format",
            "-show_entries",
            (
                "stream=width,height,codec_name,avg_frame_rate,nb_frames,"
                "nb_read_frames,duration:format=duration"
            ),
            str(video_path),
        ],
    )
    streams = payload.get("streams", [])
    stream = streams[0] if streams and isinstance(streams[0], dict) else {}
    format_info = payload.get("format", {})
    if not isinstance(format_info, dict):
        format_info = {}

    def _number(value, cast):
        try:
            return cast(value)
        except (TypeError, ValueError):
            return None

    fps = None
    rate = stream.get("avg_frame_rate")
    if isinstance(rate, str) and rate not in {"0/0", "N/A"}:
        try:
            fps = float(Fraction(rate))
        except (ValueError, ZeroDivisionError):
            pass
    duration = _number(stream.get("duration"), float)
    if duration is None:
        duration = _number(format_info.get("duration"), float)
    frames = _number(stream.get("nb_frames"), int)
    if frames is None:
        frames = _number(stream.get("nb_read_frames"), int)
    return {
        "duration": duration,
        "width": _number(stream.get("width"), int),
        "height": _number(stream.get("height"), int),
        "fps": fps,
        "codec": stream.get("codec_name"),
        "frames": frames,
    }


class ChunkedVideoWriter:
    """Video writer that splits output into time-bounded MP4 chunks."""

    def __init__(
        self,
        output_dir: str | Path,
        width: int,
        height: int,
        chunk_duration: float = 600.0,
        fps: int = DEFAULT_FPS,
        codec: str | None = None,
        pix_fmt: str | None = None,
        muxer: str = "mp4",
        crf: int = DEFAULT_CRF,
        preset: str = DEFAULT_PRESET,
        *,
        ffmpeg_path: str | os.PathLike[str] | None = None,
        ffprobe_path: str | os.PathLike[str] | None = None,
        timeout_seconds: float = DEFAULT_PROCESS_TIMEOUT_SECONDS,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.width = width
        self.height = height
        self.chunk_duration = chunk_duration
        self.fps = fps
        self.codec = codec
        self.pix_fmt = pix_fmt
        self.muxer = muxer
        self.crf = crf
        self.preset = preset
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.timeout_seconds = timeout_seconds
        self._current_writer: VideoWriter | None = None
        self._chunk_index = 0
        self._chunk_start_time: float | None = None
        self._start_time: float | None = None
        self._lock = threading.Lock()

    @property
    def start_time(self) -> float | None:
        return self._start_time

    @property
    def chunk_paths(self) -> list[Path]:
        return sorted(self.output_dir.glob("chunk_*.mp4"))

    def _get_chunk_path(self, index: int) -> Path:
        return self.output_dir / f"chunk_{index:04d}.mp4"

    def _start_new_chunk(self, timestamp: float) -> None:
        if self._current_writer is not None:
            self._current_writer.close()
        self._current_writer = VideoWriter(
            self._get_chunk_path(self._chunk_index),
            width=self.width,
            height=self.height,
            fps=self.fps,
            codec=self.codec,
            pix_fmt=self.pix_fmt,
            muxer=self.muxer,
            crf=self.crf,
            preset=self.preset,
            ffmpeg_path=self.ffmpeg_path,
            ffprobe_path=self.ffprobe_path,
            timeout_seconds=self.timeout_seconds,
        )
        self._chunk_start_time = timestamp
        self._chunk_index += 1

    def write_frame(
        self,
        image: "PILImage",
        timestamp: float,
        force_key_frame: bool = False,
    ) -> None:
        with self._lock:
            if self._start_time is None:
                self._start_time = timestamp
            needs_new_chunk = self._current_writer is None or (
                self._chunk_start_time is not None
                and timestamp - self._chunk_start_time >= self.chunk_duration
            )
            if needs_new_chunk:
                self._start_new_chunk(timestamp)
                force_key_frame = True
            assert self._current_writer is not None
            self._current_writer.write_frame(image, timestamp, force_key_frame)

    def close(self) -> None:
        with self._lock:
            if self._current_writer is not None:
                self._current_writer.close()
                self._current_writer = None

    def __enter__(self) -> "ChunkedVideoWriter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
