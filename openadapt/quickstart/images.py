"""Show a screenshot inline on terminals that can draw images.

Two protocols cover the terminals that support it: iTerm2's inline images
(iTerm2 and WezTerm) and Kitty's graphics protocol (Kitty and Ghostty). Each
terminal is recognized from the variables it sets for its own sessions. Nothing
is assumed: an unknown terminal, a terminal multiplexer (tmux and screen don't
pass images through unless configured to), or output that isn't a terminal
gets ``None``, and the caller draws a text box instead.
"""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path
from typing import Mapping, Optional

ITERM2 = "iterm2"
KITTY = "kitty"

#: Set to 0 to turn inline images off, or to iterm2/kitty to choose a protocol
#: that detection can't see (for example inside a tmux passthrough setup).
OVERRIDE_ENV = "OPENADAPT_INLINE_IMAGES"


def detect_image_protocol(
    environ: Optional[Mapping[str, str]] = None, *, isatty: bool
) -> Optional[str]:
    """Return the image protocol this terminal speaks, or None when unsure."""

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
    if program == "WezTerm":
        return ITERM2
    if env.get("KITTY_WINDOW_ID") or term == "xterm-kitty":
        return KITTY
    if program == "ghostty" or term == "xterm-ghostty":
        return KITTY
    return None


def crop_png(path: Path, box: tuple[int, int, int, int]) -> bytes:
    """Return the ``(left, top, right, bottom)`` part of a PNG, as PNG bytes."""

    from PIL import Image

    with Image.open(path) as image:
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
    "image_sequence",
]
