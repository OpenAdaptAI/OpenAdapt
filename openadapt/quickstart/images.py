"""Show a screenshot inline on terminals that can draw images.

Two protocols cover the terminals that support it: iTerm2's inline images and
Kitty's graphics protocol (Kitty and Ghostty). A terminal is a candidate when
the variables it sets for its own sessions name it, and it is used only when
it also answers a query for that protocol (:func:`terminal_answers`). An
unknown terminal, a terminal multiplexer (tmux and screen don't pass images
through unless configured to), output that isn't a terminal, or a terminal
that doesn't answer gets ``None``, and the caller shows the text description
alone.

WezTerm sets ``TERM_PROGRAM=WezTerm`` and speaks the iTerm2 protocol, but a
WezTerm run showed blank rows where the image should have been, so it is not
a candidate on its own; ``OPENADAPT_INLINE_IMAGES=iterm2`` turns it on.
"""

from __future__ import annotations

import base64
import io
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Mapping, Optional

ITERM2 = "iterm2"
KITTY = "kitty"

#: Set to 0 to turn inline images off, or to iterm2/kitty to choose a protocol
#: that detection can't see (for example inside a tmux passthrough setup).
OVERRIDE_ENV = "OPENADAPT_INLINE_IMAGES"


def detect_image_protocol(
    environ: Optional[Mapping[str, str]] = None, *, isatty: bool
) -> Optional[str]:
    """Return the image protocol this terminal names itself for, or None.

    This reads only the environment. :func:`image_protocol` also asks the
    terminal before anything is drawn.
    """

    env = os.environ if environ is None else environ
    override = (env.get(OVERRIDE_ENV) or "").strip().lower()
    if override in ("0", "off", "no", "false"):
        return None
    if override in (ITERM2, KITTY):
        return override
    if not isatty:
        return None
    term = env.get("TERM", "")
    if env.get("TMUX") or env.get("STY") or term.startswith(("screen", "tmux")):
        return None
    program = env.get("TERM_PROGRAM", "")
    if program == "iTerm.app" or env.get("LC_TERMINAL") == "iTerm2":
        return ITERM2
    if env.get("KITTY_WINDOW_ID") or term == "xterm-kitty":
        return KITTY
    if program == "ghostty" or term == "xterm-ghostty":
        return KITTY
    return None


def image_protocol(
    environ: Optional[Mapping[str, str]] = None,
    *,
    isatty: bool,
    answers: Optional[Callable[[str], bool]] = None,
) -> Optional[str]:
    """The protocol to draw with: named by the terminal and answered by it.

    ``OPENADAPT_INLINE_IMAGES`` set to a protocol is used as given, without a
    query, for setups detection can't see.
    """

    env = os.environ if environ is None else environ
    candidate = detect_image_protocol(env, isatty=isatty)
    if candidate is None:
        return None
    if (env.get(OVERRIDE_ENV) or "").strip().lower() == candidate:
        return candidate
    return candidate if (answers or terminal_answers)(candidate) else None


#: Kitty's graphics query (a 1x1 image it checks but doesn't store), and
#: iTerm2's cell-size report. Each is followed by Primary Device Attributes,
#: which every terminal answers, so a terminal that ignores the first query
#: is known as soon as its device attributes arrive.
_QUERIES = {
    KITTY: "\x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\",
    ITERM2: "\x1b]1337;ReportCellSize\x07",
}
_ANSWERS = {
    KITTY: re.compile(rb"\x1b_Gi=31;OK"),
    ITERM2: re.compile(rb"\x1b\]1337;ReportCellSize="),
}
_DEVICE_ATTRIBUTES = "\x1b[c"
_DEVICE_ANSWER = re.compile(rb"\x1b\[\?[0-9;]*c")


def terminal_answers(protocol: str, *, timeout_s: float = 0.5) -> bool:
    """Ask the controlling terminal whether it speaks ``protocol``.

    Writes the protocol's query and a device-attributes request to
    ``/dev/tty`` with echo off, then reads until the device attributes arrive
    or ``timeout_s`` passes. True only when the protocol's own answer came
    back. Windows, or no controlling terminal, is False.
    """

    if sys.platform == "win32":
        return False
    try:
        import select
        import termios
        import tty
    except ImportError:
        return False
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    try:
        try:
            saved = termios.tcgetattr(fd)
        except termios.error:
            return False
        try:
            tty.setcbreak(fd)
            os.write(fd, (_QUERIES[protocol] + _DEVICE_ATTRIBUTES).encode("ascii"))
            reply = b""
            deadline = time.monotonic() + timeout_s
            while not _DEVICE_ANSWER.search(reply):
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                ready, _, _ = select.select([fd], [], [], left)
                if not ready:
                    break
                chunk = os.read(fd, 1024)
                if not chunk:
                    break
                reply += chunk
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        return bool(_ANSWERS[protocol].search(reply))
    except OSError:
        return False
    finally:
        os.close(fd)


def crop_png(path: Path, box: tuple[int, int, int, int]) -> bytes:
    """Return the ``(left, top, right, bottom)`` part of a PNG, as PNG bytes."""

    from openadapt_flow import sealed_set
    from PIL import Image

    # A run's screenshots are encrypted on disk: decrypt in memory only.
    with Image.open(io.BytesIO(sealed_set.read_bytes(path))) as image:
        width, height = image.size
        left, top, right, bottom = box
        cropped = image.crop((left, top, min(right, width), min(bottom, height)))
        out = io.BytesIO()
        cropped.save(out, format="PNG")
    return out.getvalue()


def image_sequence(png: bytes, protocol: str, *, cols: int, rows: int) -> str:
    """The escape sequence that draws ``png`` in a ``cols`` x ``rows`` cell box."""

    data = base64.b64encode(png).decode("ascii")
    if protocol == ITERM2:
        return (
            f"\x1b]1337;File=inline=1;size={len(png)};width={cols};height={rows};"
            f"preserveAspectRatio=1:{data}\x07"
        )
    if protocol == KITTY:
        # Kitty takes the payload in chunks of at most 4096 bytes. q=2 keeps
        # the terminal from writing a reply into the person's input.
        chunks = [data[i : i + 4096] for i in range(0, len(data), 4096)] or [""]
        parts = []
        for index, chunk in enumerate(chunks):
            more = 1 if index < len(chunks) - 1 else 0
            if index == 0:
                head = f"a=T,f=100,q=2,c={cols},r={rows},m={more}"
            else:
                head = f"m={more}"
            parts.append(f"\x1b_G{head};{chunk}\x1b\\")
        return "".join(parts)
    raise ValueError(f"unknown image protocol {protocol!r}")


def beside_image(
    image: str, right: list[str], *, indent: int, cols: int, rows: int, gap: int = 3
) -> str:
    """Draw ``image`` at the left and the ``right`` lines next to it.

    The block reserves its rows first, so a screen that has to scroll does it
    before anything is drawn. The cursor is saved around the image because
    the two protocols leave it in different places.
    """

    height = max(rows, len(right))
    out = ["\n" * height, f"\x1b[{height}A\r"]
    out.append("\x1b7")
    if indent:
        out.append(f"\x1b[{indent}C")
    out.append(image)
    out.append("\x1b8")
    column = indent + cols + gap
    for index in range(height):
        line = right[index] if index < len(right) else ""
        out.append(f"\r\x1b[{column}C{line}\n" if line else "\n")
    return "".join(out)


__all__ = [
    "ITERM2",
    "KITTY",
    "OVERRIDE_ENV",
    "beside_image",
    "crop_png",
    "detect_image_protocol",
    "image_protocol",
    "image_sequence",
    "terminal_answers",
]
