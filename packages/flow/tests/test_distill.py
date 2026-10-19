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


C = _png(10, box=(50, 30, 70, 50))


def _dropped(tmp_path: Path, steps: list[tuple[dict, bytes, bytes]], name: str):
    report = distill_recording(
        _recording(tmp_path / name, steps), tmp_path / name / "out"
    )
    return [(d.index, d.rule) for d in report.dropped]


def test_drops_a_dead_end_that_ends_where_it_started(tmp_path: Path) -> None:
    steps = [
        ({"kind": "click", "x": 1, "y": 1}, A, B),  # opens the wrong menu
        ({"kind": "key", "key": "Escape"}, B, A),  # and closes it again
        ({"kind": "click", "x": 9, "y": 9}, A, C),  # the real next step
        ({"kind": "type", "text": "hello"}, C, C),
    ]
    assert _dropped(tmp_path, steps, "a") == [(0, "dead_end"), (1, "dead_end")]


def test_dead_end_takes_the_longest_run_from_the_earliest_start(tmp_path: Path) -> None:
    steps = [
        ({"kind": "click", "x": 1, "y": 1}, A, B),
        ({"kind": "click", "x": 2, "y": 2}, B, A),  # back at A: a dead end ...
        ({"kind": "click", "x": 3, "y": 3}, A, C),
        ({"kind": "click", "x": 4, "y": 4}, C, A),  # ... that keeps going to A
        ({"kind": "click", "x": 5, "y": 5}, A, B),
    ]
    first = _dropped(tmp_path, steps, "a")
    assert first == [(i, "dead_end") for i in range(4)]
    assert _dropped(tmp_path, steps, "b") == first


@pytest.mark.parametrize(
    "inner",
    [
        {"kind": "click", "x": 2, "y": 2, "structural": {"name": "Save Encounter"}},
        {"kind": "click", "x": 2, "y": 2, "submit_binding": {"form": "f"}},
        {"kind": "click", "x": 2, "y": 2, "sor_before": [], "sor_after": [{"id": 1}]},
        {"kind": "key", "key": "Enter"},
        {"kind": "type", "text": "x"},
        {"kind": "type", "text": "x", "param": "note"},
    ],
)
def test_dead_end_never_spans_a_write_or_typing(tmp_path: Path, inner: dict) -> None:
    steps = [
        ({"kind": "click", "x": 1, "y": 1}, A, B),
        (inner, B, C),
        ({"kind": "click", "x": 3, "y": 3}, C, A),
        ({"kind": "click", "x": 9, "y": 9}, A, B),
    ]
    assert not any(rule == "dead_end" for _, rule in _dropped(tmp_path, steps, "a"))


def test_dead_end_needs_the_same_url_at_both_ends(tmp_path: Path) -> None:
    steps = [
        (
            {"kind": "click", "x": 1, "y": 1, "url_before": "/a", "url_after": "/b"},
            A,
            B,
        ),
        (
            {"kind": "click", "x": 2, "y": 2, "url_before": "/b", "url_after": "/c"},
            B,
            A,
        ),
        (
            {"kind": "click", "x": 9, "y": 9, "url_before": "/c", "url_after": "/c"},
            A,
            B,
        ),
    ]
    assert _dropped(tmp_path, steps, "a") == []


def test_dead_end_keeps_a_focus_click_for_the_next_typing(tmp_path: Path) -> None:
    steps = [
        ({"kind": "click", "x": 1, "y": 1}, A, B),
        ({"kind": "click", "x": 2, "y": 2}, B, A),  # focus for the typing below
        ({"kind": "type", "text": "hello"}, A, C),
    ]
    assert _dropped(tmp_path, steps, "a") == []


def test_a_click_that_changed_the_system_of_record_is_not_a_no_effect_step(
    tmp_path: Path,
) -> None:
    steps = [
        (
            {
                "kind": "click",
                "x": 1,
                "y": 1,
                "sor_before": [],
                "sor_after": [{"id": 1}],
            },
            A,
            A,
        ),
        ({"kind": "click", "x": 9, "y": 9}, A, B),
    ]
    assert _dropped(tmp_path, steps, "a") == []


def _edge(level: int) -> bytes:
    arr = np.full((60, 80, 3), 10, dtype=np.uint8)
    arr[50, 5:40] = level  # one repainted edge
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def test_dead_end_ignores_repaint_noise_but_not_a_real_change(tmp_path: Path) -> None:
    noisy, changed = _edge(12), _edge(40)  # two levels off; thirty levels off
    steps = [
        ({"kind": "click", "x": 1, "y": 1}, A, B),
        ({"kind": "click", "x": 2, "y": 2}, B, noisy),
        ({"kind": "click", "x": 9, "y": 9}, noisy, C),
    ]
    assert _dropped(tmp_path, steps, "a") == [(0, "dead_end"), (1, "dead_end")]
    real = [
        ({"kind": "click", "x": 1, "y": 1}, A, B),
        ({"kind": "click", "x": 2, "y": 2}, B, changed),
        ({"kind": "click", "x": 9, "y": 9}, changed, C),
    ]
    assert _dropped(tmp_path, real, "b") == []


# -- a save the app refused, then corrected (OpenAdaptAI/OpenAdapt#1328) ------
#
# Shaped like the scoreboard's OpenEMR run: the agent typed the sex into
# Gender Identity, Create was refused ("Birth Sex is not valid"), it typed the
# same value into Birth Sex and pressed Create again. Synthetic records only.

PRIOR = [{"id": "1", "fname": "Prior", "sex": "Female"}]
SAVED = PRIOR + [{"id": "2", "fname": "Testa", "sex": "Female"}]


def _ev(kind: str, *, after: list | None = None, **fields) -> dict:
    return {"kind": kind, "sor_before": PRIOR, "sor_after": after or PRIOR, **fields}


def _control_click(selector: str, name: str, role: str = "combobox") -> dict:
    return _ev(
        "click",
        x=1,
        y=1,
        structural={"selector": selector, "role": role, "name": name},
    )


def _correction(saved: list | None = None) -> list[dict]:
    return [
        _control_click("#form_fname", "form_fname", "textbox"),
        _ev("type", text="Testa", field_label="form_fname"),
        _control_click("#form_sex_identified", "form_sex_identified"),
        _ev("type", text="Female", field_label="form_sex_identified"),
        _ev("key", key="Enter"),
        _control_click("#create", "Create New Patient", "button"),
        _control_click("#form_sex", "form_sex"),
        _ev("type", text="Female", field_label="form_sex"),
        _ev("key", key="Enter"),
        _control_click("#create", "Create New Patient", "button"),
        _ev(
            "click",
            x=2,
            y=2,
            structural={"selector": "#confirmCreate", "name": "Confirm Create"},
            after=saved or SAVED,
        ),
    ]


def _steps(events: list[dict]) -> list[tuple[dict, bytes, bytes]]:
    return [(event, A, B) for event in events]


def test_drops_the_misdirected_entry_and_the_refused_save(tmp_path: Path) -> None:
    assert _dropped(tmp_path, _steps(_correction()), "a") == [
        (2, "entry_corrected"),
        (3, "entry_corrected"),
        (4, "entry_corrected"),
        (5, "save_retried"),
    ]


def test_keeps_the_first_entry_when_the_record_has_that_field(tmp_path: Path) -> None:
    saved = PRIOR + [
        {"id": "2", "fname": "Testa", "sex": "Female", "sex_identified": ""}
    ]
    assert _dropped(tmp_path, _steps(_correction(saved)), "a") == [(5, "save_retried")]


def test_keeps_the_first_entry_when_the_record_lacks_the_value(tmp_path: Path) -> None:
    saved = PRIOR + [{"id": "2", "fname": "Testa", "sex": "Male"}]
    assert _dropped(tmp_path, _steps(_correction(saved)), "a") == [(5, "save_retried")]


def test_drops_nothing_without_a_refused_save(tmp_path: Path) -> None:
    events = _correction()
    del events[5]  # no Create between the two entries
    assert _dropped(tmp_path, _steps(events), "a") == []


def test_drops_nothing_when_the_refused_save_wrote(tmp_path: Path) -> None:
    events = _correction()
    events[5] = {**events[5], "sor_after": SAVED}
    assert _dropped(tmp_path, _steps(events), "a") == []


def test_drops_nothing_when_nothing_was_typed_between_saves(tmp_path: Path) -> None:
    events = _correction()
    del events[6:9]  # the save is pressed again with no correction
    assert _dropped(tmp_path, _steps(events), "a") == []


def test_drops_nothing_without_record_snapshots(tmp_path: Path) -> None:
    events = [
        {k: v for k, v in e.items() if not k.startswith("sor_")} for e in _correction()
    ]
    assert _dropped(tmp_path, _steps(events), "a") == []


def test_keeps_a_parameter_typed_into_the_first_field(tmp_path: Path) -> None:
    events = _correction()
    events[3] = {**events[3], "param": "sex"}
    assert _dropped(tmp_path, _steps(events), "a") == [(5, "save_retried")]
