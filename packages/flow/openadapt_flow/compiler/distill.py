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

"Visible effect" means at least one pixel changed outside a caret-sized
column (width <= 2 px, height <= 40 px). A blinking caret is not an effect.
"""

from __future__ import annotations

import io
import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PIL import Image

__all__ = ["DistillReport", "DroppedStep", "distill_recording", "frames_unchanged"]

_DROPPABLE_KINDS = {"click", "double_click", "scroll", "key", "hotkey"}
_ACTING_KEYS = {"enter", "return", "tab", "escape", "esc", "kp_enter"}
_FOCUS_CONSUMERS = {"type", "key", "hotkey"}
_STRUCTURAL_KEYS = ("url", "title", "pages")
_CARET_MAX_W = 2
_CARET_MAX_H = 40


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


def frames_unchanged(before: Optional[np.ndarray], after: Optional[np.ndarray]) -> bool:
    """True when the two frames differ by at most a caret-sized column."""

    if before is None or after is None or before.shape != after.shape:
        return False
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


def _load(recording_dir: Path) -> list[dict[str, Any]]:
    lines = (recording_dir / "events.jsonl").read_text().splitlines()
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

    # Rule 3: a step whose own frames show no effect.
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
    (dst / "events.jsonl").write_text("".join(line + "\n" for line in kept_lines))
    meta_path = src / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        meta["distilled_from"] = meta.get("id")
        (dst / "meta.json").write_text(json.dumps(meta, indent=2))
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
    (dst / "distill.json").write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    return report
