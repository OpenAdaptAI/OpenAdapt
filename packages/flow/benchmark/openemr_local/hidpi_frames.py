"""High-density companion frames for a benchmark run's step screenshots.

The engine runs at device scale 1: its templates, identifier crops and
qualification environment are all 1280x800, and the frames it saves under
``steps/`` are the exact bytes it acted on. A page that shows those frames
on a high-density screen wants a sharper copy. This module takes one beside
each frame without changing what the engine sees:

- :func:`attach` wraps a Playwright backend's screenshot and guarded-frame methods. Each time it
  returns a frame whose bytes differ from the last one, the same viewport is
  captured once more through the Chrome DevTools protocol with
  ``clip.scale``, which renders the page at that density (no resize, no
  media-query change, ``devicePixelRatio`` stays 1). The copy is stored as
  ``<dir>/<sha256 of the 1x frame>.png``.
- :func:`pair` matches every ``steps/*.png`` in a run to its copy by that
  hash, checks the copy downscaled against the frame, and writes the copies
  next to the run (never inside the sealed engine directory).

Opt-in: nothing happens unless a caller attaches a directory. The copies are
taken a moment after the engine's frame, with no input between, so they are
for display only and are not evidence. :func:`pair` refuses a copy that
doesn't match its frame.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
from typing import Any

#: A copy is refused as not the same screen when, downscaled to its frame's
#: size, any 32x32 tile differs from the frame by more than this mean absolute
#: difference (0-255 grey levels). On the 56 pairs of four real OpenEMR runs
#: (19 Nov 2026) the largest tile difference was 6.9 (resampling); a different
#: screen of the same form scored 59.
TILE = 32
MAX_TILE_DIFF = 16.0


def capture_scaled(page: Any, scale: int) -> bytes:
    """The page's viewport rendered at ``scale`` times its CSS size, as PNG."""

    viewport = page.viewport_size or {}
    width, height = int(viewport["width"]), int(viewport["height"])
    session = page.context.new_cdp_session(page)
    try:
        shot = session.send(
            "Page.captureScreenshot",
            {
                "format": "png",
                "clip": {
                    "x": 0,
                    "y": 0,
                    "width": width,
                    "height": height,
                    "scale": scale,
                },
                "captureBeyondViewport": False,
            },
        )
    finally:
        session.detach()
    return base64.b64decode(shot["data"])


def attach(backend: Any, out: Path, scale: int = 2) -> None:
    """Store a ``scale``-density copy of each new frame ``backend`` returns."""

    out.mkdir(parents=True, exist_ok=True)

    def wrap(name: str) -> None:
        original = getattr(backend, name, None)
        if not callable(original):
            return

        def frame(*args: Any, **kwargs: Any) -> bytes:
            png = original(*args, **kwargs)
            digest = hashlib.sha256(png).hexdigest()
            target = out / f"{digest}.png"
            error_path = out / f"{digest}.error"
            if not target.exists():
                try:
                    target.write_bytes(capture_scaled(backend.page, scale))
                    error_path.unlink(missing_ok=True)
                except Exception as error:  # display only: never fail the run
                    error_path.write_text(f"{type(error).__name__}\n")
            return png

        setattr(backend, name, frame)

    # A guarded actuation frame can be saved as a step's before frame without
    # passing through screenshot(). Both entry points retain their exact bytes.
    for name in ("screenshot", "guarded_keyboard_frame"):
        wrap(name)


def _tile_diff(frame: bytes, copy: bytes) -> float:
    """The largest mean absolute difference of any tile, copy downscaled."""

    from PIL import Image, ImageChops, ImageStat

    one = Image.open(io.BytesIO(frame)).convert("L")
    big = Image.open(io.BytesIO(copy)).convert("L").resize(one.size, Image.LANCZOS)
    diff = ImageChops.difference(one, big)
    worst = 0.0
    for top in range(0, one.size[1], TILE):
        for left in range(0, one.size[0], TILE):
            box = (
                left,
                top,
                min(left + TILE, one.size[0]),
                min(top + TILE, one.size[1]),
            )
            worst = max(worst, float(ImageStat.Stat(diff.crop(box)).mean[0]))
    return worst


def pair(
    run_dir: Path, copies: Path, out: Path, scale: int = 2, *, strict: bool = True
) -> dict[str, Any]:
    """Write ``out/<name>@<scale>x.png`` for each frame in ``run_dir/steps``.

    Returns a summary: the pairs written (frame, copy, both hashes, sizes,
    difference) and the frames left without a copy, with the reason. With
    ``strict`` (the default), any frame left without a copy raises after
    ``pairs.json`` is written.
    """

    from PIL import Image

    out.mkdir(parents=True, exist_ok=True)
    pairs, missing = [], []
    for frame_path in sorted((run_dir / "steps").glob("*.png")):
        frame = frame_path.read_bytes()
        digest = hashlib.sha256(frame).hexdigest()
        source = copies / f"{digest}.png"
        if not source.exists():
            error = copies / f"{digest}.error"
            reason = (
                f"capture failed: {error.read_text().strip()}"
                if error.exists()
                else "no copy"
            )
            missing.append({"frame": frame_path.name, "reason": reason})
            continue
        copy = source.read_bytes()
        size = Image.open(io.BytesIO(frame)).size
        copy_size = Image.open(io.BytesIO(copy)).size
        if copy_size != (size[0] * scale, size[1] * scale):
            missing.append({"frame": frame_path.name, "reason": f"size {copy_size}"})
            continue
        diff = _tile_diff(frame, copy)
        if diff > MAX_TILE_DIFF:
            missing.append({"frame": frame_path.name, "reason": f"differs {diff:.2f}"})
            continue
        name = f"{frame_path.stem}@{scale}x.png"
        (out / name).write_bytes(copy)
        pairs.append(
            {
                "frame": f"steps/{frame_path.name}",
                "frame_sha256": digest,
                "copy": name,
                "copy_sha256": hashlib.sha256(copy).hexdigest(),
                "copy_size": list(copy_size),
                "max_tile_diff": round(diff, 3),
            }
        )
    summary = {"scale": scale, "pairs": pairs, "missing": missing}
    (out / "pairs.json").write_text(json.dumps(summary, indent=2) + "\n")
    if missing and strict:
        raise ValueError(f"{run_dir}: frames without a matching copy: {missing}")
    return summary
