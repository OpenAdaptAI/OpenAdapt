"""Hover-before-click evidence for steps with no record identity.

An icon-only click (a toolbar icon, a floppy-disk Save) on a pixel surface
carries no text that says which control it is, so the identity check has
nothing to compare. Most applications answer a pointer resting on a control:
a highlight, a raised border, a tooltip or status-bar text near the pointer.
The recording can capture that answer, and replay can ask the same question
before it clicks.

At record time the recorder moves the pointer onto the target, waits
:data:`HOVER_DWELL_MS`, and saves the frame. The compiler keeps the pixels
that changed near the target (:func:`measure_response`) as the step's
:class:`~openadapt_flow.ir.HoverEvidence`. At replay, on a pixel surface, the
runtime moves the pointer onto the resolved target, waits the same time, and
compares (:func:`compare`):

- ``verified``: the live response has the recorded shape and position and
  the same content (pixels on the same render; OCR text otherwise);
- ``mismatch``: a response with other content, or in another place or
  shape; the run stops before the click;
- ``unexpected``: the recording saw no response and the live screen
  answered; the run stops before the click;
- ``absent``: the recording saw a response and the live screen did not.
  No evidence; the step goes on as it did before hover evidence existed;
- ``unverifiable``: shape and position match but neither pixels nor text
  can be compared (another render with no readable text). No evidence.

Hover evidence only adds stops: ``mismatch`` and ``unexpected`` stop the run;
the other verdicts are recorded on the step and change nothing.

Moving the pointer clicks nothing. Hover-opened menus are the one known
hazard: a response far from the target falls outside the measured window and
is ignored, but a menu that opens under the pointer is a response like any
other and is compared like one.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

from openadapt_flow.ir import Point, Region
from openadapt_flow.runtime import identity as identity_mod

HOVER_DWELL_MS = 800
"""How long the pointer rests on the target before the hover frame."""

# The measured window around the pointer. Tooltips usually open below and to
# the right of the pointer; highlights cover the control under it.
WINDOW_LEFT = 160
WINDOW_RIGHT = 320
WINDOW_UP = 80
WINDOW_DOWN = 160

CHANGE_LEVEL = 24
"""Per-channel difference (0-255) for a pixel to count as changed."""

MIN_CHANGED_PX = 12
"""Fewer changed pixels than this is noise, not a response."""

GEOMETRY_TOL_PX = 10
GEOMETRY_TOL_FRAC = 0.25
CROP_MATCH_MAX_MEAN = 0.04
"""Mean grey difference (0-1) over the whole aligned response, at most."""

BLOCK_PX = 8
BLOCK_MAX_MEAN = 0.12
"""Mean grey difference in any :data:`BLOCK_PX` square, at most."""

ALIGN_MAX_SHIFT = 3

Verdict = Literal["verified", "mismatch", "unexpected", "absent", "unverifiable"]


@dataclass(frozen=True)
class Response:
    """What changed near the pointer when it came to rest on the target."""

    region: Region
    crop: bytes
    changed_px: int


def hover_window(point: Point, viewport: tuple[int, int]) -> Region:
    x, y = int(point[0]), int(point[1])
    w, h = int(viewport[0]), int(viewport[1])
    x0 = max(0, x - WINDOW_LEFT)
    y0 = max(0, y - WINDOW_UP)
    x1 = min(w, x + WINDOW_RIGHT)
    y1 = min(h, y + WINDOW_DOWN)
    return (x0, y0, max(0, x1 - x0), max(0, y1 - y0))


def _rgb(png: bytes) -> Any:
    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        return np.asarray(im.convert("RGB"), dtype=np.int16)


def measure_response(
    pre_png: bytes, hover_png: bytes, point: Point
) -> Optional[Response]:
    """The changed box near ``point`` between two frames, or None.

    Raises ValueError when the frames differ in size (the screen changed
    shape, which is not a hover response).
    """
    import numpy as np

    a, b = _rgb(pre_png), _rgb(hover_png)
    if a.shape != b.shape:
        raise ValueError("hover frame differs in size from the frame before it")
    x, y, w, h = hover_window(point, (a.shape[1], a.shape[0]))
    if w <= 0 or h <= 0:
        return None
    diff = np.abs(a[y : y + h, x : x + w] - b[y : y + h, x : x + w]).max(axis=2)
    mask = diff > CHANGE_LEVEL
    count = int(mask.sum())
    if count < MIN_CHANGED_PX:
        return None
    ys, xs = np.nonzero(mask)
    region = (
        x + int(xs.min()),
        y + int(ys.min()),
        int(xs.max() - xs.min() + 1),
        int(ys.max() - ys.min() + 1),
    )
    crop = identity_mod.crop_region(hover_png, region)
    if crop is None:
        return None
    return Response(region=region, crop=crop, changed_px=count)


def relative(region: Region, point: Point) -> Region:
    """``region`` as an offset from ``point`` (dx, dy, w, h)."""
    return (
        int(region[0] - point[0]),
        int(region[1] - point[1]),
        int(region[2]),
        int(region[3]),
    )


def _close(a: int, b: int, size: int) -> bool:
    return abs(a - b) <= max(GEOMETRY_TOL_PX, GEOMETRY_TOL_FRAC * size)


def geometry_matches(recorded: Region, live: Region) -> bool:
    """Same place and shape relative to the target, within tolerance."""
    rx, ry, rw, rh = recorded
    lx, ly, lw, lh = live
    return (
        _close(rx + rw // 2, lx + lw // 2, rw)
        and _close(ry + rh // 2, ly + lh // 2, rh)
        and _close(rw, lw, rw)
        and _close(rh, lh, rh)
    )


def crops_look_same(recorded_png: bytes, live_png: bytes) -> bool:
    """Whether two response crops show the same thing.

    Grey images. The live crop is aligned onto the recorded one by the best
    shift of up to :data:`ALIGN_MAX_SHIFT` pixels (a response drawn a pixel
    or two away), or resized when the sizes differ by more. Then every
    :data:`BLOCK_PX` square must agree: a changed word in a tooltip is a
    local difference that an average over the whole crop would hide.
    """
    import numpy as np
    from PIL import Image

    with (
        Image.open(io.BytesIO(recorded_png)) as r,
        Image.open(io.BytesIO(live_png)) as lv,
    ):
        a = np.asarray(r.convert("L"), dtype=np.float32) / 255.0
        lg = lv.convert("L")
        if (
            abs(lg.size[0] - a.shape[1]) > ALIGN_MAX_SHIFT
            or abs(lg.size[1] - a.shape[0]) > ALIGN_MAX_SHIFT
        ):
            lg = lg.resize((a.shape[1], a.shape[0]))
        b = np.asarray(lg, dtype=np.float32) / 255.0
    pad = ALIGN_MAX_SHIFT
    h, w = a.shape
    canvas = np.full((h + 2 * pad, w + 2 * pad), np.nan, dtype=np.float32)
    bh, bw = min(b.shape[0], h + pad), min(b.shape[1], w + pad)
    best: Optional[Any] = None
    best_mean = float("inf")
    for dy in range(-pad, pad + 1):
        for dx in range(-pad, pad + 1):
            canvas[:] = np.nan
            y0, x0 = pad + dy, pad + dx
            canvas[y0 : y0 + bh, x0 : x0 + bw] = b[:bh, :bw]
            window = canvas[pad : pad + h, pad : pad + w]
            diff = np.abs(window - a)
            covered = ~np.isnan(diff)
            if covered.mean() < 0.9:
                continue
            mean = float(np.nanmean(diff))
            if mean < best_mean:
                best_mean, best = mean, np.where(covered, diff, 0.0)
    if best is None or best_mean > CROP_MATCH_MAX_MEAN:
        return False
    for y in range(0, h, BLOCK_PX):
        for x in range(0, w, BLOCK_PX):
            if float(best[y : y + BLOCK_PX, x : x + BLOCK_PX].mean()) > BLOCK_MAX_MEAN:
                return False
    return True


def _text(png: bytes, ocr: Callable[..., Any]) -> str:
    try:
        lines = ocr(png) or []
    except Exception:  # noqa: BLE001 - unreadable is no text
        return ""
    words = " ".join(str(getattr(line, "text", "")) for line in lines)
    return identity_mod.ocr_canonical(identity_mod.squash(words))


def compare(
    *,
    recorded_response: bool,
    recorded_region: Optional[Region],
    recorded_crop: Optional[bytes],
    live: Optional[Response],
    point: Point,
    same_render: bool,
    ocr: Optional[Callable[..., Any]] = None,
) -> tuple[Verdict, str]:
    """Compare the live hover response with the recorded one.

    Args:
        recorded_response: Whether the recording saw any response.
        recorded_region: The recorded response box, relative to the click.
        recorded_crop: The recorded response pixels.
        live: The live response at the resolved point, or None.
        point: The resolved point.
        same_render: The live frame is drawn at the recorded scale and by the
            same rasterizer, so pixels can be compared.
        ocr: OCR for the other-render comparison.

    Returns:
        ``(verdict, reason)``.
    """
    if not recorded_response:
        if live is None:
            # Agrees, but no response is not evidence of which control it is.
            return "unverifiable", "no_response_as_recorded"
        return "unexpected", "response_where_none_was_recorded"
    if live is None:
        return "absent", "no_response"
    if recorded_region is None or recorded_crop is None:
        return "unverifiable", "recording_kept_no_response"
    if not geometry_matches(recorded_region, relative(live.region, point)):
        return "mismatch", "response_moved_or_resized"
    if same_render and not identity_mod.text_render_differs(recorded_crop, live.crop):
        if crops_look_same(recorded_crop, live.crop):
            return "verified", "same_pixels"
        if ocr is None:
            return "mismatch", "different_pixels"
        # Another rasterizer can redraw the same tooltip with other pixels
        # (ClearType switched); its text still decides.
    if ocr is not None:
        rec_text, live_text = _text(recorded_crop, ocr), _text(live.crop, ocr)
        if rec_text and live_text:
            if rec_text == live_text:
                return "verified", "same_text"
            return "mismatch", "different_text"
        if rec_text or live_text:
            return "mismatch", "text_on_one_side_only"
    if same_render:
        return "mismatch", "different_pixels"
    return "unverifiable", "no_comparable_content"


__all__ = [
    "HOVER_DWELL_MS",
    "Response",
    "Verdict",
    "compare",
    "crops_look_same",
    "geometry_matches",
    "hover_window",
    "measure_response",
    "relative",
]
