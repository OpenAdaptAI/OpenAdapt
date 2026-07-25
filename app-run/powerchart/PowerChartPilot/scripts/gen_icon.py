#!/usr/bin/env python3
"""Generate PowerChart Pilot app icon (1024² PNG + .icns)."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 1024
RADIUS = 200
BG = (18, 72, 92, 255)       # deep teal (clinical / chart feel)
BG_HI = (48, 140, 150, 255)
FG = (245, 248, 250, 255)
ACCENT = (232, 168, 74, 255)  # warm amber mark


def make_png(out: Path) -> None:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    base = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    bd = ImageDraw.Draw(base)
    bd.rounded_rectangle((0, 0, SIZE, SIZE), radius=RADIUS, fill=BG)

    sheen = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sheen)
    sd.polygon([(0, 0), (SIZE, 0), (0, SIZE)], fill=(*BG_HI[:3], 90))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, SIZE, SIZE), radius=RADIUS, fill=255)
    base = Image.composite(Image.alpha_composite(base, sheen), base, mask)

    draw = ImageDraw.Draw(base)
    # Chart bars
    for i, h in enumerate((280, 420, 340, 520, 380)):
        x0 = 220 + i * 120
        y0 = 720 - h
        draw.rounded_rectangle(
            (x0, y0, x0 + 80, 720), radius=18, fill=(*FG[:3], 230)
        )
    # Accent "play / pilot" chevron
    draw.polygon([(640, 260), (820, 380), (640, 500)], fill=ACCENT)

    # Letter mark
    try:
        font = ImageFont.truetype("/System/Library/Fonts/SFNSRounded.ttf", 220)
    except OSError:
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 220)
        except OSError:
            font = ImageFont.load_default()
    draw.text((90, 120), "PC", fill=FG, font=font)

    out.parent.mkdir(parents=True, exist_ok=True)
    base.save(out)
    print(f"wrote {out}")


def make_icns(png: Path, icns: Path) -> None:
    """Build a multi-resolution .icns via iconutil."""
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        sizes = [
            (16, "icon_16x16.png"),
            (32, "icon_16x16@2x.png"),
            (32, "icon_32x32.png"),
            (64, "icon_32x32@2x.png"),
            (128, "icon_128x128.png"),
            (256, "icon_128x128@2x.png"),
            (256, "icon_256x256.png"),
            (512, "icon_256x256@2x.png"),
            (512, "icon_512x512.png"),
            (1024, "icon_512x512@2x.png"),
        ]
        src = Image.open(png).convert("RGBA")
        for px, name in sizes:
            src.resize((px, px), Image.Resampling.LANCZOS).save(iconset / name)
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
            check=True,
        )
    print(f"wrote {icns}")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    assets = root / "assets"
    png = assets / "icon-source.png"
    icns = assets / "AppIcon.icns"
    make_png(png)
    make_icns(png, icns)


if __name__ == "__main__":
    main()
