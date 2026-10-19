"""Drive a desktop app on an X display with keys only, and read it back.

PROTOTYPE. This is a small scripted driver for the demo. In a deployment the
same steps are a recorded and compiled Flow workflow run by the governed
runtime over RDP; this driver stands in for that so the template runs on a
bare X server (Xvfb) today.

What it observes is what a person sees: the window title, the screen pixels
(OCR) and the clipboard after Ctrl+A, Ctrl+C. It makes no model calls.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional

from PIL import Image

#: Seconds to linger on each reached screen; raise it to slow a recorded demo.
PACE = float(os.environ.get("DEMO_PACE", "0.15"))


class DriverHalt(RuntimeError):
    """The screen did not reach the expected state. Stop and ask a person."""


def _run(*args: str, input_text: Optional[str] = None) -> str:
    out = subprocess.run(
        args, check=True, capture_output=True, text=True, input=input_text
    )
    return out.stdout


def norm(text: str) -> str:
    """Compare form: NFKC, no whitespace, upper case."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).upper()


_FOLD = str.maketrans({"O": "0", "I": "1", "L": "1"})


def label_key(text: str) -> str:
    """Looser form used only to find a label such as 'DOB' read as 'D0B'."""
    return norm(text).translate(_FOLD)


@dataclass
class OcrLine:
    text: str
    bbox: tuple[int, int, int, int]
    confidence: float


@dataclass
class Frame:
    png: bytes
    lines: list[OcrLine]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.png).hexdigest()

    def crop_sha256(self, bbox: tuple[int, int, int, int]) -> str:
        x, y, w, h = bbox
        img = Image.open(io.BytesIO(self.png)).crop((x, y, x + w, y + h))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return hashlib.sha256(buf.getvalue()).hexdigest()

    def value_after(self, label: str) -> Optional[OcrLine]:
        """The OCR line that starts with ``label:``; its text is the value."""
        want = label_key(label + ":")
        for line in sorted(self.lines, key=lambda ln: (ln.bbox[1], ln.bbox[0])):
            key = label_key(line.text)
            if key.startswith(want):
                raw = unicodedata.normalize("NFKC", line.text)
                value = raw.split(":", 1)[1].strip() if ":" in raw else ""
                return OcrLine(value, line.bbox, line.confidence)
        return None


_OCR = None


def ocr(png: bytes) -> list[OcrLine]:
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR = RapidOCR()
    result, _ = _OCR(png)
    lines = []
    for box, text, conf in result or []:
        xs = [int(p[0]) for p in box]
        ys = [int(p[1]) for p in box]
        bbox = (min(xs), min(ys), max(1, max(xs) - min(xs)), max(1, max(ys) - min(ys)))
        lines.append(OcrLine(text, bbox, float(conf)))
    return lines


class X11App:
    def __init__(self, title_prefix: str, *, timeout: float = 8.0) -> None:
        self.prefix = title_prefix
        self.timeout = timeout
        deadline = time.time() + timeout
        while True:
            ids = _run(
                "xdotool", "search", "--name", f"^{re.escape(title_prefix)}"
            ).split()
            if len(ids) == 1:
                self.wid = ids[0]
                break
            if len(ids) > 1:
                raise DriverHalt(f"{len(ids)} windows match {title_prefix!r}")
            if time.time() > deadline:
                raise DriverHalt(f"no window {title_prefix!r}")
            time.sleep(0.2)

    def title(self) -> str:
        return _run("xdotool", "getwindowname", self.wid).strip()

    def screen(self) -> str:
        return self.title()[len(self.prefix) :].lstrip(" -")

    def focus(self) -> None:
        _run("xdotool", "windowfocus", "--sync", self.wid)

    def key(self, *keys: str) -> None:
        self.focus()
        _run("xdotool", "key", "--delay", "40", *keys)

    def type(self, text: str) -> None:
        self.focus()
        _run("xdotool", "type", "--delay", "25", text)

    def wait_screen(self, pattern: str) -> re.Match[str]:
        deadline = time.time() + self.timeout
        while True:
            m = re.fullmatch(pattern, self.screen())
            if m:
                time.sleep(PACE)
                return m
            if time.time() > deadline:
                raise DriverHalt(f"expected screen {pattern!r}, saw {self.screen()!r}")
            time.sleep(0.1)

    def fill(self, values: list[str]) -> None:
        """Type into a form's fields in tab order."""
        for i, v in enumerate(values):
            if i:
                self.key("Tab")
            self.type(v)

    def copy_pane(self) -> str:
        """Ctrl+A, Ctrl+C on the record pane, then read the clipboard."""
        # Clear the clipboard first so a stale value cannot pass as a read.
        # xclip -i stays alive to serve the selection, so do not wait on it.
        subprocess.Popen(
            ["xclip", "-selection", "clipboard", "-i", "/dev/null"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).wait(2)
        self.key("ctrl+a", "ctrl+c")
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                text = subprocess.run(
                    ["xclip", "-selection", "clipboard", "-o"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                ).stdout
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                text = ""
            if len(text) >= 10:  # a stale or empty clipboard is not a read
                return text
            time.sleep(0.1)
        raise DriverHalt("clipboard stayed empty after copy")

    def frame(self) -> Frame:
        png = subprocess.run(
            ["import", "-window", self.wid, "png:-"], check=True, capture_output=True
        ).stdout
        return Frame(png, ocr(png))


def pane_value(text: str, label: str, *, last: bool = False) -> Optional[str]:
    """The value after ``label:`` in copied pane text."""
    hits = [
        ln.split(":", 1)[1].strip()
        for ln in text.splitlines()
        if ln.strip().lower().startswith(label.lower() + ":")
    ]
    if not hits:
        return None
    return hits[-1] if last else hits[0]
