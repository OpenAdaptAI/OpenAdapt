"""Small X11 capture and real input adapter used by the local benchmark."""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import time
from pathlib import Path

from PIL import Image, ImageGrab


class X11Unavailable(RuntimeError):
    pass


class DeliveryAcknowledgementLost(RuntimeError):
    """Input was flushed to X11, but the application acknowledgement was lost."""


def _library(name: str) -> ctypes.CDLL:
    path = ctypes.util.find_library(name)
    candidates = [path] if path else []
    candidates.extend(
        (
            f"/opt/X11/lib/lib{name}.6.dylib",
            f"/usr/lib/x86_64-linux-gnu/lib{name}.so.6",
        )
    )
    for candidate in candidates:
        try:
            return ctypes.CDLL(candidate)
        except OSError:
            continue
    raise X11Unavailable(f"X11 library {name!r} is unavailable")


def headed_x11_available() -> bool:
    try:
        session = X11Session()
        session.capture()
    except Exception:
        return False
    return True


class X11Session:
    """Capture the X root and inject pointer or keyboard events through XTest."""

    def __init__(self, display: str | None = None) -> None:
        self.display_name = display or os.environ.get("DISPLAY")
        if not self.display_name:
            raise X11Unavailable("DISPLAY is not set")
        self.x11 = _library("X11")
        self.xtst = _library("Xtst")
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XFlush.argtypes = [ctypes.c_void_p]
        self.x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
        self.x11.XStringToKeysym.restype = ctypes.c_ulong
        self.x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        self.xtst.XTestFakeMotionEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        self.xtst.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        self.xtst.XTestFakeKeyEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        self.display = self.x11.XOpenDisplay(self.display_name.encode("utf-8"))
        if not self.display:
            raise X11Unavailable(f"cannot open X display {self.display_name}")

    def capture(self, path: Path | None = None) -> Image.Image:
        image = ImageGrab.grab(xdisplay=self.display_name).convert("RGB")
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            image.save(path, format="PNG")
        return image

    def move(self, x: int, y: int) -> None:
        if not self.xtst.XTestFakeMotionEvent(self.display, -1, x, y, 0):
            raise X11Unavailable("XTest rejected pointer motion")
        self.x11.XFlush(self.display)
        time.sleep(0.06)

    def click(self, x: int, y: int, *, lose_acknowledgement: bool = False) -> None:
        self.move(x, y)
        for pressed in (1, 0):
            if not self.xtst.XTestFakeButtonEvent(self.display, 1, pressed, 0):
                raise X11Unavailable("XTest rejected pointer button input")
            self.x11.XFlush(self.display)
            time.sleep(0.025)
        if lose_acknowledgement:
            raise DeliveryAcknowledgementLost("post-dispatch application acknowledgement was lost")
        time.sleep(0.08)

    def _key(self, name: str, *, pressed: bool) -> None:
        keysym = self.x11.XStringToKeysym(name.encode("ascii"))
        keycode = self.x11.XKeysymToKeycode(self.display, keysym)
        if not keycode or not self.xtst.XTestFakeKeyEvent(self.display, keycode, int(pressed), 0):
            raise X11Unavailable(f"cannot inject key {name!r}")
        self.x11.XFlush(self.display)

    def type_text(self, text: str) -> None:
        shifted = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ_:+{}|"<>?')
        names = {
            " ": "space",
            "-": "minus",
            ".": "period",
            ",": "comma",
            ":": "semicolon",
            "\n": "Return",
        }
        for character in text:
            name = names.get(character, character.lower() if character.isalpha() else character)
            needs_shift = character in shifted
            if needs_shift:
                self._key("Shift_L", pressed=True)
            self._key(name, pressed=True)
            self._key(name, pressed=False)
            if needs_shift:
                self._key("Shift_L", pressed=False)
            time.sleep(0.008)
        time.sleep(0.08)
