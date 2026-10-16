"""A tiny rendered app where a dialog blocks a workflow step.

Used by the tests and by openadapt-agent's end-to-end repair test. The page
has a Save shortcut and a "Saved" banner the workflow clicks to confirm. When
``dialog`` is up, a "Survey Required" dialog covers the banner, so the
recorded workflow halts. The dialog appears when Save is pressed, as a
survey prompt would. Clicking the dialog's Dismiss button closes it.

Frames are drawn with OpenCV, so Flow's real template matcher and OCR run
on them. Nothing here talks to a network or a real application.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    ProgramGraph,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
)

__all__ = [
    "DIALOG_REGION",
    "DISMISS_CLICK",
    "VIEWPORT",
    "DialogApp",
    "base_bundle",
    "record_click",
]

VIEWPORT = (1280, 800)
WORKFLOW_NAME = "dialog-fixture-save"
#: The dialog's outer box (x, y, w, h).
DIALOG_REGION = (40, 60, 562, 122)
#: Where a person (or calling agent) clicks to dismiss the dialog.
DISMISS_CLICK = (140, 140)
_DISMISS_BUTTON = (60, 116, 220, 164)
_BANNER = (400, 300, 560, 340)


class DialogApp:
    """A fake backend whose screenshots are rendered frames."""

    def __init__(self, *, dialog: bool = True, saved: bool = False) -> None:
        #: Whether Save opens the dialog.
        self.dialog_on_save = dialog
        self.saved = saved
        self.dialog = dialog and saved
        self.actions: list[tuple] = []

    @property
    def viewport(self) -> tuple[int, int]:
        return VIEWPORT

    def render(self) -> bytes:
        import cv2
        import numpy as np

        font = cv2.FONT_HERSHEY_SIMPLEX
        img = np.full((VIEWPORT[1], VIEWPORT[0], 3), 245, np.uint8)
        cv2.putText(img, "Encounter list", (40, 30), font, 0.8, (30, 30, 30), 2)
        cv2.putText(
            img, "Visit notes and orders", (40, 400), font, 0.8, (30, 30, 30), 2
        )
        if self.saved:
            x0, y0, x1, y1 = _BANNER
            cv2.rectangle(img, (x0, y0), (x1, y1), (200, 235, 200), -1)
            cv2.putText(img, "Saved OK", (x0 + 20, y0 + 28), font, 0.8, (0, 80, 0), 2)
        if self.dialog:
            x, y, w, h = DIALOG_REGION
            cv2.rectangle(img, (x, y), (x + w - 2, y + h - 2), (255, 255, 255), -1)
            cv2.rectangle(img, (x, y), (x + w - 2, y + h - 2), (90, 90, 90), 2)
            cv2.putText(img, "Survey Required", (60, 95), font, 0.9, (0, 0, 0), 2)
            bx0, by0, bx1, by1 = _DISMISS_BUTTON
            cv2.rectangle(img, (bx0, by0), (bx1, by1), (225, 225, 225), -1)
            cv2.rectangle(img, (bx0, by0), (bx1, by1), (60, 60, 60), 2)
            cv2.putText(img, "Dismiss", (84, 148), font, 0.8, (0, 0, 0), 2)
            cv2.putText(img, "Answer it later", (260, 148), font, 0.7, (0, 0, 0), 2)
            # The dialog covers the banner's row on this small page.
            cv2.rectangle(img, (380, 280), (600, 360), (255, 255, 255), -1)
            cv2.rectangle(img, (380, 280), (600, 360), (90, 90, 90), 2)
        ok, buf = cv2.imencode(".png", img)
        assert ok
        return buf.tobytes()

    def screenshot(self) -> bytes:
        return self.render()

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self.actions.append(("click", x, y))
        bx0, by0, bx1, by1 = _DISMISS_BUTTON
        if self.dialog and bx0 <= x <= bx1 and by0 <= y <= by1:
            self.dialog = False

    def press(self, key: str) -> None:
        self.actions.append(("press", key))
        if key == "S":
            self.saved = True
            self.dialog = self.dialog_on_save

    def type_text(self, text: str) -> None:
        self.actions.append(("type", len(text)))

    def scroll(self, dx: int, dy: int) -> None:
        self.actions.append(("scroll", dx, dy))


def _crop(png: bytes, box: tuple[int, int, int, int]) -> bytes:
    import io

    from PIL import Image

    x0, y0, x1, y1 = box
    with Image.open(io.BytesIO(png)) as image:
        out = io.BytesIO()
        image.crop((x0, y0, x1, y1)).save(out, format="PNG")
        return out.getvalue()


def base_bundle(path: Path | str) -> Path:
    """Save the workflow that halts while the dialog is up."""
    path = Path(path)
    (path / "templates").mkdir(parents=True, exist_ok=True)
    app = DialogApp(dialog=False, saved=True)
    (path / "templates" / "verify.png").write_bytes(_crop(app.render(), _BANNER))
    x0, y0, x1, y1 = _BANNER
    program = ProgramGraph(
        entry="s_save",
        states={
            "s_save": State(
                id="s_save",
                kind=StateKind.ACTION,
                step=Step(
                    id="s_save", intent="Save encounter", action=ActionKind.KEY, key="S"
                ),
                transitions=[Transition(target="s_verify")],
            ),
            "s_verify": State(
                id="s_verify",
                kind=StateKind.ACTION,
                step=Step(
                    id="s_verify",
                    intent="Confirm saved",
                    action=ActionKind.CLICK,
                    anchor=Anchor(
                        template="templates/verify.png",
                        region=(x0, y0, x1 - x0, y1 - y0),
                        click_point=((x0 + x1) // 2, (y0 + y1) // 2),
                        ocr_text="Saved OK",
                    ),
                    timeout_s=0.2,
                ),
                transitions=[Transition(target="__end__")],
            ),
            "__end__": State(id="__end__", kind=StateKind.TERMINAL, outcome="success"),
        },
    )
    Workflow(name=WORKFLOW_NAME, viewport=VIEWPORT, program=program).save(path)
    return path


def record_click(
    directory: Path | str,
    before: bytes,
    after: bytes,
    click: tuple[int, int],
    *,
    dialog_region: Optional[tuple[int, int, int, int]] = None,
) -> Path:
    """Write a one-click recording in the format ``openadapt-flow record`` uses."""
    directory = Path(directory)
    (directory / "frames").mkdir(parents=True, exist_ok=True)
    (directory / "frames" / "0000_before.png").write_bytes(before)
    (directory / "frames" / "0000_after.png").write_bytes(after)
    event: dict = {"i": 0, "kind": "click", "x": click[0], "y": click[1], "t": 1.0}
    if dialog_region is not None:
        event["identifier_region"] = list(dialog_region)
        event["identifier_scope"] = "dialog"
    (directory / "events.jsonl").write_text(json.dumps(event) + "\n")
    (directory / "meta.json").write_text(
        json.dumps(
            {
                "id": f"rec-{directory.name}",
                "created_at": "2026-10-14T00:00:00+00:00",
                "viewport": list(VIEWPORT),
                "app_url": "",
                "params": {},
            }
        )
    )
    return directory
