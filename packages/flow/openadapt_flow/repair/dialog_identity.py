"""Find the dialog that encloses a click, so a dismiss step can carry identity.

A step that only dismisses a dialog has no record identity of its own: no
patient row, no name band. Without identity evidence the fault campaign
refuses it, because the same button in an unrelated dialog would pass. This
module finds the dialog's box. The recording then marks it as the step's
identifier region with ``identifier_scope: "dialog"``, and the compiler:

- reads the dialog's text (title, body, button labels) as the step's
  identity, the same OCR identity check every marked row gets; and
- uses the clicked control's own label as the locator, and aims at its
  center.

A DOM- or accessibility-backed caller should pass the dialog's box (for
example the bounding box of the nearest ``role=dialog`` element) and skip the
pixel search.
"""

from __future__ import annotations

from typing import Optional

__all__ = ["enclosing_dialog_region", "mark_dialog_in_recording"]

Region = tuple[int, int, int, int]

#: The dialog must be clearly bigger than the control inside it.
_MIN_AREA_RATIO = 4.0
#: And smaller than most of the screen (a page background isn't a dialog).
_MAX_SCREEN_FRACTION = 0.6


def enclosing_dialog_region(
    frame_png: bytes, click: tuple[int, int]
) -> Optional[Region]:
    """The smallest drawn box around ``click`` that is much larger than the
    control under it, or None. Pixel-only; returns None when unsure."""
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(frame_png, np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None
    height, width = image.shape
    edges = cv2.Canny(image, 40, 120)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes: set[Region] = set()
    for contour in contours:
        approx = cv2.approxPolyDP(contour, 0.02 * cv2.arcLength(contour, True), True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        x, y, w, h = cv2.boundingRect(approx)
        if w < 12 or h < 12:
            continue
        if x <= click[0] < x + w and y <= click[1] < y + h:
            boxes.add((x, y, w, h))
    if not boxes:
        return None
    ordered = sorted(boxes, key=lambda box: box[2] * box[3])
    control_area = ordered[0][2] * ordered[0][3]
    for box in ordered:
        area = box[2] * box[3]
        if area >= _MIN_AREA_RATIO * control_area:
            if area > _MAX_SCREEN_FRACTION * width * height:
                return None
            return box
    return None


def mark_dialog_in_recording(
    recording_dir, *, dialog_region: Optional[Region] = None
) -> int:
    """Mark the enclosing dialog on each click in a one-shot fix recording.

    Leaves events that already carry an ``identifier_region`` alone. Returns
    how many events were marked. Unmarked clicks stay identity-free, so the
    fault campaign refuses them (fail closed).
    """
    import json
    from pathlib import Path

    recording = Path(recording_dir)
    events_path = recording / "events.jsonl"
    lines = [
        line
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    marked = 0
    out = []
    for line in lines:
        event = json.loads(line)
        if event.get("kind") == "click" and "identifier_region" not in event:
            region = dialog_region
            if region is None:
                frame = recording / "frames" / f"{int(event['i']):04d}_before.png"
                if frame.is_file():
                    region = enclosing_dialog_region(
                        frame.read_bytes(), (int(event["x"]), int(event["y"]))
                    )
            if region is not None:
                event["identifier_region"] = [int(v) for v in region]
                event["identifier_scope"] = "dialog"
                marked += 1
        out.append(json.dumps(event))
    events_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return marked
