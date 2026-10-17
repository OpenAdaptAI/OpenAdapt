"""Distillation drops only steps whose frames show no effect."""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from openadapt_flow.compiler.distill import distill_recording, frames_unchanged


def _png(fill: int, *, box: tuple[int, int, int, int] | None = None) -> bytes:
    arr = np.full((60, 80, 3), fill, dtype=np.uint8)
    if box is not None:
        x0, y0, x1, y1 = box
        arr[y0:y1, x0:x1] = 255 - fill
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def _recording(tmp_path: Path, steps: list[tuple[dict, bytes, bytes]]) -> Path:
    rec = tmp_path / "rec"
    (rec / "frames").mkdir(parents=True)
    lines = []
    for i, (event, before, after) in enumerate(steps):
        (rec / "frames" / f"{i:04d}_before.png").write_bytes(before)
        (rec / "frames" / f"{i:04d}_after.png").write_bytes(after)
        lines.append(json.dumps({"i": i, **event}))
    (rec / "events.jsonl").write_text("\n".join(lines) + "\n")
    (rec / "meta.json").write_text(json.dumps({"id": "abc", "params": {}}))
    return rec


A = _png(10)
B = _png(10, box=(10, 10, 30, 30))
CARET = _png(10, box=(40, 5, 41, 25))


def test_caret_only_change_counts_as_unchanged() -> None:
    load = lambda b: np.asarray(Image.open(io.BytesIO(b)).convert("RGB"))  # noqa: E731
    assert frames_unchanged(load(A), load(CARET))
    assert not frames_unchanged(load(A), load(B))


def test_drops_no_effect_click_and_renumbers(tmp_path: Path) -> None:
    rec = _recording(
        tmp_path,
        [
            ({"kind": "click", "x": 1, "y": 1}, A, A),  # no effect, next is a click
            ({"kind": "click", "x": 5, "y": 5}, A, B),
            ({"kind": "click", "x": 5, "y": 5}, B, B),  # repeat, no effect
            ({"kind": "click", "x": 9, "y": 9}, B, A),
            ({"kind": "key", "key": "Enter"}, A, A),  # Enter is never dropped
        ],
    )
    report = distill_recording(rec, tmp_path / "out")
    assert [(d.index, d.rule) for d in report.dropped] == [
        (0, "no_visible_effect"),
        (2, "no_visible_effect"),
    ]
    events = [
        json.loads(x)
        for x in (tmp_path / "out" / "events.jsonl").read_text().splitlines()
    ]
    assert [(e["i"], e["distilled_from_i"]) for e in events] == [(0, 1), (1, 3), (2, 4)]
    assert (tmp_path / "out" / "frames" / "0000_after.png").read_bytes() == B
    assert (
        json.loads((tmp_path / "out" / "meta.json").read_text())["distilled_from"]
        == "abc"
    )
    assert not (tmp_path / "out" / "frames" / "0003_before.png").exists()


def test_keeps_focus_click_before_type_and_typing(tmp_path: Path) -> None:
    rec = _recording(
        tmp_path,
        [
            ({"kind": "click", "x": 1, "y": 1}, A, A),
            ({"kind": "type", "text": "x", "param": "fname"}, A, A),
            (
                {"kind": "click", "x": 2, "y": 2, "url_before": "a", "url_after": "b"},
                A,
                A,
            ),
            ({"kind": "click", "x": 3, "y": 3, "submit_binding": {"k": 1}}, A, A),
        ],
    )
    report = distill_recording(rec, tmp_path / "out")
    assert report.dropped == []
    assert report.steps_out == 4


def test_drops_scroll_that_is_undone(tmp_path: Path) -> None:
    rec = _recording(
        tmp_path,
        [
            ({"kind": "scroll", "dx": 0, "dy": 300}, A, B),
            ({"kind": "scroll", "dx": 0, "dy": -300}, B, A),
            ({"kind": "click", "x": 1, "y": 1}, A, B),
        ],
    )
    report = distill_recording(rec, tmp_path / "out")
    assert [d.rule for d in report.dropped] == ["scroll_undone", "scroll_undone"]
    assert report.steps_out == 1


def test_refuses_existing_output(tmp_path: Path) -> None:
    rec = _recording(tmp_path, [({"kind": "click", "x": 1, "y": 1}, A, B)])
    (tmp_path / "out").mkdir()
    with pytest.raises(FileExistsError):
        distill_recording(rec, tmp_path / "out")


def test_drops_list_selection_replaced_by_the_next_one(tmp_path: Path) -> None:
    combo = {"kind": "click", "x": 1, "y": 1, "structural": {"role": "combobox"}}
    rec = _recording(
        tmp_path,
        [
            (combo, A, B),
            ({"kind": "type", "text": "United States", "field_label": "country"}, B, A),
            ({"kind": "key", "key": "Enter"}, A, B),
            (
                {
                    "kind": "type",
                    "text": "USA",
                    "field_label": "country",
                    "param": "cc",
                },
                B,
                A,
            ),
        ],
    )
    report = distill_recording(rec, tmp_path / "out")
    assert [(d.index, d.rule) for d in report.dropped] == [
        (1, "selection_replaced"),
        (2, "selection_replaced"),
    ]


def test_text_box_typing_is_never_treated_as_replaced(tmp_path: Path) -> None:
    box = {"kind": "click", "x": 1, "y": 1, "structural": {"role": "textbox"}}
    rec = _recording(
        tmp_path,
        [
            (box, A, B),
            ({"kind": "type", "text": "27 ", "field_label": "street"}, B, A),
            ({"kind": "type", "text": "Willow", "field_label": "street"}, A, B),
        ],
    )
    assert distill_recording(rec, tmp_path / "out").dropped == []


def test_scroll_target_must_reach_its_demonstrated_position() -> None:
    from openadapt_flow.runtime.replayer import _scroll_target_positioned

    frame = _png(10)  # 80x60
    # Scrolling down: a target still 30 px lower than recorded (> 25% of 60) is not ready.
    assert not _scroll_target_positioned((40, 10), (40, 40), (0, 300), frame)
    assert _scroll_target_positioned((40, 10), (40, 20), (0, 300), frame)
    # Already past the recorded point is fine.
    assert _scroll_target_positioned((40, 40), (40, 10), (0, 300), frame)
    # Scrolling up mirrors it.
    assert not _scroll_target_positioned((40, 50), (40, 10), (0, -300), frame)
