#!/usr/bin/env python3
"""Render a paced MP4 from exact real-RDP qualification frames.

The renderer is presentation-only. It reads the retained frame hashes and
writes derivative video frames directly to FFmpeg. It does not stage another
PNG sequence and it does not run inside the production actuation path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1280
HEIGHT = 800
FPS = 12
MAX_FRAMES_PER_PHASE = 10
PHASE_HOLD_S = 0.9
CARD_HOLD_S = 1.8
PHASE_DIRS = (
    "01-demonstration",
    "02-verified-replay",
    "03-safe-halt",
)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    raise RuntimeError("DejaVu Sans or Arial is required to render the demo")


def _write_repeated(process: subprocess.Popen, image: Image.Image, seconds: float) -> None:
    payload = image.convert("RGB").tobytes()
    for _ in range(max(1, round(FPS * seconds))):
        assert process.stdin is not None
        process.stdin.write(payload)


def _card(title: str, body: list[str], *, accent: str = "#48d6c5") -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#081012")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((72, 70, 224, 112), 20, fill=accent)
    draw.text((94, 78), "OpenAdapt", font=_font(20, bold=True), fill="#081012")
    draw.text((74, 188), title, font=_font(52, bold=True), fill="#f5fbfa")
    y = 280
    for line in body:
        draw.text((76, y), line, font=_font(27), fill="#b8c8c6")
        y += 48
    return image


def _overlay(frame: Image.Image, *, phase: str, detail: str) -> Image.Image:
    image = frame.convert("RGBA").resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    panel = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    box = (820, 632, 1248, 772)
    draw.rounded_rectangle(box, 18, fill=(7, 18, 20, 205))
    draw.text((850, 652), "OpenAdapt · RDP", font=_font(19, bold=True), fill="#48d6c5")
    draw.text((850, 687), phase, font=_font(28, bold=True), fill="#ffffff")
    draw.text((850, 728), detail, font=_font(18), fill="#c3d1cf")
    return Image.alpha_composite(image, panel).convert("RGB")


def _selected_frames(manifest: dict) -> list[dict]:
    frames = [event for event in manifest["events"] if event["kind"] == "frame"]
    if len(frames) <= MAX_FRAMES_PER_PHASE:
        return frames
    last = len(frames) - 1
    indexes = {
        round(index * last / (MAX_FRAMES_PER_PHASE - 1))
        for index in range(MAX_FRAMES_PER_PHASE)
    }
    return [frames[index] for index in sorted(indexes)]


def _read_phase(root: Path, name: str) -> tuple[dict, list[Image.Image]]:
    phase = root / name
    manifest_path = phase / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["schema_version"] != "openadapt.rdp-presentation.v1":
        raise RuntimeError(f"unsupported presentation manifest: {manifest_path}")
    images = []
    for event in _selected_frames(manifest):
        path = phase / event["file"]
        payload = path.read_bytes()
        observed = hashlib.sha256(payload).hexdigest()
        if observed != event["sha256"]:
            raise RuntimeError(f"frame hash mismatch: {path}")
        images.append(Image.open(path).convert("RGB"))
    if not images:
        raise RuntimeError(f"presentation phase has no retained frames: {phase}")
    return manifest, images


def render(presentation_dir: Path, output: Path) -> dict:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg is required to render the presentation MP4")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-y",
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgb24",
        "-video_size",
        f"{WIDTH}x{HEIGHT}",
        "-framerate",
        str(FPS),
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    phases = []
    try:
        _write_repeated(
            process,
            _card(
                "Verified execution over RDP",
                [
                    "A demonstration becomes a governed pixel workflow.",
                    "A saved effect is checked outside the remote screen.",
                    "A changed screen halts before the write.",
                ],
            ),
            CARD_HOLD_S,
        )
        phase_copy = {
            "01-demonstration": (
                "1 · Demonstrate",
                "Exact frames from the RDP client",
            ),
            "02-verified-replay": (
                "2 · Governed replay",
                "Identity and effect checks active",
            ),
            "03-safe-halt": (
                "3 · Changed screen",
                "The workflow refuses the write",
            ),
        }
        for name in PHASE_DIRS:
            manifest, frames = _read_phase(presentation_dir, name)
            label, detail = phase_copy[name]
            for frame in frames:
                _write_repeated(
                    process,
                    _overlay(frame, phase=label, detail=detail),
                    PHASE_HOLD_S,
                )
            outcome = manifest["outcome"]
            summary = manifest["summary"]
            if outcome == "VERIFIED":
                body = [
                    "The expected patient note was saved.",
                    "The independent document check matched.",
                    "The run used zero model calls.",
                ]
                accent = "#48d6c5"
            elif outcome == "HALTED":
                body = [
                    "The runtime detected a changed screen.",
                    "It stopped before the effect.",
                    "The independent check found no write.",
                ]
                accent = "#ffb84d"
            else:
                body = [
                    "The operator demonstration is complete.",
                    "OpenAdapt retained the actions and the RDP frames.",
                    "The compiler can now qualify the workflow.",
                ]
                accent = "#7ba7ff"
            _write_repeated(
                process,
                _card(outcome, body, accent=accent),
                CARD_HOLD_S,
            )
            phases.append(
                {
                    "phase": manifest["phase"],
                    "outcome": outcome,
                    "summary": summary,
                    "source_frame_count": manifest["frame_count"],
                    "presented_frame_count": len(frames),
                }
            )
        _write_repeated(
            process,
            _card(
                "OpenAdapt Execute",
                [
                    "Structured transaction in.",
                    "VERIFIED or HALTED with evidence out.",
                    "Reference fixture · synthetic data · real RDP round-trip.",
                ],
            ),
            CARD_HOLD_S,
        )
    finally:
        if process.stdin is not None:
            process.stdin.close()
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"FFmpeg failed with exit code {return_code}")
    video_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    render_manifest = {
        "schema_version": "openadapt.rdp-presentation-render.v1",
        "video": output.name,
        "video_sha256": video_hash,
        "fps": FPS,
        "frame_size": [WIDTH, HEIGHT],
        "timing": "paced presentation timing; source frames remain exact",
        "phases": phases,
    }
    manifest_path = output.with_suffix(".manifest.json")
    manifest_path.write_text(
        json.dumps(render_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return render_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--presentation-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = render(args.presentation_dir, args.output)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
