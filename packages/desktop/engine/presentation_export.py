"""Deterministic presentation-video derivatives for exact overlay timelines.

Raw recordings remain immutable. This module validates the canonical Types 0.5
timeline, binds it to the exact source-media hash, composites in memory over a
rawvideo pipe, and atomically publishes a separate MP4 derivative.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import uuid
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO, Literal

from openadapt_types import (
    ControlOverlayFrameV2,
    ControlOverlayMediaFrameBindingV2,
    ControlOverlayTimelineV2,
)
from PIL import Image, ImageDraw, ImageFont

TIMELINE_SCHEMA = "openadapt.control-overlay-timeline/v2"
MAX_TIMELINE_BYTES = 8 * 1024 * 1024
MAX_FRAME_PIXELS = 33_177_600  # 8K UHD
MAX_MEDIA_FRAMES = 2_000_000
PlacementCorner = Literal["bottom-left", "bottom-right"]


@dataclass(frozen=True)
class PresentationExportPlan:
    capture_dir: Path
    timeline_path: Path
    media_path: Path
    media_sha256: str
    timeline: ControlOverlayTimelineV2


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timeline_candidates(capture_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in capture_dir.rglob("*.json"):
        if (
            not path.is_file()
            or path.is_symlink()
            or not path.resolve().is_relative_to(capture_dir)
            or path.stat().st_size > MAX_TIMELINE_BYTES
        ):
            continue
        try:
            prefix = path.read_text(encoding="utf-8")[:4096]
        except (OSError, UnicodeDecodeError):
            continue
        if TIMELINE_SCHEMA in prefix:
            candidates.append(path)
    return sorted(candidates)


def inspect_capture_for_presentation(capture_dir: Path) -> PresentationExportPlan:
    """Resolve one exact canonical timeline and its hash-bound raw MP4."""

    capture_dir = capture_dir.resolve(strict=True)
    timelines = _timeline_candidates(capture_dir)
    if len(timelines) != 1:
        raise ValueError(
            "presentation export requires exactly one canonical V2 overlay timeline; "
            f"found {len(timelines)}"
        )
    timeline_path = timelines[0]
    timeline = ControlOverlayTimelineV2.model_validate_json(
        timeline_path.read_text(encoding="utf-8")
    )
    if timeline.media_frame_count > MAX_MEDIA_FRAMES:
        raise ValueError("presentation timeline exceeds the local frame-count limit")

    matching: list[Path] = []
    for path in sorted(capture_dir.rglob("*.mp4")):
        if (
            "presentation" in path.relative_to(capture_dir).parts
            or not path.is_file()
            or path.is_symlink()
            or not path.resolve().is_relative_to(capture_dir)
        ):
            continue
        if _sha256(path) == timeline.media_sha256:
            matching.append(path)
    if len(matching) != 1:
        raise ValueError(
            "presentation timeline must bind exactly one raw MP4 in this capture; "
            f"found {len(matching)}"
        )
    return PresentationExportPlan(
        capture_dir=capture_dir,
        timeline_path=timeline_path,
        media_path=matching[0],
        media_sha256=timeline.media_sha256,
        timeline=timeline,
    )


def presentation_export_status(capture_dir: Path) -> dict[str, object]:
    try:
        plan = inspect_capture_for_presentation(capture_dir)
    except (OSError, ValueError) as error:
        return {"ready": False, "reason": str(error)}
    return {
        "ready": True,
        "reason": None,
        "media_sha256": plan.media_sha256,
        "media_frame_count": plan.timeline.media_frame_count,
    }


def _probe_video(ffprobe: Path, media: Path) -> tuple[int, int]:
    completed = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(media),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"could not inspect source video: {completed.stderr.strip()}")
    try:
        streams = json.loads(completed.stdout)["streams"]
        width = int(streams[0]["width"])
        height = int(streams[0]["height"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("source video has no usable video stream") from error
    if width <= 0 or height <= 0 or width * height > MAX_FRAME_PIXELS:
        raise RuntimeError("source video dimensions exceed the presentation boundary")
    return width, height


def _runtime_supports_rawvideo(ffmpeg: Path) -> bool:
    completed = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-encoders"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return completed.returncode == 0 and " rawvideo " in completed.stdout


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _phase_color(phase: str) -> tuple[int, int, int, int]:
    if phase == "verified":
        return (48, 190, 124, 255)
    if phase in {"halted", "failed", "completed_unverified", "rolled_back"}:
        return (244, 104, 83, 255)
    if phase in {"paused", "pausing", "stopping"}:
        return (244, 185, 66, 255)
    return (99, 102, 241, 255)


def _plain_status(phase: str) -> str:
    return {
        "idle": "Ready",
        "observing": "Resolving target",
        "recording": "Recording demonstration",
        "executing": "Running workflow",
        "pausing": "Pausing safely",
        "paused": "Paused safely",
        "resuming": "Resuming safely",
        "stopping": "Stopping safely",
        "verifying": "Verifying result",
        "verified": "Outcome verified",
        "completed_unverified": "Needs verification",
        "halted": "Stopped safely",
        "failed": "Run failed",
        "rolled_back": "Change rolled back",
    }[phase]


def _safety_label(phase: str) -> str:
    return {
        "verified": "VERIFIED",
        "completed_unverified": "UNVERIFIED",
        "halted": "HALTED",
        "failed": "FAILED",
        "rolled_back": "ROLLED BACK",
        "paused": "PAUSED",
        "recording": "LOCAL CAPTURE",
        "observing": "LOCAL CAPTURE",
    }.get(phase, "CHECKS ACTIVE")


def _expanded_phase(phase: str) -> bool:
    return phase in {
        "paused",
        "verified",
        "completed_unverified",
        "halted",
        "failed",
        "rolled_back",
    }


def _intersection_area(
    first: tuple[int, int, int, int], second: tuple[int, int, int, int]
) -> int:
    width = max(0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0, min(first[3], second[3]) - max(first[1], second[1]))
    return width * height


def choose_capsule_bounds(
    *,
    width: int,
    height: int,
    panel_width: int,
    panel_height: int,
    margin: int,
    protected_regions: tuple[tuple[int, int, int, int], ...] = (),
) -> tuple[tuple[int, int, int, int], PlacementCorner]:
    """Choose the least-conflicting bottom corner, preferring bottom-left."""

    bottom = max(panel_height, height - margin)
    top = max(0, bottom - panel_height)
    right_bounds = (
        max(0, width - margin - panel_width),
        top,
        max(panel_width, width - margin),
        bottom,
    )
    left_bounds = (
        min(margin, max(0, width - panel_width)),
        top,
        min(width, margin + panel_width),
        bottom,
    )

    def collision_area(bounds: tuple[int, int, int, int]) -> int:
        return sum(_intersection_area(bounds, region) for region in protected_regions)

    left_collision = collision_area(left_bounds)
    right_collision = collision_area(right_bounds)
    if left_collision > 0 and right_collision < left_collision:
        return right_bounds, "bottom-right"
    return left_bounds, "bottom-left"


def _capsule_bounds_for_corner(
    *,
    width: int,
    height: int,
    panel_width: int,
    panel_height: int,
    margin: int,
    corner: PlacementCorner,
) -> tuple[int, int, int, int]:
    """Return one explicit corner without re-evaluating frame-local evidence."""

    bottom = max(panel_height, height - margin)
    top = max(0, bottom - panel_height)
    if corner == "bottom-right":
        return (
            max(0, width - margin - panel_width),
            top,
            max(panel_width, width - margin),
            bottom,
        )
    return (
        min(margin, max(0, width - panel_width)),
        top,
        min(width, margin + panel_width),
        bottom,
    )


def _panel_dimensions(
    *, width: int, height: int, expanded: bool
) -> tuple[int, int, int]:
    margin = max(12, round(min(width, height) * 0.02))
    available_width = max(1, width - 2 * margin)
    available_height = max(1, height - 2 * margin)
    panel_width = min(max(320, round(width * 0.36)), available_width)
    panel_height = min(112 if expanded else 88, available_height)
    return margin, panel_width, panel_height


def _target_rect(
    frame: ControlOverlayFrameV2,
    *,
    frame_index: int,
    media_sha256: str,
    width: int,
    height: int,
) -> tuple[int, int, int, int] | None:
    target = frame.target_tracking
    if target is None or not isinstance(target.binding, ControlOverlayMediaFrameBindingV2):
        return None
    if (
        target.binding.media_sha256 != media_sha256
        or target.binding.frame_index != frame_index
    ):
        return None
    viewport = target.source_viewport
    expected_width = viewport.width_css_px * viewport.device_pixel_ratio
    expected_height = viewport.height_css_px * viewport.device_pixel_ratio
    # A full-frame mapping is valid only when the exact media dimensions prove
    # that the video is the CSS viewport. Browser chrome offsets are never
    # guessed. A future producer may carry an explicit content-box contract.
    if not (
        math.isclose(expected_width, width, abs_tol=1.0)
        and math.isclose(expected_height, height, abs_tol=1.0)
    ):
        return None
    rect = target.rect
    left = round(rect.x * width)
    top = round(rect.y * height)
    right = round((rect.x + rect.width) * width)
    bottom = round((rect.y + rect.height) * height)
    return left, top, right, bottom


def _step_key(frame: ControlOverlayFrameV2) -> tuple[int | None, int | None]:
    return frame.step.current, frame.step.total


def build_step_placement_plan(
    timeline: ControlOverlayTimelineV2,
    *,
    width: int,
    height: int,
    media_sha256: str,
) -> dict[int, PlacementCorner]:
    """Bind one collision-aware corner to each contiguous workflow-step segment.

    Placement may use only exact target geometry retained for an inventoried
    media frame. Once selected, it remains stable for every event in that step
    segment. The target rectangle itself is still rendered only on its exact
    bound frame; this plan never persists or interpolates target evidence.
    """

    placements: dict[int, PlacementCorner] = {}
    events = timeline.events
    segment_start = 0
    while segment_start < len(events):
        segment_key = _step_key(events[segment_start].frame)
        segment_end = segment_start + 1
        while (
            segment_end < len(events)
            and _step_key(events[segment_end].frame) == segment_key
        ):
            segment_end += 1
        segment = events[segment_start:segment_end]
        exact_targets = tuple(
            target
            for event in segment
            if event.frame.visible
            for target in (
                _target_rect(
                    event.frame,
                    frame_index=event.media_frame_index,
                    media_sha256=media_sha256,
                    width=width,
                    height=height,
                ),
            )
            if target is not None
        )
        expanded = any(
            event.frame.visible and _expanded_phase(event.frame.phase.value)
            for event in segment
        )
        margin, panel_width, panel_height = _panel_dimensions(
            width=width,
            height=height,
            expanded=expanded,
        )
        _, corner = choose_capsule_bounds(
            width=width,
            height=height,
            panel_width=panel_width,
            panel_height=panel_height,
            margin=margin,
            protected_regions=exact_targets,
        )
        for event in segment:
            placements[event.media_frame_index] = corner
        segment_start = segment_end
    return placements


def render_presentation_frame(
    image: Image.Image,
    frame: ControlOverlayFrameV2,
    *,
    frame_index: int,
    media_sha256: str,
    protected_regions: tuple[tuple[int, int, int, int], ...] = (),
    placement_corner: PlacementCorner | None = None,
) -> Image.Image:
    """Render one canonical status frame and an optional exactly bound target."""

    output = image.convert("RGBA")
    if not frame.visible:
        return output
    draw = ImageDraw.Draw(output, "RGBA")
    width, height = output.size
    margin, panel_width, panel_height = _panel_dimensions(
        width=width,
        height=height,
        expanded=_expanded_phase(frame.phase.value),
    )
    target = _target_rect(
        frame,
        frame_index=frame_index,
        media_sha256=media_sha256,
        width=width,
        height=height,
    )
    avoidance = protected_regions + ((target,) if target is not None else ())
    if placement_corner is None:
        bounds, _corner = choose_capsule_bounds(
            width=width,
            height=height,
            panel_width=panel_width,
            panel_height=panel_height,
            margin=margin,
            protected_regions=avoidance,
        )
    else:
        bounds = _capsule_bounds_for_corner(
            width=width,
            height=height,
            panel_width=panel_width,
            panel_height=panel_height,
            margin=margin,
            corner=placement_corner,
        )
    left, top, right, bottom = bounds
    accent = _phase_color(frame.phase.value)
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=16,
        fill=(13, 18, 28, 226),
    )
    draw.rounded_rectangle((left, top, left + 8, bottom), radius=4, fill=accent)
    font = ImageFont.load_default(size=max(11, min(15, round(panel_height * 0.16))))
    draw.text(
        (left + 22, top + 14),
        _plain_status(frame.phase.value),
        fill=(245, 247, 250, 255),
        font=font,
    )
    step = "Step pending"
    if frame.step.current is not None and frame.step.total is not None:
        step = f"Step {frame.step.current} of {frame.step.total}"
    elif frame.step.current is not None:
        step = f"Step {frame.step.current}"
    elif frame.step.total is not None:
        step = f"{frame.step.total} steps"
    draw.text(
        (left + 22, top + 42),
        f"{step}  ·  {_safety_label(frame.phase.value)}",
        fill=(205, 211, 221, 255),
        font=font,
    )
    if frame.phase.value not in {"idle", "observing", "recording"} and bottom - top >= 70:
        draw.text(
            (left + 22, top + 66),
            "Resolve  —  Act  —  Verify",
            fill=(139, 148, 163, 255),
            font=font,
        )
    if _expanded_phase(frame.phase.value) and bottom - top >= 98:
        profile = f" · {frame.profile.value} profile" if frame.profile is not None else ""
        draw.text(
            (left + 22, top + 88),
            f"{frame.workflow_label.value}{profile}",
            fill=(177, 185, 198, 255),
            font=font,
        )

    if target is not None:
        line_width = max(3, round(min(width, height) * 0.006))
        draw.rounded_rectangle(target, radius=8, outline=accent, width=line_width)
    return output


def _wait_process(process: subprocess.Popen, label: str) -> None:
    if process.stdin is not None and process.stdin.closed:
        process.stdin = None
    _, stderr = process.communicate(timeout=60)
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{label} failed: {detail}")


def export_presentation_video(capture_dir: Path) -> dict[str, object]:
    """Compose a new MP4 from one exact raw medium and canonical V2 timeline."""

    plan = inspect_capture_for_presentation(capture_dir)
    ffmpeg = Path(os.environ.get("OPENADAPT_FFMPEG_PATH", ""))
    ffprobe = Path(os.environ.get("OPENADAPT_FFPROBE_PATH", ""))
    if not ffmpeg.is_absolute() or not ffmpeg.is_file():
        raise RuntimeError("the managed FFmpeg runtime is not ready")
    if not ffprobe.is_absolute() or not ffprobe.is_file():
        raise RuntimeError("the managed ffprobe runtime is not ready")
    if not _runtime_supports_rawvideo(ffmpeg):
        raise RuntimeError(
            "the installed managed video runtime predates direct rawvideo composition; retry "
            "runtime provisioning after the OpenAdapt FFmpeg r2 update"
        )

    width, height = _probe_video(ffprobe, plan.media_path)
    frame_bytes = width * height * 4
    timeline = plan.timeline
    frame_rate = Fraction(timeline.media_frame_count * 1000, timeline.duration_ms)
    rate = f"{frame_rate.numerator}/{frame_rate.denominator}"
    presentation_dir = plan.capture_dir / "presentation"
    if presentation_dir.is_symlink():
        raise RuntimeError("presentation output directory cannot be a symbolic link")
    presentation_dir.mkdir(parents=True, exist_ok=True)
    if not presentation_dir.resolve().is_relative_to(plan.capture_dir):
        raise RuntimeError("presentation output directory escaped the capture boundary")
    token = uuid.uuid4().hex
    encoded = presentation_dir / f".{token}.video.mp4"
    remuxed = presentation_dir / f".{token}.complete.mp4"
    output = presentation_dir / f"{plan.media_path.stem}-openadapt-{token[:8]}.mp4"

    decoder = subprocess.Popen(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(plan.media_path),
            "-map",
            "0:v:0",
            "-fps_mode",
            "passthrough",
            "-pix_fmt",
            "rgba",
            "-c:v",
            "rawvideo",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    encoder = subprocess.Popen(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "rawvideo",
            "-pixel_format",
            "rgba",
            "-video_size",
            f"{width}x{height}",
            "-framerate",
            rate,
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "mpeg4",
            "-q:v",
            "3",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            str(encoded),
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if decoder.stdout is None or encoder.stdin is None:
        raise RuntimeError("could not open the direct video-composition pipe")

    events = {event.media_frame_index: event for event in timeline.events}
    placement_plan = build_step_placement_plan(
        timeline,
        width=width,
        height=height,
        media_sha256=plan.media_sha256,
    )
    current = timeline.events[0].frame
    placement_corner = placement_plan[timeline.events[0].media_frame_index]
    try:
        for frame_index in range(timeline.media_frame_count):
            raw = _read_exact(decoder.stdout, frame_bytes)
            if len(raw) != frame_bytes:
                raise RuntimeError(
                    f"source video ended at frame {frame_index}; timeline expects "
                    f"{timeline.media_frame_count}"
                )
            event = events.get(frame_index)
            if event is not None:
                current = event.frame
                placement_corner = placement_plan[event.media_frame_index]
            rendered = render_presentation_frame(
                Image.frombytes("RGBA", (width, height), raw),
                current,
                frame_index=frame_index,
                media_sha256=plan.media_sha256,
                placement_corner=placement_corner,
            )
            encoder.stdin.write(rendered.tobytes())
        if decoder.stdout.read(1):
            raise RuntimeError("source video has more decoded frames than its exact timeline")
        encoder.stdin.close()
        _wait_process(decoder, "presentation video decode")
        _wait_process(encoder, "presentation video encode")

        # Preserve a compatible source audio stream without decoding it. The
        # optional map also succeeds for silent recordings.
        completed = subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(encoded),
                "-i",
                str(plan.media_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a?",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-y",
                str(remuxed),
            ],
            check=False,
            capture_output=True,
            timeout=120,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"presentation audio remux failed: {detail}")
        os.replace(remuxed, output)
        if _sha256(plan.media_path) != plan.media_sha256:
            output.unlink(missing_ok=True)
            raise RuntimeError("raw recording changed during presentation export")
    except Exception:
        decoder.kill()
        encoder.kill()
        raise
    finally:
        for temporary in (encoded, remuxed):
            temporary.unlink(missing_ok=True)

    return {
        "ok": True,
        "path": str(output),
        "sha256": _sha256(output),
        "source_media_sha256": plan.media_sha256,
        "media_frame_count": timeline.media_frame_count,
        "raw_media_unchanged": True,
        "placement_policy": "step-stable-collision-aware-bottom-corner",
    }
