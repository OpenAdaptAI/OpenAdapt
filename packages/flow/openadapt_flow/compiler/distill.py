"""Distill a recording: drop demonstration steps that had no effect.

An agent demonstration (run 1 of ``openadapt-agent learn``) carries steps a
person would not record: a click that changed nothing, a scroll that was
undone by the next scroll, a repeated click on a control that was already
active. Compiling those steps makes every later run slower and gives the
resolver more places to halt.

:func:`distill_recording` writes a new recording directory with those steps
removed and the frames renumbered, plus ``distill.json`` listing each dropped
step and the rule that dropped it. The source recording is not changed.

The rules are conservative. A step is dropped only when its own before and
after frames show it had no visible effect, and never when:

* it types text or carries a parameter (typing is never redundant here);
* it is a key press of Enter, Tab or Escape (they can act without a visible
  change on the frame that was captured);
* the next step types or presses a key (a click with no visible change can
  still move keyboard focus to the field the next step types into);
* its URL, title or page count changed (``*_before`` / ``*_after`` keys);
* it carries a ``submit_binding`` (the compiler treats it as a submit).

One more rule uses structure when the recording has it: text typed into a
list box (role ``combobox``), and the Enter after it, is dropped when the
next step types into the same list again. Only literal text is dropped this
way, never a parameter.

The last rule drops a dead end: a run of two or more steps that ends where it
started. An agent opens the wrong menu and presses Escape, or clicks into a
field and then clicks away. The run is dropped only when all of these hold:

* the frame after its last step matches the frame before its first step,
  apart from a caret and repaint noise of up to 2 levels per channel (a
  focus glow redrawn one level off on an unchanged screen);
* the URL, title, page count and system-of-record snapshot (when recorded)
  are the same at both ends, and no step in the run changed the
  system-of-record snapshot;
* every step is a click, double-click, right-click, scroll, key or hotkey,
  with no parameter, no ``submit_binding``, no Enter or Tab, and no target
  whose label names a write (:func:`openadapt_flow.risk.is_write_shaped`);
* the run does not end on a click that gives focus to the next step's typing.

Typing never takes part in a dead end. The longest qualifying run from the
earliest start wins, and the scan resumes after it, so the result does not
depend on anything but the recording.

The correction rule needs the system-of-record snapshots. An agent types a
value into the wrong field, presses Save, the app refuses (validation), the
agent types the same value into the right field and presses Save again. The
refused save is dropped when all of these hold:

* it is a click on a control whose label names a write, the
  system-of-record snapshot did not change across it, and its URL, title and
  page count stayed the same;
* a later click hits the same control (same selector, or same accessible
  name when there is no selector), nothing was written in between, and at
  least one step in between types text.

Inside such a correction, a literal value typed before the refused save is
dropped when the same literal is typed into a different field between the
two saves, and the record the run finally saved holds that value under the
second field's name but has no field named like the first. The click that
focused the first field and an Enter that closed it go with it. When any
condition fails, nothing is dropped.

"Visible effect" means at least one pixel changed outside a caret-sized
column (width <= 2 px, height <= 40 px). A blinking caret is not an effect.
"""

from __future__ import annotations

import io
import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PIL import Image

__all__ = [
    "DistillReport",
    "DroppedStep",
    "distill_recording",
    "field_key",
    "frames_unchanged",
]

_DROPPABLE_KINDS = {"click", "double_click", "scroll", "key", "hotkey"}
_ACTING_KEYS = {"enter", "return", "tab", "escape", "esc", "kp_enter"}
_FOCUS_CONSUMERS = {"type", "key", "hotkey"}
_STRUCTURAL_KEYS = ("url", "title", "pages", "sor")
_DEAD_END_KINDS = {"click", "double_click", "right_click", "scroll", "key", "hotkey"}
_DEAD_END_BLOCKING_KEYS = {"enter", "return", "kp_enter", "tab"}
_CARET_MAX_W = 2
_CARET_MAX_H = 40
#: Per-channel difference a browser repaint leaves on an unchanged screen
#: (a focus glow redrawn one level off). Used only to close a dead end.
_REPAINT_NOISE = 2


@dataclass
class DroppedStep:
    """One step removed from the recording."""

    index: int
    kind: str
    rule: str


@dataclass
class DistillReport:
    """What :func:`distill_recording` kept and removed."""

    source: str
    output: str
    steps_in: int
    steps_out: int
    dropped: list[DroppedStep] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pixels(path: Path) -> Optional[np.ndarray]:
    if not path.exists():
        return None
    with Image.open(io.BytesIO(path.read_bytes())) as image:
        return np.asarray(image.convert("RGB"))


def frames_unchanged(
    before: Optional[np.ndarray],
    after: Optional[np.ndarray],
    *,
    tolerance: int = 0,
) -> bool:
    """True when the two frames differ by at most a caret-sized column.

    ``tolerance`` ignores per-channel differences up to that many levels.
    """

    if before is None or after is None or before.shape != after.shape:
        return False
    if tolerance:
        delta = np.abs(before.astype(np.int16) - after.astype(np.int16))
        diff = np.any(delta > tolerance, axis=2)
    else:
        diff = np.any(before != after, axis=2)
    if not diff.any():
        return True
    ys, xs = np.nonzero(diff)
    width = int(xs.max() - xs.min() + 1)
    height = int(ys.max() - ys.min() + 1)
    return width <= _CARET_MAX_W and height <= _CARET_MAX_H


def _structure_changed(event: dict[str, Any]) -> bool:
    for key in _STRUCTURAL_KEYS:
        b, a = f"{key}_before", f"{key}_after"
        if (b in event or a in event) and event.get(b) != event.get(a):
            return True
    return False


def _keeps_because(
    event: dict[str, Any], nxt: Optional[dict[str, Any]]
) -> Optional[str]:
    kind = event.get("kind")
    if kind not in _DROPPABLE_KINDS:
        return "kind"
    if event.get("param") or event.get("secret"):
        return "param"
    if kind == "key" and str(event.get("key", "")).lower() in _ACTING_KEYS:
        return "acting_key"
    if kind == "hotkey" and str(event.get("key", "")).lower() in _ACTING_KEYS:
        return "acting_key"
    if event.get("submit_binding"):
        return "submit_binding"
    if _structure_changed(event):
        return "structure_changed"
    if (
        kind in ("click", "double_click")
        and nxt is not None
        and nxt.get("kind") in _FOCUS_CONSUMERS
    ):
        return "focus_for_next"
    return None


def _target_labels(event: dict[str, Any]) -> list[str]:
    structural = event.get("structural") or {}
    labels = [structural.get("name"), structural.get("selector")]
    labels.append(event.get("structured_identity"))
    return [str(label) for label in labels if label]


def _dead_end_member(event: dict[str, Any]) -> bool:
    """True when ``event`` may sit inside a dead end (see the module doc)."""

    from openadapt_flow.risk import is_write_shaped

    if event.get("kind") not in _DEAD_END_KINDS:
        return False
    if event.get("param") or event.get("secret") or event.get("submit_binding"):
        return False
    if str(event.get("key", "")).lower() in _DEAD_END_BLOCKING_KEYS:
        return False
    if ("sor_before" in event or "sor_after" in event) and event.get(
        "sor_before"
    ) != event.get("sor_after"):
        return False
    return not any(is_write_shaped(label) for label in _target_labels(event))


def _same_structure(first: dict[str, Any], last: dict[str, Any]) -> bool:
    for key in _STRUCTURAL_KEYS:
        b, a = f"{key}_before", f"{key}_after"
        if (b in first or a in last) and first.get(b) != last.get(a):
            return False
    return True


def field_key(label: Optional[str]) -> str:
    """A field or record-field name reduced for an exact comparison.

    ``#form_sex``, ``form_sex`` and ``sex`` all give ``sex``.
    ``form_sex_identified`` gives ``sexidentified``, which never equals
    ``sex``: names are compared whole, never by substring.
    """

    text = str(label or "").strip().lower().lstrip("#")
    text = re.sub(r"^form[_\-\s]+", "", text)
    return re.sub(r"[^0-9a-z]", "", text)


def _sor_unchanged(event: dict[str, Any]) -> bool:
    """True when the event recorded both snapshots and they are equal."""

    return (
        "sor_before" in event
        and "sor_after" in event
        and event["sor_before"] == event["sor_after"]
    )


def _control(event: dict[str, Any]) -> Optional[tuple[Any, ...]]:
    structural = event.get("structural") or {}
    frame = tuple(structural.get("frame_path") or ())
    if structural.get("selector"):
        return (frame, "selector", structural["selector"])
    if structural.get("name"):
        return (frame, "name", structural["name"])
    return None


def _refused_save(event: dict[str, Any]) -> bool:
    """A click on a write-labelled control that wrote nothing and went nowhere."""

    from openadapt_flow.risk import is_write_shaped

    if event.get("kind") != "click" or event.get("param") or event.get("secret"):
        return False
    if not _sor_unchanged(event) or _structure_changed(event):
        return False
    if _control(event) is None:
        return False
    return any(is_write_shaped(label) for label in _target_labels(event))


def _literal_type(event: dict[str, Any]) -> bool:
    return (
        event.get("kind") == "type"
        and not event.get("param")
        and not event.get("secret")
        and bool(event.get("text"))
        and bool(field_key(event.get("field_label")))
        and _sor_unchanged(event)
    )


def _saved_rows(events: list[dict[str, Any]]) -> Optional[list[dict[str, Any]]]:
    """Rows the run added or changed, from its first and last snapshots."""

    if not events:
        return None
    first = events[0].get("sor_before")
    last = events[-1].get("sor_after")
    if not isinstance(first, list) or not isinstance(last, list):
        return None
    return [row for row in last if isinstance(row, dict) and row not in first]


def _record_moved(
    saved: list[dict[str, Any]], value: str, from_field: str, to_field: str
) -> bool:
    """True when the saved record holds ``value`` under ``to_field`` and has
    no field named ``from_field`` at all."""

    columns = {field_key(key) for row in saved for key in row}
    holders = {
        field_key(key)
        for row in saved
        for key, held in row.items()
        if held is not None and str(held) == value
    }
    return to_field in holders and from_field not in columns


def _focuses(event: dict[str, Any], field: str) -> bool:
    structural = event.get("structural") or {}
    return event.get("kind") == "click" and field in {
        field_key(structural.get("selector")),
        field_key(structural.get("name")),
    }


def _corrections(events: list[dict[str, Any]], dropped: dict[int, str]) -> None:
    """A save the app refused, corrected, then pressed again (module doc)."""

    saved = _saved_rows(events)
    if not saved:
        return
    for s1, first in enumerate(events):
        if s1 in dropped or not _refused_save(first):
            continue
        s2 = next(
            (
                j
                for j in range(s1 + 1, len(events))
                if events[j].get("kind") == "click"
                and _control(events[j]) == _control(first)
            ),
            None,
        )
        if s2 is None:
            continue
        if not all(_sor_unchanged(event) for event in events[:s2]):
            continue
        if not any(event.get("kind") == "type" for event in events[s1 + 1 : s2]):
            continue
        dropped[s1] = "save_retried"
        for t2 in range(s1 + 1, s2):
            later = events[t2]
            if not _literal_type(later):
                continue
            value = str(later["text"])
            to_field = field_key(later.get("field_label"))
            for t1 in range(s1):
                early = events[t1]
                if t1 in dropped or not _literal_type(early) or early["text"] != value:
                    continue
                from_field = field_key(early.get("field_label"))
                if from_field == to_field:
                    continue
                if any(
                    event.get("kind") == "type"
                    and field_key(event.get("field_label")) == from_field
                    for event in events[t1 + 1 :]
                ):
                    continue
                if not _record_moved(saved, value, from_field, to_field):
                    continue
                dropped[t1] = "entry_corrected"
                focus = t1 - 1
                if (
                    focus >= 0
                    and focus not in dropped
                    and _focuses(events[focus], from_field)
                    and not _structure_changed(events[focus])
                ):
                    dropped[focus] = "entry_corrected"
                enter = t1 + 1
                if (
                    enter < s1
                    and events[enter].get("kind") == "key"
                    and str(events[enter].get("key", "")).lower() in ("enter", "return")
                    and not _structure_changed(events[enter])
                ):
                    dropped[enter] = "entry_corrected"


def _load(recording_dir: Path) -> list[dict[str, Any]]:
    lines = (recording_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def distill_recording(recording_dir: Path | str, out_dir: Path | str) -> DistillReport:
    """Write a copy of ``recording_dir`` without no-effect steps.

    Args:
        recording_dir: A Recorder output directory (``events.jsonl``,
            ``frames/``, ``meta.json``).
        out_dir: Where to write the distilled recording. Must not exist.

    Returns:
        The report, also written to ``out_dir/distill.json``.
    """

    src = Path(recording_dir)
    dst = Path(out_dir)
    if dst.exists():
        raise FileExistsError(dst)
    events = _load(src)
    frames = src / "frames"
    dropped: dict[int, str] = {}

    def frame(i: int, side: str) -> Optional[np.ndarray]:
        return _pixels(frames / f"{i:04d}_{side}.png")

    # Rule 1: a scroll undone by the next scroll, with the screen back where it started.
    for pos in range(len(events) - 1):
        a, b = events[pos], events[pos + 1]
        if pos in dropped or a.get("kind") != "scroll" or b.get("kind") != "scroll":
            continue
        if int(a.get("dx", 0)) + int(b.get("dx", 0)) or int(a.get("dy", 0)) + int(
            b.get("dy", 0)
        ):
            continue
        if frames_unchanged(frame(a["i"], "before"), frame(b["i"], "after")):
            dropped[pos] = dropped[pos + 1] = "scroll_undone"

    # Rule 2: a list selection replaced by the next selection in the same list.
    # Typing into a list box (role combobox) selects an option; typing again
    # selects another, so the first choice (and the Enter that closed it) has
    # no effect on the result. Text boxes append, so they are never matched.
    role = None
    for pos, event in enumerate(events):
        if event.get("kind") == "click":
            role = (event.get("structural") or {}).get("role")
            continue
        if event.get("kind") != "type" or role != "combobox" or event.get("param"):
            continue
        if event.get("secret") or pos in dropped:
            continue
        j = pos + 1
        enter = None
        if j < len(events) and events[j].get("kind") == "key":
            if str(events[j].get("key", "")).lower() in ("enter", "return"):
                enter, j = j, j + 1
        if j >= len(events):
            continue
        later = events[j]
        if later.get("kind") == "type" and later.get("field_label") == event.get(
            "field_label"
        ):
            if event.get("field_label") and not _structure_changed(event):
                dropped[pos] = "selection_replaced"
                if enter is not None and not _structure_changed(events[enter]):
                    dropped[enter] = "selection_replaced"

    # The correction rule (see the module doc), before the frame rules.
    _corrections(events, dropped)

    # Rule 3: a dead end, a run of steps that ends where it started.
    pos = 0
    while pos < len(events):
        if pos in dropped or not _dead_end_member(events[pos]):
            pos += 1
            continue
        start = events[pos]
        run = [pos]
        end: Optional[int] = None
        for j in range(pos + 1, len(events)):
            if j in dropped:
                continue
            if not _dead_end_member(events[j]):
                break
            run.append(j)
            nxt = next(
                (events[k] for k in range(j + 1, len(events)) if k not in dropped),
                None,
            )
            if (
                events[j].get("kind") in ("click", "double_click")
                and nxt is not None
                and nxt.get("kind") in _FOCUS_CONSUMERS
            ):
                continue
            if _same_structure(start, events[j]) and frames_unchanged(
                frame(start["i"], "before"),
                frame(events[j]["i"], "after"),
                tolerance=_REPAINT_NOISE,
            ):
                end = j
        if end is None:
            pos += 1
            continue
        for j in run:
            if j <= end:
                dropped[j] = "dead_end"
        pos = end + 1

    # Rule 4: a step whose own frames show no effect.
    for pos, event in enumerate(events):
        if pos in dropped:
            continue
        nxt = next(
            (events[j] for j in range(pos + 1, len(events)) if j not in dropped), None
        )
        if _keeps_because(event, nxt) is not None:
            continue
        if frames_unchanged(frame(event["i"], "before"), frame(event["i"], "after")):
            dropped[pos] = "no_visible_effect"

    dst.mkdir(parents=True)
    (dst / "frames").mkdir()
    kept_lines = []
    for new_i, (pos, event) in enumerate(
        (p, e) for p, e in enumerate(events) if p not in dropped
    ):
        old_i = event["i"]
        for side in ("before", "after"):
            source_frame = frames / f"{old_i:04d}_{side}.png"
            if source_frame.exists():
                shutil.copyfile(
                    source_frame, dst / "frames" / f"{new_i:04d}_{side}.png"
                )
        kept_lines.append(json.dumps({**event, "i": new_i, "distilled_from_i": old_i}))
    (dst / "events.jsonl").write_text(
        "".join(line + "\n" for line in kept_lines), encoding="utf-8"
    )
    meta_path = src / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["distilled_from"] = meta.get("id")
        (dst / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    for name in sorted(p.name for p in src.iterdir() if p.is_file()):
        if name not in ("events.jsonl", "meta.json") and not (dst / name).exists():
            shutil.copyfile(src / name, dst / name)
    report = DistillReport(
        source=str(src),
        output=str(dst),
        steps_in=len(events),
        steps_out=len(kept_lines),
        dropped=[
            DroppedStep(
                index=events[pos]["i"], kind=str(events[pos].get("kind")), rule=rule
            )
            for pos, rule in sorted(dropped.items())
        ],
    )
    (dst / "distill.json").write_text(
        json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    return report
