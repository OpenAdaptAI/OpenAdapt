"""Convert a pre-2026-07-17 capture directory into the current recording.db format.

The recorder used to write a bespoke ``capture.db`` holding a single ``capture``
row and a generic ``events`` table of ``(id, timestamp, type, data JSON,
parent_id)``. PR #28 replaced it with the SQLAlchemy schema in
``openadapt_capture.db.models``, and ``CaptureSession.load`` reads only
``recording.db``. Legacy captures are therefore unloadable by current code.

The event mapping is not guesswork; ``openadapt_capture/events.py`` documents it:

    mouse.move  -> ActionEvent(name="move")
    mouse.down  -> ActionEvent(name="click",   mouse_pressed=True)
    mouse.up    -> ActionEvent(name="click",   mouse_pressed=False)
    key.down    -> ActionEvent(name="press")
    key.up      -> ActionEvent(name="release")

One thing is reconstructed rather than recovered. A legacy capture stored its
frames in ``video.mp4`` and kept only a curated subset as PNGs under
``screenshots/``; nothing recorded which ``screen.frame`` each PNG came from. So
frame timestamps are distributed evenly across the recording window. Action
timestamps are exact, and every action is attached to the screenshot nearest in
time. That is accurate enough for a demo fixture and is not evidence of
anything; do not use a migrated capture for measurement.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

NAME_BY_TYPE = {
    "mouse.move": ("move", None),
    "mouse.down": ("click", True),
    "mouse.up": ("click", False),
    "key.down": ("press", None),
    "key.up": ("release", None),
}


def _step_number(path: Path) -> int:
    match = re.search(r"step_(\d+)", path.name)
    return int(match.group(1)) if match else 0


def migrate(src: Path, dest: Path) -> dict:
    """Write ``dest/recording.db`` from the legacy capture at ``src``."""
    from openadapt_capture.db import get_session_for_path
    from openadapt_capture.db.models import ActionEvent, Base, Recording, Screenshot

    legacy = sqlite3.connect(src / "capture.db")
    legacy.row_factory = sqlite3.Row
    cap = legacy.execute("SELECT * FROM capture").fetchone()
    if cap is None:
        raise SystemExit(f"{src}: capture.db has no capture row")

    shots = sorted((src / "screenshots").glob("*step_*.png"), key=_step_number)
    if not shots:
        raise SystemExit(f"{src}: no screenshots/*step_*.png to migrate")

    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "recording.db"
    if db_path.exists():
        db_path.unlink()

    session = get_session_for_path(str(db_path))
    Base.metadata.create_all(session.get_bind())

    started = float(cap["started_at"])
    ended = float(cap["ended_at"] or started)
    recording = Recording(
        timestamp=started,
        monitor_width=cap["screen_width"],
        monitor_height=cap["screen_height"],
        pixel_ratio=cap["pixel_ratio"],
        platform=cap["platform"],
        task_description=cap["task_description"] or src.name,
        double_click_interval_seconds=cap["double_click_interval_seconds"],
        double_click_distance_pixels=cap["double_click_distance_pixels"],
        video_start_time=cap["video_start_time"],
    )
    session.add(recording)
    session.flush()

    # Even spacing across the real recording window; see the module docstring.
    span = max(ended - started, 0.001)
    stride = span / max(len(shots) - 1, 1)

    key_fields = (
        "key_name",
        "key_char",
        "key_vk",
        "canonical_key_name",
        "canonical_key_char",
        "canonical_key_vk",
    )

    rows = legacy.execute(
        "SELECT timestamp, type, data FROM events WHERE type IN (%s) ORDER BY timestamp"
        % ",".join("?" * len(NAME_BY_TYPE)),
        tuple(NAME_BY_TYPE),
    ).fetchall()

    pending = []
    dropped = 0
    for row in rows:
        payload = json.loads(row["data"])
        name, pressed = NAME_BY_TYPE[row["type"]]
        if name in ("press", "release") and not any(
            payload.get(field) not in (None, "") for field in key_fields
        ):
            # capture.py:_require_key_identity refuses a key event carrying no
            # identity at all, and it is right to. The legacy recordings hold a
            # small number of these. Drop them and say how many rather than
            # invent a key that was never pressed.
            dropped += 1
            continue
        pending.append((float(row["timestamp"]), name, pressed, payload))

    # source_ordinal is one sequence over the whole capture stream. An action
    # must carry a strictly greater ordinal than the frame it is bound to,
    # because the frame is the evidence captured before the action happened
    # (capture.py enforces this for sealed actions). So interleave frames and
    # actions by time, number them once, and bind each action to the newest
    # frame at or before it.
    timeline = [(started + i * stride, 0, i) for i in range(len(shots))]
    timeline += [(when, 1, idx) for idx, (when, *_rest) in enumerate(pending)]
    timeline.sort(key=lambda entry: (entry[0], entry[1]))

    shot_ordinal: dict[int, int] = {}
    action_ordinal: dict[int, int] = {}
    action_binding: dict[int, int] = {}
    latest_shot: int | None = None
    for ordinal, (_when, kind, index) in enumerate(timeline):
        if kind == 0:
            shot_ordinal[index] = ordinal
            latest_shot = index
        else:
            action_ordinal[index] = ordinal
            action_binding[index] = latest_shot if latest_shot is not None else 0

    screenshots = []
    for index, png in enumerate(shots):
        shot = Screenshot(
            recording_id=recording.id,
            recording_timestamp=started,
            timestamp=started + index * stride,
            source_ordinal=shot_ordinal[index],
            png_data=png.read_bytes(),
        )
        session.add(shot)
        screenshots.append(shot)
    session.flush()

    counts: dict[str, int] = {}
    for index, (when, name, pressed, payload) in enumerate(pending):
        bound = screenshots[action_binding[index]]
        session.add(
            ActionEvent(
                name=name,
                timestamp=when,
                recording_timestamp=started,
                recording_id=recording.id,
                source_ordinal=action_ordinal[index],
                screenshot_id=bound.id,
                screenshot_timestamp=bound.timestamp,
                screenshot_source_ordinal=bound.source_ordinal,
                mouse_x=payload.get("x"),
                mouse_y=payload.get("y"),
                mouse_button_name=payload.get("button"),
                mouse_pressed=pressed,
                key_name=payload.get("key_name"),
                key_char=payload.get("key_char"),
                key_vk=payload.get("key_vk"),
                canonical_key_name=payload.get("canonical_key_name"),
                canonical_key_char=payload.get("canonical_key_char"),
                canonical_key_vk=payload.get("canonical_key_vk"),
            )
        )
        counts[name] = counts.get(name, 0) + 1

    session.commit()
    session.close()
    legacy.close()
    return {
        "destination": str(db_path),
        "screenshots": len(screenshots),
        "actions": counts,
        "dropped_key_events_without_identity": dropped,
        "megabytes": round(db_path.stat().st_size / 1024 / 1024, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="legacy capture directory")
    parser.add_argument("destination", type=Path, help="directory to write recording.db into")
    args = parser.parse_args()
    if not (args.source / "capture.db").exists():
        raise SystemExit(f"{args.source}: no capture.db; nothing to migrate")
    result = migrate(args.source, args.destination)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
