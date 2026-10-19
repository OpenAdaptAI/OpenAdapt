#!/usr/bin/env python3
"""Render app icons and the macOS DMG window from the OpenAdapt robot mark.

The v0.15.0 DMG shipped a green rounded square with a bar. Finder read that
as a minus. This script paints the website robot mark onto a full-bleed ink
square. macOS applies the squircle mask; do not bake one. It also paints a
drag-to-Applications DMG background.

Requires Pillow. macOS ``iconutil`` writes ``icon.icns``. Optional
``rsvg-convert`` rasterizes the SVG mark at the target size.

Brand mark: ``src-tauri/icons/brand/openadapt-mark.png`` copied from
``openadapt-web`` ``public/apple-touch-icon.png``, plus
``openadapt-mark.svg`` from ``public/images/favicon.svg``.

Design tokens: ``--ink`` #0B1220, ``--inset-text`` #E2E8F0.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / "src-tauri" / "icons"
MARK = ICONS / "brand" / "openadapt-mark.png"
MARK_SVG = ICONS / "brand" / "openadapt-mark.svg"
DMG_BG = ROOT / "src-tauri" / "dmg" / "background.png"

# Canonical tokens from src/styles/vendor/openadapt-web/tokens.json.
INK = (11, 18, 32, 255)  # --ink #0B1220
INSET_TEXT = (226, 232, 240, 255)  # --inset-text #E2E8F0
INSET_MUTED = (226, 232, 240, 170)
FORBIDDEN_PNG_SHA256 = "bfe0288bd6f672e2883e771ae509769f25e704bb"

ICONSET_MAP = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}

# Tauri DMG defaults (bundle.macOS.dmg).
DMG_SIZE = (660, 400)
APP_POSITION = (180, 170)
APPLICATION_FOLDER_POSITION = (480, 170)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_mark(min_px: int) -> Image.Image:
    """Load the robot mark as RGBA, preferring a sharp SVG raster."""
    rsvg = shutil.which("rsvg-convert")
    if MARK_SVG.is_file() and rsvg:
        with tempfile.TemporaryDirectory(prefix="oa-mark-") as raw:
            out = Path(raw) / "mark.png"
            subprocess.run(
                [
                    rsvg,
                    "-w",
                    str(min_px),
                    "-h",
                    str(min_px),
                    "-o",
                    str(out),
                    str(MARK_SVG),
                ],
                check=True,
            )
            return Image.open(out).convert("RGBA")
    return Image.open(MARK).convert("RGBA")


def robot_layer(size: int) -> Image.Image:
    """Return a size x size luminance mask of the robot glyph."""
    mark = load_mark(max(size * 2, 512))
    # Flatten onto white so a transparent SVG and an opaque PNG share one path.
    flat = Image.new("RGBA", mark.size, (255, 255, 255, 255))
    flat.alpha_composite(mark)
    glyph = ImageOps.invert(flat.convert("L"))
    box = glyph.getbbox()
    if box:
        glyph = glyph.crop(box)
    pad = int(size * 0.18)
    inner = max(1, size - pad * 2)
    fitted = ImageOps.contain(glyph, (inner, inner), Image.Resampling.LANCZOS)
    layer = Image.new("L", (size, size), 0)
    x = (size - fitted.width) // 2
    y = (size - fitted.height) // 2
    layer.paste(fitted, (x, y))
    return layer


def render_app_icon(size: int) -> Image.Image:
    """Full-bleed ink square. Do not bake a squircle; the OS applies the mask."""
    img = Image.new("RGBA", (size, size), INK)
    robot = robot_layer(size)
    glyph = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    glyph.putalpha(robot)
    img.alpha_composite(glyph)
    return img


def write_png(path: Path, image: Image.Image) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")


def render_icns() -> None:
    iconutil = shutil.which("iconutil")
    if not iconutil:
        raise SystemExit("iconutil is required on macOS to write icon.icns")
    with tempfile.TemporaryDirectory(prefix="oa-iconset-") as raw:
        iconset = Path(raw) / "OpenAdapt.iconset"
        iconset.mkdir()
        for name, size in ICONSET_MAP.items():
            render_app_icon(size).save(iconset / name, "PNG")
        out = ICONS / "icon.icns"
        subprocess.run(
            [iconutil, "-c", "icns", "-o", str(out), str(iconset)],
            check=True,
        )


def render_ico() -> None:
    # PIL writes one ICO image per `sizes` entry by downsampling this master.
    # Saving a 16px image with append_images produced a 16px-only ICO.
    sizes = ((16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256))
    ICONS.mkdir(parents=True, exist_ok=True)
    render_app_icon(256).save(ICONS / "icon.ico", format="ICO", sizes=sizes)


def _font(size: int) -> ImageFont.ImageFont:
    candidates: list[tuple[str, int | None]] = [
        ("/System/Library/Fonts/Avenir Next.ttc", 0),
        ("/System/Library/Fonts/SFNS.ttf", None),
        ("/System/Library/Fonts/Helvetica.ttc", 0),
        ("/System/Library/Fonts/Supplemental/Arial.ttf", None),
    ]
    for path, index in candidates:
        try:
            if index is None:
                return ImageFont.truetype(path, size=size)
            return ImageFont.truetype(path, size=size, index=index)
        except OSError:
            continue
    return ImageFont.load_default()


def dmg_background() -> Image.Image:
    """Ink plate, wordmark, drag hint, arrow. No empty slots under the icons."""
    width, height = DMG_SIZE
    img = Image.new("RGBA", (width, height), INK)
    draw = ImageDraw.Draw(img)
    font = _font(22)
    small = _font(13)
    draw.text((330, 40), "OpenAdapt Desktop", fill=INSET_TEXT, font=font, anchor="mt")
    draw.text((330, 68), "Drag to Applications", fill=INSET_MUTED, font=small, anchor="mt")
    left_x, y = APP_POSITION
    right_x, _ = APPLICATION_FOLDER_POSITION
    # Leave room for the ~128px Finder icons centered on those points.
    start = (left_x + 72, y)
    end = (right_x - 72, y)
    draw.line((start, end), fill=INSET_MUTED, width=3)
    draw.polygon(
        [
            (end[0], end[1]),
            (end[0] - 14, end[1] - 8),
            (end[0] - 14, end[1] + 8),
        ],
        fill=INSET_MUTED,
    )
    robot = robot_layer(36)
    badge = Image.new("RGBA", (36, 36), (255, 255, 255, 255))
    badge.putalpha(robot)
    img.alpha_composite(badge, (330 - 18, height - 56))
    return img.convert("RGB")


def main() -> None:
    if not MARK.is_file():
        raise SystemExit(f"missing brand mark: {MARK}")
    master = render_app_icon(1024)
    write_png(ICONS / "icon.png", master)
    digest = sha256_file(ICONS / "icon.png")
    if digest.startswith(FORBIDDEN_PNG_SHA256):
        raise SystemExit("icon.png is still the minus-bar placeholder")
    write_png(ICONS / "32x32.png", render_app_icon(32))
    write_png(ICONS / "64x64.png", render_app_icon(64))
    write_png(ICONS / "128x128.png", render_app_icon(128))
    write_png(ICONS / "128x128@2x.png", render_app_icon(256))
    write_png(ICONS / "icon-512.png", render_app_icon(512))
    write_png(ICONS / "icon-1024.png", master)
    render_ico()
    render_icns()
    write_png(DMG_BG, dmg_background())
    print("wrote", ICONS / "icon.icns")
    print("wrote", DMG_BG)
    print("icon.png sha256", digest)


if __name__ == "__main__":
    main()
