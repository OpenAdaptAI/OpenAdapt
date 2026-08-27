#!/usr/bin/env python3
"""Generate the public, sealed Capture fixtures from synthetic source specs."""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import sqlite3
import struct
import tempfile
import zlib
from pathlib import Path

import sqlalchemy

from openadapt_capture.control import (
    TERMINAL_STATE_SCHEMA_VERSION,
    write_terminal_state,
)
from openadapt_capture.db import create_db
from openadapt_capture.db.models import ActionEvent, Recording, Screenshot
from openadapt_capture.desktop_capture import DesktopCaptureScope
from openadapt_capture.terminal import seal_capture

SOURCE_SCHEMA = "openadapt.capture.synthetic-fixture-source/v1"
PROVENANCE_SCHEMA = "openadapt.capture.synthetic-fixture-provenance/v1"
FIXTURE_SCHEMA = "openadapt.capture.synthetic-fixture/v1"
GENERATED_FILENAMES = {
    "capture-artifact-manifest.json",
    "capture-state.json",
    "capture-terminal.json",
    "recording.db",
    "synthetic-provenance.json",
}
VIEWPORT = (960, 540)
FRAME_INTERVAL_US = 1_000_000
BUILDER_LOCK_PATH = "uv.lock"
REQUIRED_SQLALCHEMY_VERSION = "2.0.52"
REQUIRED_SQLITE_VERSION = "3.53.1"


# This module defines the 3x5 bitmap glyphs. The renderer uses no platform font
# and writes no PNG metadata, so public frames cannot inherit a local user name,
# font path, or image profile.
GLYPHS = {
    " ": ("000", "000", "000", "000", "000"),
    "+": ("000", "010", "111", "010", "000"),
    "-": ("000", "000", "111", "000", "000"),
    ".": ("000", "000", "000", "000", "010"),
    ":": ("000", "010", "000", "010", "000"),
    "/": ("001", "001", "010", "100", "100"),
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "A": ("010", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("111", "100", "100", "100", "111"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "F": ("111", "100", "110", "100", "100"),
    "G": ("111", "100", "101", "101", "111"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "111"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("101", "111", "111", "111", "101"),
    "O": ("111", "101", "101", "101", "111"),
    "P": ("111", "101", "111", "100", "100"),
    "Q": ("111", "101", "101", "111", "001"),
    "R": ("110", "101", "110", "101", "101"),
    "S": ("111", "100", "111", "001", "111"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
}


def canonical_json_bytes(value: object) -> bytes:
    """Return the one public fixture JSON representation."""
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_exact_builder() -> None:
    """Refuse a database writer that cannot reproduce the committed bytes."""
    actual = (sqlite3.sqlite_version, sqlalchemy.__version__)
    expected = (REQUIRED_SQLITE_VERSION, REQUIRED_SQLALCHEMY_VERSION)
    if actual != expected:
        raise RuntimeError(
            "synthetic capture generation requires "
            f"SQLite {expected[0]} and SQLAlchemy {expected[1]}; got "
            f"SQLite {actual[0]} and SQLAlchemy {actual[1]}"
        )


class Canvas:
    """Small deterministic RGB canvas for source-owned fixture frames."""

    def __init__(self, width: int, height: int, color: tuple[int, int, int]) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray(color * (width * height))

    def rectangle(
        self,
        left: int,
        top: int,
        right: int,
        bottom: int,
        color: tuple[int, int, int],
    ) -> None:
        left = max(0, min(self.width, left))
        right = max(left, min(self.width, right))
        top = max(0, min(self.height, top))
        bottom = max(top, min(self.height, bottom))
        row = bytes(color) * (right - left)
        for y in range(top, bottom):
            offset = (y * self.width + left) * 3
            self.pixels[offset : offset + len(row)] = row

    def outline(
        self,
        left: int,
        top: int,
        right: int,
        bottom: int,
        color: tuple[int, int, int],
        width: int = 2,
    ) -> None:
        self.rectangle(left, top, right, top + width, color)
        self.rectangle(left, bottom - width, right, bottom, color)
        self.rectangle(left, top, left + width, bottom, color)
        self.rectangle(right - width, top, right, bottom, color)

    def text(
        self,
        left: int,
        top: int,
        value: str,
        color: tuple[int, int, int],
        scale: int = 3,
    ) -> None:
        cursor = left
        for character in value.upper():
            glyph = GLYPHS.get(character, GLYPHS[" "])
            for row_index, row in enumerate(glyph):
                for column_index, bit in enumerate(row):
                    if bit == "1":
                        self.rectangle(
                            cursor + column_index * scale,
                            top + row_index * scale,
                            cursor + (column_index + 1) * scale,
                            top + (row_index + 1) * scale,
                            color,
                        )
            cursor += 4 * scale

    def png(self) -> bytes:
        raw = bytearray()
        row_bytes = self.width * 3
        for row_index in range(self.height):
            start = row_index * row_bytes
            raw.append(0)
            raw.extend(self.pixels[start : start + row_bytes])
        compressor = zlib.compressobj(
            level=9,
            method=zlib.DEFLATED,
            wbits=15,
            memLevel=9,
            strategy=zlib.Z_FIXED,
        )
        compressed = compressor.compress(bytes(raw)) + compressor.flush()
        return (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", compressed)
            + _png_chunk(b"IEND", b"")
        )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind)
    checksum = binascii.crc32(payload, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def _draw_shell(canvas: Canvas, *, fixture_id: str, caption: str, step: int, total: int) -> None:
    canvas.rectangle(0, 0, 960, 540, (18, 26, 43))
    canvas.rectangle(0, 0, 960, 48, (12, 18, 31))
    canvas.text(24, 16, "OPENADAPT SYNTHETIC WORKSPACE", (102, 226, 196), 3)
    canvas.text(705, 16, f"FRAME {step:02d}/{total:02d}", (203, 213, 225), 3)
    canvas.rectangle(24, 72, 196, 500, (25, 36, 57))
    canvas.text(44, 96, "PUBLIC FIXTURE", (148, 163, 184), 3)
    canvas.rectangle(44, 132, 176, 134, (54, 68, 92))
    canvas.text(44, 158, fixture_id.replace("_", " "), (241, 245, 249), 3)
    canvas.text(44, 438, "NO USER DATA", (102, 226, 196), 3)
    canvas.text(44, 466, "SOURCE GENERATED", (148, 163, 184), 2)
    canvas.rectangle(220, 72, 936, 500, (241, 245, 249))
    canvas.rectangle(220, 72, 936, 112, (226, 232, 240))
    canvas.text(244, 86, caption, (30, 41, 59), 3)


def _draw_calculator(canvas: Canvas, frame: dict, index: int, total: int) -> None:
    _draw_shell(
        canvas,
        fixture_id="demo_new",
        caption=str(frame["caption"]),
        step=index + 1,
        total=total,
    )
    canvas.rectangle(360, 138, 700, 466, (30, 41, 59))
    canvas.outline(360, 138, 700, 466, (71, 85, 105), 3)
    canvas.text(388, 158, "CALCULATOR", (148, 163, 184), 3)
    canvas.rectangle(388, 194, 672, 250, (12, 18, 31))
    display = str(frame.get("display", "0"))
    canvas.text(620 - len(display) * 18, 214, display, (241, 245, 249), 4)
    labels = ("7", "8", "9", "/", "4", "5", "6", "X", "1", "2", "3", "-", "0", ".", "=", "+")
    for button_index, label in enumerate(labels):
        row, column = divmod(button_index, 4)
        left = 388 + column * 72
        top = 272 + row * 42
        active = label == str(frame.get("active", ""))
        color = (45, 212, 191) if active else (54, 68, 92)
        canvas.rectangle(left, top, left + 56, top + 30, color)
        canvas.text(left + 22, top + 7, label, (12, 18, 31) if active else (241, 245, 249), 3)
    canvas.text(744, 160, "DECLARED TIMELINE", (71, 85, 105), 2)
    canvas.text(744, 190, "EXACT BINDINGS", (71, 85, 105), 2)
    canvas.text(744, 220, "SEALED ARTIFACTS", (71, 85, 105), 2)


def _draw_display_settings(canvas: Canvas, frame: dict, index: int, total: int) -> None:
    _draw_shell(
        canvas,
        fixture_id="turn-off-nightshift",
        caption=str(frame["caption"]),
        step=index + 1,
        total=total,
    )
    canvas.text(254, 138, "DISPLAY SETTINGS", (30, 41, 59), 4)
    canvas.rectangle(254, 184, 902, 254, (226, 232, 240))
    canvas.text(278, 206, "DISPLAY ONE", (54, 68, 92), 3)
    canvas.rectangle(748, 202, 868, 232, (102, 226, 196))
    canvas.text(772, 210, "ACTIVE", (12, 18, 31), 2)
    canvas.rectangle(254, 276, 902, 404, (255, 255, 255))
    canvas.outline(254, 276, 902, 404, (203, 213, 225), 2)
    canvas.text(278, 300, "WARM DISPLAY", (30, 41, 59), 3)
    canvas.text(278, 330, "SCHEDULED COLOR ADJUSTMENT", (100, 116, 139), 2)
    enabled = bool(frame.get("enabled", True))
    track = (45, 212, 191) if enabled else (148, 163, 184)
    canvas.rectangle(798, 298, 866, 326, track)
    knob_left = 840 if enabled else 802
    canvas.rectangle(knob_left, 302, knob_left + 20, 322, (255, 255, 255))
    canvas.text(278, 368, "STATUS", (100, 116, 139), 2)
    canvas.text(368, 366, "ON" if enabled else "OFF", (13, 148, 136), 3)
    progress = int(frame.get("progress", index + 1))
    canvas.rectangle(254, 432, 902, 438, (203, 213, 225))
    canvas.rectangle(254, 432, 254 + int(648 * progress / total), 438, (45, 212, 191))


def render_frame(spec: dict, frame: dict, index: int) -> bytes:
    canvas = Canvas(*VIEWPORT, (18, 26, 43))
    if spec["theme"] == "calculator":
        _draw_calculator(canvas, frame, index, len(spec["frames"]))
    elif spec["theme"] == "display-settings":
        _draw_display_settings(canvas, frame, index, len(spec["frames"]))
    else:  # The spec validator makes this unreachable.
        raise ValueError(f"unknown synthetic fixture theme: {spec['theme']}")
    return canvas.png()


def load_spec(path: Path) -> tuple[dict, bytes]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid JSON") from exc
    if raw != canonical_json_bytes(value):
        raise ValueError(f"{path}: source spec is not canonical JSON plus one LF")
    expected_keys = {
        "actions",
        "fixture_id",
        "frames",
        "schema_version",
        "start_timestamp_us",
        "task_description",
        "theme",
    }
    if set(value) != expected_keys or value.get("schema_version") != SOURCE_SCHEMA:
        raise ValueError(f"{path}: source spec has an unsupported shape")
    if value["theme"] not in {"calculator", "display-settings"}:
        raise ValueError(f"{path}: unknown theme")
    frames = value["frames"]
    actions = value["actions"]
    if not isinstance(frames, list) or len(frames) < 2 or len(actions) != len(frames):
        raise ValueError(f"{path}: a fixture needs N frames and N actions")
    for frame in frames:
        allowed = {"active", "caption", "display", "enabled", "progress"}
        if not isinstance(frame, dict) or not set(frame).issubset(allowed) or "caption" not in frame:
            raise ValueError(f"{path}: frame has an unsupported shape")
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"label", "x", "y"}:
            raise ValueError(f"{path}: action has an unsupported shape")
        if not all(isinstance(action[field], int) for field in ("x", "y")):
            raise ValueError(f"{path}: action coordinates must be integers")
        if not (0 <= action["x"] < VIEWPORT[0] and 0 <= action["y"] < VIEWPORT[1]):
            raise ValueError(f"{path}: action coordinates are outside the fixture")
    return value, raw


def _timeline(spec: dict) -> tuple[list[dict], list[dict]]:
    """Derive one exact 1-based journal and complete before/after bindings."""
    frames = []
    actions = []
    start_us = int(spec["start_timestamp_us"])
    for index, frame in enumerate(spec["frames"]):
        frames.append(
            {
                "index": index,
                "offset_us": index * FRAME_INTERVAL_US,
                "timestamp_us": start_us + index * FRAME_INTERVAL_US,
                "frame": frame,
            }
        )
    for index, action in enumerate(spec["actions"]):
        if index == 0:
            before_index, after_index, offset_us = 0, 1, 250_000
        elif index == 1:
            before_index, after_index, offset_us = 0, 1, 750_000
        else:
            before_index, after_index = index - 1, index
            offset_us = before_index * FRAME_INTERVAL_US + 500_000
        actions.append(
            {
                "index": index,
                "offset_us": offset_us,
                "timestamp_us": start_us + offset_us,
                "before_frame_index": before_index,
                "after_frame_index": after_index,
                "action": action,
            }
        )
    journal = [*(dict(item, kind="frame") for item in frames), *(dict(item, kind="action") for item in actions)]
    journal.sort(key=lambda item: (item["timestamp_us"], item["kind"], item["index"]))
    for source_ordinal, item in enumerate(journal, start=1):
        item["source_ordinal"] = source_ordinal
    frame_ordinals = {item["index"]: item["source_ordinal"] for item in journal if item["kind"] == "frame"}
    for item in frames:
        item["source_ordinal"] = frame_ordinals[item["index"]]
    for item in actions:
        matching = next(
            row for row in journal if row["kind"] == "action" and row["index"] == item["index"]
        )
        item["source_ordinal"] = matching["source_ordinal"]
        item["before_frame_source_ordinal"] = frame_ordinals[item["before_frame_index"]]
        item["after_frame_source_ordinal"] = frame_ordinals[item["after_frame_index"]]
    return frames, actions


def _remove_previous_generated_files(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    unexpected = {path.name for path in destination.iterdir()} - GENERATED_FILENAMES
    if unexpected:
        raise RuntimeError(
            f"{destination}: refusing to replace a directory with unknown files: {sorted(unexpected)}"
        )
    for name in GENERATED_FILENAMES:
        path = destination / name
        if path.exists() or path.is_symlink():
            path.unlink()


def _normalize_database(path: Path) -> None:
    """Compact the fixture and remove build-environment header variation."""
    database = sqlite3.connect(path)
    try:
        database.execute("PRAGMA journal_mode=DELETE")
        database.execute("PRAGMA page_size=4096")
        database.execute("PRAGMA auto_vacuum=NONE")
        database.execute("VACUUM")
    finally:
        database.close()
    raw = bytearray(path.read_bytes())
    # SQLite bytes 96..99 record the writer library version. They do not affect
    # the database contract, but they differ between otherwise identical Python
    # runtimes. Use zero for a source-generated fixture so exact regeneration
    # does not claim a platform-specific writer identity.
    raw[96:100] = b"\0\0\0\0"
    path.write_bytes(raw)


def generate_fixture(
    spec_path: Path,
    destination: Path,
    *,
    generator_path: Path,
) -> None:
    spec, spec_raw = load_spec(spec_path)
    _remove_previous_generated_files(destination)
    source_sha256 = sha256_bytes(spec_raw)
    generator_sha256 = sha256_file(generator_path)
    builder_lock = generator_path.parents[1] / BUILDER_LOCK_PATH
    if not builder_lock.is_file():
        raise RuntimeError(f"the tracked builder lock is missing: {builder_lock}")
    frames, actions = _timeline(spec)
    start_us = int(spec["start_timestamp_us"])
    started_at = start_us / 1_000_000

    provenance = {
        "schema_version": PROVENANCE_SCHEMA,
        "fixture_id": spec["fixture_id"],
        "synthetic": True,
        "contains_human_recording": False,
        "contains_personal_data": False,
        "qualification_eligible": False,
        "purpose": "public_documentation",
        "source": {
            "path": f"examples/captures/specs/{spec_path.name}",
            "sha256": source_sha256,
        },
        "generator": {
            "path": "scripts/generate_synthetic_captures.py",
            "sha256": generator_sha256,
        },
        "builder": {
            "sqlite_version": REQUIRED_SQLITE_VERSION,
            "sqlalchemy_version": REQUIRED_SQLALCHEMY_VERSION,
            "database_normalization": "vacuum_4096_writer_version_zero",
            "lock_path": BUILDER_LOCK_PATH,
            "lock_sha256": sha256_file(builder_lock),
        },
        "timing": {
            "kind": "declared_synthetic",
            "start_timestamp_us": start_us,
            "frame_interval_us": FRAME_INTERVAL_US,
            "frame_timestamps_us": [frame["timestamp_us"] for frame in frames],
        },
        "action_bindings": [
            {
                "source_ordinal": action["source_ordinal"],
                "timestamp_us": action["timestamp_us"],
                "before_frame_source_ordinal": action["before_frame_source_ordinal"],
                "after_frame_source_ordinal": action["after_frame_source_ordinal"],
                "label": action["action"]["label"],
            }
            for action in actions
        ],
    }

    scope = DesktopCaptureScope.from_monitors(
        [
            {"left": 0, "top": 0, "width": VIEWPORT[0], "height": VIEWPORT[1]},
            {"left": 0, "top": 0, "width": VIEWPORT[0], "height": VIEWPORT[1]},
        ]
    )
    config = {
        "capture_desktop": scope.snapshot(),
        "fixture": {
            "schema_version": FIXTURE_SCHEMA,
            "synthetic": True,
            "contains_human_recording": False,
            "contains_personal_data": False,
            "qualification_eligible": False,
            "purpose": "public_documentation",
            "provenance_path": "synthetic-provenance.json",
            "source_spec_sha256": source_sha256,
            "provenance": provenance,
        },
    }

    engine, session_factory = create_db(str(destination / "recording.db"))
    session = session_factory()
    try:
        recording = Recording(
            timestamp=started_at,
            monitor_width=VIEWPORT[0],
            monitor_height=VIEWPORT[1],
            pixel_ratio=1.0,
            platform="synthetic",
            task_description=str(spec["task_description"]),
            double_click_interval_seconds=0.5,
            double_click_distance_pixels=5.0,
            config=config,
        )
        session.add(recording)
        session.flush()

        stored_frames: dict[int, Screenshot] = {}
        for frame in frames:
            png = render_frame(spec, frame["frame"], frame["index"])
            row = Screenshot(
                recording_id=recording.id,
                recording_timestamp=started_at,
                timestamp=frame["timestamp_us"] / 1_000_000,
                source_ordinal=frame["source_ordinal"],
                png_data=png,
                png_sha256=sha256_bytes(png),
            )
            session.add(row)
            stored_frames[frame["index"]] = row
        session.flush()

        for action in actions:
            before = stored_frames[action["before_frame_index"]]
            after = stored_frames[action["after_frame_index"]]
            details = action["action"]
            session.add(
                ActionEvent(
                    recording_id=recording.id,
                    recording_timestamp=started_at,
                    timestamp=action["timestamp_us"] / 1_000_000,
                    source_ordinal=action["source_ordinal"],
                    name="click",
                    mouse_x=details["x"],
                    mouse_y=details["y"],
                    mouse_button_name="left",
                    mouse_pressed=False,
                    active_segment_description=details["label"],
                    screenshot=before,
                    screenshot_timestamp=before.timestamp,
                    screenshot_source_ordinal=action["before_frame_source_ordinal"],
                    after_screenshot_timestamp=after.timestamp,
                    after_screenshot_source_ordinal=action["after_frame_source_ordinal"],
                )
            )
        session.commit()
    finally:
        session.close()
        engine.dispose()
    _normalize_database(destination / "recording.db")

    (destination / "synthetic-provenance.json").write_bytes(canonical_json_bytes(provenance))

    finalized_at = (start_us + (len(frames) - 1) * FRAME_INTERVAL_US) / 1_000_000
    session_id = f"openadapt-public-synthetic-{spec['fixture_id']}-v1"
    write_terminal_state(
        destination,
        {
            "schema_version": TERMINAL_STATE_SCHEMA_VERSION,
            "session_id": session_id,
            "pid": 4242,
            "process_started_at": started_at - 1.0,
            "phase": "complete",
            "ready": True,
            "complete": True,
            "integrity_verified": True,
            "error_code": None,
            "started_at": started_at,
            "finalized_at": finalized_at,
            "event_counts": {
                "action": len(actions),
                "screen": len(frames),
                "window": 0,
                "browser": 0,
                "video": 0,
            },
        },
    )
    seal_capture(
        destination,
        session_id=session_id,
        process_started_at=started_at - 1.0,
        capture_started_at=started_at,
        capture_ended_at=finalized_at,
        event_counts={
            "action": len(actions),
            "screen": len(frames),
            "window": 0,
            "browser": 0,
            "video": 0,
        },
        last_source_ordinal=len(frames) + len(actions),
    )


def generate_all(*, spec_root: Path, output_root: Path, generator_path: Path) -> None:
    require_exact_builder()
    spec_paths = sorted(spec_root.glob("*.json"))
    if {path.stem for path in spec_paths} != {"demo_new", "turn-off-nightshift"}:
        raise RuntimeError("the public fixture source set must contain the two named specs")
    for spec_path in spec_paths:
        generate_fixture(
            spec_path,
            output_root / spec_path.stem,
            generator_path=generator_path,
        )


def check_generated(*, spec_root: Path, output_root: Path, generator_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="openadapt-synthetic-captures-") as temporary:
        generated_root = Path(temporary)
        generate_all(
            spec_root=spec_root,
            output_root=generated_root,
            generator_path=generator_path,
        )
        for fixture_id in ("demo_new", "turn-off-nightshift"):
            expected = output_root / fixture_id
            generated = generated_root / fixture_id
            expected_files = {path.name for path in expected.iterdir()}
            generated_files = {path.name for path in generated.iterdir()}
            if expected_files != generated_files:
                raise RuntimeError(
                    f"{fixture_id}: generated files differ: "
                    f"expected {sorted(expected_files)}, got {sorted(generated_files)}"
                )
            for name in sorted(expected_files):
                if (expected / name).read_bytes() != (generated / name).read_bytes():
                    raise RuntimeError(f"{fixture_id}/{name}: generated bytes are stale")


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spec-root",
        type=Path,
        default=repository_root / "examples" / "captures" / "specs",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=repository_root / "examples" / "captures",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generator_path = Path(__file__).resolve()
    if args.check:
        check_generated(
            spec_root=args.spec_root.resolve(),
            output_root=args.output_root.resolve(),
            generator_path=generator_path,
        )
    else:
        generate_all(
            spec_root=args.spec_root.resolve(),
            output_root=args.output_root.resolve(),
            generator_path=generator_path,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
