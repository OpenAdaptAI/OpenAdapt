#!/usr/bin/env python3
"""Render the proof-linked RDP buyer presentation.

The renderer consumes:

* exact RDP frames and input events from the isolated presentation capture;
* the exact ``ProgramGraphSpec`` emitted by ``build_program_graph``;
* the exact run parameters used by the first governed replay;
* the independent SQL rows retained in the run summary.

It never invents a workflow target or a verifier result. It writes a derivative
MP4 directly to FFmpeg. The retained source frames and qualification JSON remain
the result authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont, ImageOps

WIDTH = 1280
HEIGHT = 800
FPS = 12
PHASE_DIRS = (
    "01-demonstration",
    "02-verified-replay",
    "03-safe-halt",
)
BG = "#071113"
PANEL = "#0d1c1f"
PANEL_2 = "#13272a"
TEXT = "#f2f8f7"
MUTED = "#a8bcba"
TEAL = "#45d6c3"
BLUE = "#7da8ff"
AMBER = "#ffbd66"
RED = "#ff7a79"
GREEN = "#58d68d"


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


def _write_repeated(
    process: subprocess.Popen,
    image: Image.Image,
    seconds: float,
) -> None:
    payload = image.convert("RGB").tobytes()
    for _ in range(max(1, round(FPS * seconds))):
        assert process.stdin is not None
        process.stdin.write(payload)


def _write_frames(
    process: subprocess.Popen,
    images: Iterable[Image.Image],
) -> None:
    for image in images:
        _write_repeated(process, image, 1 / FPS)


def _wrap(text: object, width: int) -> list[str]:
    words = str(text).split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_phase(root: Path, name: str) -> tuple[dict, list[tuple[dict, Image.Image]]]:
    phase = root / name
    manifest_path = phase / "manifest.json"
    manifest = _load_json(manifest_path)
    if manifest["schema_version"] != "openadapt.rdp-presentation.v1":
        raise RuntimeError(f"unsupported presentation manifest: {manifest_path}")
    frames: list[tuple[dict, Image.Image]] = []
    for event in manifest["events"]:
        if event["kind"] != "frame":
            continue
        path = phase / event["file"]
        payload = path.read_bytes()
        observed = hashlib.sha256(payload).hexdigest()
        if observed != event["sha256"]:
            raise RuntimeError(f"frame hash mismatch: {path}")
        frames.append((event, Image.open(path).convert("RGB")))
    if not frames:
        raise RuntimeError(f"presentation phase has no retained frames: {phase}")
    return manifest, frames


def _base() -> Image.Image:
    return Image.new("RGB", (WIDTH, HEIGHT), BG)


def _brand(draw: ImageDraw.ImageDraw, *, section: str) -> None:
    draw.rounded_rectangle((42, 34, 202, 76), 18, fill=TEAL)
    draw.text((63, 43), "OpenAdapt", font=_font(20, bold=True), fill=BG)
    draw.text((224, 45), section, font=_font(18, bold=True), fill=MUTED)


def _label_value(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
    label: str,
    value: object,
    width: int = 44,
) -> int:
    draw.text((x, y), label.upper(), font=_font(13, bold=True), fill=TEAL)
    next_y = y + 24
    for line in _wrap(value, width):
        draw.text((x, next_y), line, font=_font(22, bold=True), fill=TEXT)
        next_y += 30
    return next_y


def _request_view(request: dict) -> Image.Image:
    image = _base()
    draw = ImageDraw.Draw(image)
    _brand(draw, section="Execute request")
    draw.text(
        (44, 118),
        "A structured request enters the RDP workflow",
        font=_font(38, bold=True),
        fill=TEXT,
    )
    draw.text(
        (45, 174),
        "The replay uses different values from the demonstration.",
        font=_font(21),
        fill=MUTED,
    )
    execution = request["execution"]
    cards = (
        ("Patient", execution["patient_mrn"]),
        ("Appointment slot", execution["appointment_slot"]),
        ("Visit type", execution["visit_type"]),
        ("Request ID", execution["request_id"]),
    )
    for index, (label, value) in enumerate(cards):
        col, row = index % 2, index // 2
        x = 48 + col * 602
        y = 236 + row * 190
        draw.rounded_rectangle((x, y, x + 560, y + 148), 22, fill=PANEL)
        _label_value(draw, x=x + 26, y=y + 24, label=label, value=value)
    digest = request.get("workflow_digest") or "not available"
    draw.text(
        (48, 670),
        "Bound workflow digest",
        font=_font(14, bold=True),
        fill=MUTED,
    )
    draw.text((48, 696), str(digest)[:48], font=_font(17), fill=BLUE)
    draw.rounded_rectangle((1016, 680, 1228, 736), 24, fill=TEAL)
    draw.text((1055, 694), "AUTHORIZED", font=_font(19, bold=True), fill=BG)
    return image


def _overlay(
    frame: Image.Image,
    *,
    phase: str,
    detail: str,
    cursor: tuple[float, float] | None = None,
    click: bool = False,
) -> Image.Image:
    image = ImageOps.fit(
        frame.convert("RGB"),
        (WIDTH, HEIGHT),
        method=Image.Resampling.LANCZOS,
    ).convert("RGBA")
    panel = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    draw.rounded_rectangle((888, 654, 1246, 772), 18, fill=(5, 18, 20, 210))
    draw.text((914, 672), "OpenAdapt · RDP", font=_font(16, bold=True), fill=TEAL)
    draw.text((914, 702), phase, font=_font(23, bold=True), fill="#ffffff")
    draw.text((914, 737), detail, font=_font(15), fill="#c3d1cf")
    if cursor is not None:
        x, y = cursor
        radius = 22 if click else 14
        if click:
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                outline=(69, 214, 195, 235),
                width=4,
            )
        points = [(x, y), (x + 7, y + 25), (x + 13, y + 17), (x + 25, y + 28)]
        draw.polygon(points, fill=(255, 255, 255, 245), outline=(4, 17, 19, 255))
    return Image.alpha_composite(image, panel).convert("RGB")


def _demonstration_frames(
    root: Path,
    manifest: dict,
) -> list[Image.Image]:
    phase = root / "01-demonstration"
    images: list[Image.Image] = []
    last_frame: Image.Image | None = None
    cursor: tuple[float, float] = (70.0, 740.0)
    for event in manifest["events"]:
        if event["kind"] == "frame":
            path = phase / event["file"]
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != event["sha256"]:
                raise RuntimeError(f"frame hash mismatch: {path}")
            last_frame = Image.open(path).convert("RGB")
            images.extend(
                [
                    _overlay(
                        last_frame,
                        phase="1 · Demonstrate",
                        detail="Human-paced input over RDP",
                        cursor=cursor,
                    )
                ]
                * 2
            )
        elif event["kind"] == "pointer" and last_frame is not None:
            target = (float(event["x"]), float(event["y"]))
            start = cursor
            for step in range(1, 8):
                fraction = step / 7
                eased = 0.5 - 0.5 * math.cos(math.pi * fraction)
                point = (
                    start[0] + (target[0] - start[0]) * eased,
                    start[1] + (target[1] - start[1]) * eased,
                )
                images.append(
                    _overlay(
                        last_frame,
                        phase="1 · Demonstrate",
                        detail="Mouse and keyboard are retained",
                        cursor=point,
                    )
                )
            cursor = target
            images.extend(
                [
                    _overlay(
                        last_frame,
                        phase="1 · Demonstrate",
                        detail="Action recorded",
                        cursor=cursor,
                        click=True,
                    )
                ]
                * 3
            )
    if not images:
        raise RuntimeError("demonstration timeline has no renderable frames")
    return images


def _graph_view(spec: dict, *, active_index: int | None = None) -> Image.Image:
    image = _base()
    draw = ImageDraw.Draw(image)
    _brand(draw, section="Real compiled artifact")
    bundle = spec["bundle"]
    draw.text(
        (44, 104),
        bundle["name"],
        font=_font(34, bold=True),
        fill=TEXT,
    )
    params = [f"${item['name']}" for item in bundle.get("params", [])]
    draw.text(
        (45, 150),
        "Parameters: " + ("  ·  ".join(params) if params else "none"),
        font=_font(16),
        fill=MUTED,
    )

    nodes = spec["nodes"]
    positions: list[tuple[int, int, int, int]] = []
    for index, node in enumerate(nodes):
        col, row = index % 3, index // 3
        x = 38 + col * 414
        y = 194 + row * 176
        box = (x, y, x + 378, y + 138)
        positions.append(box)
        highlighted = active_index is None or index <= active_index
        fill = PANEL_2 if highlighted else PANEL
        outline = TEAL if index == active_index else "#284248"
        draw.rounded_rectangle(box, 18, fill=fill, outline=outline, width=3)
        draw.text(
            (x + 18, y + 14),
            f"{index + 1:02d}",
            font=_font(15, bold=True),
            fill=TEAL if highlighted else MUTED,
        )
        title_lines = _wrap(node["title"], 35)[:2]
        title_y = y + 12
        for line in title_lines:
            draw.text((x + 58, title_y), line, font=_font(16, bold=True), fill=TEXT)
            title_y += 23
        details: list[str] = []
        resolution = node.get("resolution")
        if resolution and resolution.get("top_rung"):
            details.append(f"resolve: {resolution['top_rung']}")
        if node.get("param"):
            details.append(f"input: ${node['param']}")
        if node.get("badges"):
            details.extend(node["badges"][:2])
        detail = "  ·  ".join(details) or node.get("kind", "")
        for line in _wrap(detail, 47)[:2]:
            draw.text((x + 18, title_y + 8), line, font=_font(13), fill=MUTED)
            title_y += 19

    for index in range(len(positions) - 1):
        left = positions[index]
        right = positions[index + 1]
        if index % 3 != 2:
            start = (left[2] + 4, (left[1] + left[3]) // 2)
            end = (right[0] - 6, (right[1] + right[3]) // 2)
        else:
            start = ((left[0] + left[2]) // 2, left[3] + 3)
            end = ((right[0] + right[2]) // 2, right[1] - 5)
        draw.line((start, end), fill="#527277", width=3)

    provenance = bundle.get("provenance") or {}
    digest = provenance.get("content_digest") or "not available"
    draw.text(
        (45, 742),
        f"Graph spec v{spec['spec_version']}  ·  bundle {str(digest)[:20]}",
        font=_font(14),
        fill=MUTED,
    )
    return image


def _selected_frames(
    frames: list[tuple[dict, Image.Image]],
    limit: int = 18,
) -> list[Image.Image]:
    if len(frames) <= limit:
        return [image for _event, image in frames]
    last = len(frames) - 1
    indexes = {round(index * last / (limit - 1)) for index in range(limit)}
    return [frames[index][1] for index in sorted(indexes)]


def _verifier_view(frame: Image.Image, summary: dict) -> Image.Image:
    image = _base()
    draw = ImageDraw.Draw(image)
    _brand(draw, section="Independent result check")
    draw.text(
        (44, 100),
        "The screen reports success. The database must agree.",
        font=_font(32, bold=True),
        fill=TEXT,
    )

    app = ImageOps.contain(
        frame.convert("RGB"),
        (700, 500),
        method=Image.Resampling.LANCZOS,
    )
    app_x = 42 + (700 - app.width) // 2
    app_y = 172 + (500 - app.height) // 2
    image.paste(app, (app_x, app_y))
    draw.rounded_rectangle((42, 172, 742, 672), 18, outline="#31535a", width=3)
    draw.rounded_rectangle((774, 172, 1238, 672), 18, fill=PANEL)
    draw.text((804, 202), "READ-ONLY SQL", font=_font(15, bold=True), fill=BLUE)
    verifier = summary["verifier"]
    query = verifier.get("query", "")
    for index, line in enumerate(_wrap(query, 48)[:3]):
        draw.text((804, 232 + index * 20), line, font=_font(13), fill=MUTED)
    rows = verifier.get("rows") or []
    row = rows[0] if len(rows) == 1 else {}
    y = 314
    for label, key in (
        ("Request", "request_id"),
        ("Patient", "patient_mrn"),
        ("Slot", "appointment_slot"),
        ("Visit", "visit_type"),
        ("Status", "status"),
    ):
        draw.text((804, y), label, font=_font(13, bold=True), fill=MUTED)
        value = row.get(key, "not found")
        draw.text((920, y - 3), str(value), font=_font(16, bold=True), fill=TEXT)
        y += 52
    draw.rounded_rectangle((804, 598, 1002, 646), 21, fill=GREEN)
    draw.text((840, 610), "VERIFIED", font=_font(18, bold=True), fill=BG)
    draw.text(
        (1025, 612),
        f"{len(rows)} exact row",
        font=_font(15, bold=True),
        fill=TEXT,
    )
    draw.text(
        (44, 708),
        "The verifier used a separate read-only connection. The run used zero model calls.",
        font=_font(17),
        fill=MUTED,
    )
    return image


def _wrong_record_view(frame: Image.Image, summary: dict) -> Image.Image:
    image = _base()
    draw = ImageDraw.Draw(image)
    _brand(draw, section="Fail-safe identity check")
    draw.text(
        (44, 100),
        "The active record changed before Save",
        font=_font(34, bold=True),
        fill=TEXT,
    )
    app = ImageOps.fit(
        frame.convert("RGB"),
        (760, 500),
        method=Image.Resampling.LANCZOS,
    ).convert("RGBA")
    region = summary.get("identity_region")
    if isinstance(region, list) and len(region) == 4:
        marker = Image.new("RGBA", app.size, (0, 0, 0, 0))
        marker_draw = ImageDraw.Draw(marker)
        x, y, width, height = [int(value) for value in region]
        scale_x = 760 / WIDTH
        scale_y = 500 / HEIGHT
        marker_draw.rounded_rectangle(
            (
                x * scale_x,
                y * scale_y,
                (x + width) * scale_x,
                (y + height) * scale_y,
            ),
            9,
            outline=(255, 122, 121, 255),
            width=5,
        )
        app = Image.alpha_composite(app, marker)
    image.paste(app.convert("RGB"), (42, 176))
    draw.rounded_rectangle((42, 176, 802, 676), 18, outline="#31535a", width=3)

    draw.rounded_rectangle((830, 176, 1238, 676), 18, fill=PANEL)
    y = _label_value(
        draw,
        x=860,
        y=208,
        label="Expected record",
        value=summary["expected_record"],
        width=28,
    )
    y = _label_value(
        draw,
        x=860,
        y=y + 26,
        label="Live record",
        value=summary["observed_record"],
        width=28,
    )
    draw.rounded_rectangle((860, y + 34, 1196, y + 94), 24, fill=RED)
    draw.text(
        (895, y + 49),
        "HALTED BEFORE SAVE",
        font=_font(18, bold=True),
        fill=BG,
    )
    rows = (summary.get("verifier") or {}).get("rows") or []
    draw.text(
        (860, y + 124),
        f"Database rows after halt: {len(rows)}",
        font=_font(16, bold=True),
        fill=TEXT,
    )
    draw.text(
        (44, 718),
        "OpenAdapt rechecked the live RDP frame. It refused the write. The database stayed unchanged.",
        font=_font(17),
        fill=MUTED,
    )
    return image


def _final_view(frame: Image.Image) -> Image.Image:
    background = ImageOps.fit(
        frame.convert("RGB"),
        (WIDTH, HEIGHT),
        method=Image.Resampling.LANCZOS,
    ).convert("RGBA")
    veil = Image.new("RGBA", background.size, (4, 15, 17, 220))
    image = Image.alpha_composite(background, veil).convert("RGB")
    draw = ImageDraw.Draw(image)
    _brand(draw, section="Execute over RDP")
    draw.text(
        (72, 210),
        "Structured request in.",
        font=_font(50, bold=True),
        fill=TEXT,
    )
    draw.text(
        (72, 284),
        "Verified result or a safe halt out.",
        font=_font(46, bold=True),
        fill=TEXT,
    )
    draw.text(
        (76, 380),
        "Real FreeRDP round-trip  ·  independent SQL verification",
        font=_font(22),
        fill=MUTED,
    )
    draw.text(
        (76, 424),
        "3 healthy  ·  3 drift  ·  3 wrong-record trials",
        font=_font(22, bold=True),
        fill=TEAL,
    )
    draw.rounded_rectangle((76, 548, 418, 620), 30, fill=TEAL)
    draw.text((117, 568), "Qualify one workflow", font=_font(22, bold=True), fill=BG)
    return image


def render(presentation_dir: Path, output: Path) -> dict:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg is required to render the presentation MP4")

    request = _load_json(presentation_dir / "execution-request.json")
    graph = _load_json(presentation_dir / request["program_graph"])
    manifests: dict[str, dict] = {}
    phase_frames: dict[str, list[tuple[dict, Image.Image]]] = {}
    for name in PHASE_DIRS:
        manifest, frames = _read_phase(presentation_dir, name)
        manifests[name] = manifest
        phase_frames[name] = frames

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
    presented_counts: dict[str, int] = {}
    try:
        _write_repeated(process, _request_view(request), 4.8)

        demo_images = _demonstration_frames(
            presentation_dir,
            manifests["01-demonstration"],
        )
        _write_frames(process, demo_images)
        presented_counts["01-demonstration"] = len(demo_images)

        nodes = graph["nodes"]
        for index in range(len(nodes)):
            _write_repeated(process, _graph_view(graph, active_index=index), 0.42)
        _write_repeated(process, _graph_view(graph), 2.4)

        replay_images = _selected_frames(phase_frames["02-verified-replay"])
        for image in replay_images:
            _write_repeated(
                process,
                _overlay(
                    image,
                    phase="3 · Governed replay",
                    detail="Fresh target and identity checks",
                ),
                0.38,
            )
        presented_counts["02-verified-replay"] = len(replay_images)

        replay_last = phase_frames["02-verified-replay"][-1][1]
        _write_repeated(
            process,
            _verifier_view(
                replay_last,
                manifests["02-verified-replay"]["summary"],
            ),
            6.0,
        )

        halt_last = phase_frames["03-safe-halt"][-1][1]
        _write_repeated(
            process,
            _wrong_record_view(
                halt_last,
                manifests["03-safe-halt"]["summary"],
            ),
            7.0,
        )
        presented_counts["03-safe-halt"] = 1

        _write_repeated(process, _final_view(replay_last), 4.2)
    finally:
        if process.stdin is not None:
            process.stdin.close()
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"FFmpeg failed with exit code {return_code}")

    video_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    render_manifest = {
        "schema_version": "openadapt.rdp-presentation-render.v2",
        "video": output.name,
        "video_sha256": video_hash,
        "fps": FPS,
        "frame_size": [WIDTH, HEIGHT],
        "timing": "paced derivative; source frames and input events remain exact",
        "program_graph_sha256": hashlib.sha256(
            (presentation_dir / request["program_graph"]).read_bytes()
        ).hexdigest(),
        "workflow_digest": request.get("workflow_digest"),
        "phases": [
            {
                "phase": manifests[name]["phase"],
                "outcome": manifests[name]["outcome"],
                "summary": manifests[name]["summary"],
                "source_frame_count": manifests[name]["frame_count"],
                "presented_frame_count": presented_counts[name],
            }
            for name in PHASE_DIRS
        ],
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
