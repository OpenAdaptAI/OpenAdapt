#!/usr/bin/env python3
"""Regenerate docs/images/demo_viewer.png, the screenshot in the README.

The README shows the output of `openadapt-viewer demo`, so this script runs
that command and photographs the result rather than mocking up a page. The
demo's pass/fail data is unseeded, so a regenerated image will not match the
committed one task for task. That is expected; the point is that the layout,
the controls and the chrome in the README are what the tool actually emits.

    uv run python scripts/generate_demo_screenshot.py

Needs a browser (`uv run playwright install chromium`) and, because the
generated page pulls Alpine.js from a CDN, a network connection. Without
Alpine the task click below does nothing and the detail panel stays empty.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "images" / "demo_viewer.png"

# Wide enough for the four summary cards to sit on one row, which is how the
# viewer is meant to be read.
VIEWPORT_WIDTH = 1100
SELECTED_TASK = "task_003"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", "-o", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--tasks", "-n", type=int, default=10)
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed: uv sync --all-extras", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        page_path = Path(tmp) / "viewer.html"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "openadapt_viewer.cli",
                "demo",
                "--tasks",
                str(args.tasks),
                "--output",
                str(page_path),
            ],
            check=True,
        )

        args.output.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(
                viewport={"width": VIEWPORT_WIDTH, "height": 900},
                device_scale_factor=2,
            )
            page.goto(page_path.as_uri())
            # Alpine is fetched from the CDN; give it time to hydrate.
            page.wait_for_timeout(3000)
            page.get_by_text(SELECTED_TASK, exact=True).first.click()
            page.wait_for_timeout(1500)
            page.screenshot(path=str(args.output), full_page=True)
            browser.close()

    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
