"""Render the README proof image from a real ``openadapt-flow demo`` run.

No mockups. The script runs the demo (the bundled tutorial against the
synthetic MockMed app, once against an honest backend and once against a
backend that drops the save after showing success), then screenshots the top
of the proof page the demo wrote: the provenance label, the headline, both
run cards with their final screens and record checks, and the
identical-screens note.

Usage:
    python scripts/make_demo_proof_image.py --out docs/showcase/demo-proof.png
    python scripts/make_demo_proof_image.py --from-dir openadapt-demo \\
        --out docs/showcase/demo-proof.png

``--from-dir`` reuses the page from an earlier ``openadapt-flow demo --out``
folder instead of running the demo again. Requires the ``browser`` extra.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

#: CSS width of the page when it's captured. Wider than the demo page's
#: one-column breakpoint (640 px) so both cards sit side by side, and close to
#: the width GitHub gives a README image.
VIEWPORT_WIDTH = 900
#: Pixel density of the capture, so text stays sharp when GitHub scales it.
DEVICE_SCALE = 2
#: Space kept below the identical-screens note, in CSS pixels.
BOTTOM_MARGIN = 28
#: Presentation-only changes for the still image.
IMAGE_ONLY_CSS = (
    ".lede, .shot .full-link { display: none; } h1 { margin-bottom: 28px; }"
)


def _run_demo(out_dir: Path) -> Path:
    from openadapt_flow.__main__ import main

    code = main(["demo", "--out", str(out_dir), "--no-open"])
    if code != 0:
        raise SystemExit(
            f"openadapt-flow demo exited {code}; refusing to publish an image "
            "of a demo that didn't show the difference."
        )
    return out_dir / "index.html"


def _capture(page_path: Path, out: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(
                viewport={"width": VIEWPORT_WIDTH, "height": 1000},
                device_scale_factor=DEVICE_SCALE,
                color_scheme="light",
            )
            page.goto(page_path.resolve().as_uri())
            # An image can't follow links, and the README states the setup in
            # its own words, so drop the lede paragraph and the full-screen
            # links. Every result, count, and frame stays as the demo wrote it.
            page.add_style_tag(content=IMAGE_ONLY_CSS)
            bottom = page.evaluate(
                "() => { const r = document.querySelector('.callout')"
                ".getBoundingClientRect(); return r.bottom + window.scrollY; }"
            )
            page.screenshot(
                path=str(out),
                clip={
                    "x": 0,
                    "y": 0,
                    "width": VIEWPORT_WIDTH,
                    "height": float(bottom) + BOTTOM_MARGIN,
                },
                full_page=True,
            )
        finally:
            browser.close()


def _shrink(path: Path) -> None:
    """Re-encode as an adaptive-palette PNG; the page is flat UI colors."""

    from PIL import Image

    with Image.open(path) as image:
        palette = image.convert("RGB").quantize(colors=256, dither=Image.Dither.NONE)
    palette.save(path, optimize=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="PNG path to write")
    parser.add_argument(
        "--from-dir",
        default=None,
        help="An earlier `openadapt-flow demo --out` folder to capture instead",
    )
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.from_dir:
        page_path = Path(args.from_dir) / "index.html"
        if not page_path.is_file():
            raise SystemExit(f"{page_path} doesn't exist")
        _capture(page_path, out)
    else:
        with tempfile.TemporaryDirectory(prefix="openadapt-demo-image-") as tmp:
            page_path = _run_demo(Path(tmp) / "demo")
            _capture(page_path, out)
    _shrink(out)
    print(f"Wrote {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
