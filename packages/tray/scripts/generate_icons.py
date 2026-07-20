#!/usr/bin/env python3
"""Generate OpenAdapt Tray state icons from the OpenAdapt mark.

Every tray icon is the OpenAdapt mark (the chat/robot face) tinted with a
per-state colour, so the state stays readable at a glance while the brand mark
replaces the old plain coloured circle.

Source of truth
---------------
* ``assets/mark.svg``        -- the vector mark (from openadapt.ai favicon).
* ``assets/mark-master.png`` -- a 1024px transparent raster master rendered from
  the SVG. This is the canonical silhouette used for tinting.
* ``src/openadapt_tray/assets/mark-master.png`` -- a 512px copy packaged inside
  the wheel so the installed app can tint the mark at runtime.

This script re-renders ``mark-master.png`` from ``mark.svg`` when a rasterizer
(``rsvg-convert``, ``cairosvg`` or ``inkscape``) is available, then tints it
into per-state PNGs (1x + @2x) and a Windows ``logo.ico``. Tinting itself is
pure Pillow and uses the exact same implementation as the runtime
(``IconManager.tint_mark``), so committed and runtime icons never diverge.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

# Make the package importable so we reuse the runtime tinting implementation.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from openadapt_tray.icons import IconManager  # noqa: E402
from openadapt_tray.state import TrayState  # noqa: E402

ASSETS = REPO_ROOT / "assets"
ICONS_DIR = ASSETS / "icons"
MARK_SVG = ASSETS / "mark.svg"
MARK_MASTER = ASSETS / "mark-master.png"
PACKAGED_MASTER = REPO_ROOT / "src" / "openadapt_tray" / "assets" / "mark-master.png"

MASTER_SIZE = 1024
PACKAGED_MASTER_SIZE = 512
BASE_SIZE = 64  # 1x tray icon; @2x is doubled.
ICO_SIZES = [16, 32, 48, 64, 128, 256]

# Filename stem per state (matches IconManager.STATE_ICONS).
STATE_STEMS = {
    TrayState.IDLE: "idle",
    TrayState.RECORDING_STARTING: "recording_starting",
    TrayState.RECORDING: "recording",
    TrayState.RECORDING_STOPPING: "recording_stopping",
    TrayState.COMPILING: "compiling",
    TrayState.ERROR: "error",
}


def _render_svg_to_png(svg: Path, out: Path, size: int) -> bool:
    """Render ``svg`` to a transparent ``size`` x ``size`` PNG.

    Tries rsvg-convert, then cairosvg, then inkscape. Returns True on success.
    """
    if shutil.which("rsvg-convert"):
        subprocess.run(
            ["rsvg-convert", "-w", str(size), "-h", str(size),
             str(svg), "-o", str(out)],
            check=True,
        )
        return True
    try:
        import cairosvg  # type: ignore

        cairosvg.svg2png(
            url=str(svg), write_to=str(out), output_width=size, output_height=size
        )
        return True
    except Exception:
        pass
    if shutil.which("inkscape"):
        subprocess.run(
            ["inkscape", str(svg), "-w", str(size), "-h", str(size),
             "--export-filename", str(out)],
            check=True,
        )
        return True
    return False


def _trim_square(img: Image.Image, margin_frac: float = 0.08) -> Image.Image:
    """Trim to the mark bbox and pad to a centred square with a small margin."""
    img = img.convert("RGBA")
    bbox = img.split()[3].getbbox()
    if bbox:
        img = img.crop(bbox)
    w, h = img.size
    side = max(w, h)
    margin = int(side * margin_frac)
    canvas = side + margin * 2
    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    out.paste(img, ((canvas - w) // 2, (canvas - h) // 2), img)
    return out


def regenerate_master() -> None:
    """Re-render the mark master from the SVG when a rasterizer is available."""
    if not MARK_SVG.exists():
        print(f"  mark.svg not found at {MARK_SVG}; keeping committed master.")
        return
    tmp = MARK_MASTER.with_suffix(".raw.png")
    if not _render_svg_to_png(MARK_SVG, tmp, MASTER_SIZE):
        print("  No SVG rasterizer found; keeping committed mark-master.png.")
        return
    master = _trim_square(Image.open(tmp)).resize(
        (MASTER_SIZE, MASTER_SIZE), Image.LANCZOS
    )
    master.save(MARK_MASTER)
    tmp.unlink(missing_ok=True)
    print(f"  Re-rendered {MARK_MASTER.name} from {MARK_SVG.name}")


def sync_packaged_master() -> None:
    """Copy a downscaled master into the package so it ships in the wheel."""
    PACKAGED_MASTER.parent.mkdir(parents=True, exist_ok=True)
    master = Image.open(MARK_MASTER).convert("RGBA")
    master.resize(
        (PACKAGED_MASTER_SIZE, PACKAGED_MASTER_SIZE), Image.LANCZOS
    ).save(PACKAGED_MASTER)
    print(f"  Synced packaged master -> {PACKAGED_MASTER.relative_to(REPO_ROOT)}")


def generate_state_icons() -> None:
    """Tint the mark per state into 1x + @2x PNGs under assets/icons."""
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    manager = IconManager()
    for state, stem in STATE_STEMS.items():
        manager.render_state_icon(state, BASE_SIZE).save(ICONS_DIR / f"{stem}.png")
        manager.render_state_icon(state, BASE_SIZE * 2).save(
            ICONS_DIR / f"{stem}@2x.png"
        )
        print(f"  Created {stem}.png + {stem}@2x.png")


def generate_ico() -> None:
    """Write a multi-size Windows .ico using the brand-blue idle mark.

    Pillow resamples the single base image down to every size in ``sizes``, so
    the base must be rendered at the largest size for a crisp multi-res .ico.
    """
    manager = IconManager()
    base = manager.render_state_icon(TrayState.IDLE, max(ICO_SIZES))
    ico_path = ASSETS / "logo.ico"
    base.save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
    )
    print(f"  Created {ico_path.name}")


def main() -> None:
    """Regenerate the master, packaged master, state icons and .ico."""
    print(f"Generating OpenAdapt mark icons in {ICONS_DIR}")
    regenerate_master()
    sync_packaged_master()
    generate_state_icons()
    generate_ico()
    print("Done!")


if __name__ == "__main__":
    main()
