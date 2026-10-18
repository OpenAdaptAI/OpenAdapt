"""A mock accounts-ledger desktop app for the records example. Synthetic data only.

It draws one account screen at a time as a PNG and behaves like a Flow
backend (``screenshot``, ``click``, ``press``). Copying a field puts the
app's own text on its clipboard. ``export_csv`` stands in for the app's
scheduled report export: it reads saved data, not the open screen, so its
reads use ``source_kind="committed_store"``.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

ACCOUNTS = {
    "AC-1042": {
        "holder": "Dana Whitlock",
        "balance": "$12,480.50",
        "opened": "2019-04-02",
        "status": "Open",
    },
    "AC-2210": {
        "holder": "Omar Reyes",
        "balance": "$310.00",
        "opened": "2021-11-15",
        "status": "Closed",
    },
    "AC-3307": {
        "holder": "Lena Park",
        "balance": "$7,905.25",
        "opened": "2023-06-30",
        "status": "Open",
    },
}
LABELS = ("account_number", "holder", "balance", "opened", "status")
ROW_Y = {name: 60 + i * 56 for i, name in enumerate(LABELS)}
VALUE_X, VALUE_W, VALUE_H = 260, 420, 40


def _font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def region(name: str) -> tuple[int, int, int, int]:
    """Screen box of a field's value."""

    return (VALUE_X, ROW_Y[name], VALUE_W, VALUE_H)


@dataclass
class AccountsApp:
    """The app plus its clipboard. ``display_overrides`` changes what the
    screen shows for a field without changing what copy returns, to model a
    rendering problem the double read must catch."""

    viewport: tuple[int, int] = (720, 360)
    current: Optional[str] = None
    clipboard_text: Optional[str] = None
    display_overrides: dict[tuple[str, str], str] = field(default_factory=dict)
    _focused: Optional[str] = None
    _selected: bool = False

    # -- app actions ---------------------------------------------------------

    def open_record(self, key: str) -> None:
        self.current = key
        self._focused = None

    def close_record(self) -> None:
        self.current = None

    def value(self, name: str) -> str:
        assert self.current is not None
        return (
            self.current if name == "account_number" else ACCOUNTS[self.current][name]
        )

    def export_csv(self, path: Path) -> Path:
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(LABELS))
            writer.writeheader()
            for key, row in ACCOUNTS.items():
                writer.writerow({"account_number": key, **row})
        return path

    # -- backend surface -----------------------------------------------------

    def screenshot(self) -> bytes:
        image = Image.new("RGB", self.viewport, "white")
        draw = ImageDraw.Draw(image)
        draw.text((20, 14), "Accounts ledger", fill="black", font=_font(26))
        if self.current is not None:
            for name in LABELS:
                y = ROW_Y[name]
                draw.text(
                    (20, y + 6),
                    name.replace("_", " ").title(),
                    fill="#444",
                    font=_font(22),
                )
                shown = self.display_overrides.get(
                    (self.current, name), self.value(name)
                )
                draw.text((VALUE_X + 6, y + 4), shown, fill="black", font=_font(28))
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()

    def simulated_ocr(self, png: bytes, *, region=None) -> list:
        """What a perfect OCR engine would read in ``region``: the text drawn
        there. For fast, deterministic tests; the demo uses real OCR."""

        from types import SimpleNamespace

        if self.current is None or region is None:
            return []
        for name in LABELS:
            if globals()["region"](name) == tuple(region):
                shown = self.display_overrides.get(
                    (self.current, name), self.value(name)
                )
                return [SimpleNamespace(text=shown, confidence=0.98, region=region)]
        return []

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self._focused = None
        self._selected = False
        for name in LABELS:
            rx, ry, rw, rh = region(name)
            if rx <= x < rx + rw and ry <= y < ry + rh:
                self._focused = name

    def press(self, key: str) -> None:
        if key.endswith("+a"):
            self._selected = self._focused is not None
        elif key.endswith("+c") and self._selected and self._focused:
            self.clipboard_text = self.value(self._focused)


class AppClipboard:
    """The app's clipboard, as a ``ClipboardReader``."""

    def __init__(self, app: AccountsApp) -> None:
        self.app = app

    def read_text(self) -> Optional[str]:
        return self.app.clipboard_text

    def write_text(self, text: str) -> None:
        self.app.clipboard_text = text
