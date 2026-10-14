"""Generate an animated WebP of the benchmark viewer for README.

This script:
1. Generates a compact benchmark viewer HTML (with embedded screenshots).
2. Uses Playwright to capture frames showing the overview, task selection,
   and step-by-step screenshot replay.
3. Assembles frames into a lossless animated WebP.

Usage:
    uv run python scripts/generate_viewer_animation.py \
        --benchmark-dir benchmark_results/phase0_multi_domain_v3 \
        --output animations/benchmark-viewer.webp

Requirements:
    pip install playwright pillow
    python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openadapt_evals.benchmarks.viewer import generate_benchmark_viewer

logger = logging.getLogger(__name__)

# Animation settings
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800
FRAME_DURATION_MS = 2500  # 2.5s per frame
STEP_DURATION_MS = 1500   # 1.5s per step screenshot
TRANSITION_PAUSE_MS = 800  # brief pause for transitions


def capture_frames(
    html_path: Path,
    benchmark_dir: Path,
    output_dir: Path,
    viewport_width: int = VIEWPORT_WIDTH,
    viewport_height: int = VIEWPORT_HEIGHT,
) -> list[Path]:
    """Capture animation frames from the viewer using Playwright.

    Args:
        html_path: Path to the generated HTML viewer.
        benchmark_dir: Path to benchmark results for context.
        output_dir: Directory to save frame PNGs.
        viewport_width: Browser viewport width.
        viewport_height: Browser viewport height.

    Returns:
        List of paths to captured frame PNGs.
    """
    frames: list[Path] = []
    frame_idx = 0

    def save_frame(page, label: str = "") -> Path:
        nonlocal frame_idx
        path = output_dir / f"frame_{frame_idx:03d}.png"
        page.screenshot(path=str(path))
        logger.info(f"Frame {frame_idx}: {label}")
        frame_idx += 1
        frames.append(path)
        return path

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": viewport_width, "height": viewport_height},
        )

        # Load the viewer
        page.goto(f"file://{html_path.resolve()}")
        page.wait_for_load_state("networkidle")
        time.sleep(0.5)

        # Frame 1: Overview (task list visible, no task selected)
        save_frame(page, "overview - task list")

        # Get number of tasks from the page
        task_count = page.locator(".task-item").count()
        logger.info(f"Found {task_count} tasks in viewer")

        # For each task, select it, show overview, then cycle through
        # a few steps
        tasks_to_show = min(task_count, 5)

        for task_idx in range(tasks_to_show):
            # Click on the task
            task_item = page.locator(".task-item").nth(task_idx)
            task_item.click()
            page.wait_for_timeout(600)

            # Frame: task selected, showing detail header + screenshot
            save_frame(page, f"task {task_idx} - detail view")

            # Cycle through a few steps for this task
            step_count_text = page.locator("#step-progress").text_content() or "0 / 0"
            parts = step_count_text.split("/")
            total_steps = int(parts[1].strip()) if len(parts) == 2 else 0

            # Show up to 3 steps per task (first, middle, last)
            if total_steps > 1:
                steps_to_show = [0]
                if total_steps > 2:
                    steps_to_show.append(total_steps // 2)
                steps_to_show.append(total_steps - 1)
                # Remove duplicates while preserving order
                seen = set()
                steps_to_show = [s for s in steps_to_show if not (s in seen or seen.add(s))]

                for step in steps_to_show:
                    if step == 0:
                        continue  # Already showing step 0
                    # Click "Next" button to advance to the step
                    next_btn = page.locator("button:has-text('Next')")
                    clicks_needed = step - (steps_to_show[steps_to_show.index(step) - 1] if steps_to_show.index(step) > 0 else 0)
                    for _ in range(clicks_needed):
                        next_btn.click()
                        page.wait_for_timeout(300)

                    save_frame(page, f"task {task_idx} - step {step}/{total_steps}")

        browser.close()

    return frames


def assemble_animation(
    frame_paths: list[Path],
    output_path: Path,
    frame_duration_ms: int = FRAME_DURATION_MS,
) -> None:
    """Assemble frames into an animated WebP.

    Args:
        frame_paths: List of paths to frame PNG files.
        output_path: Output path for the animated WebP.
        frame_duration_ms: Duration per frame in milliseconds.
    """
    if not frame_paths:
        raise ValueError("No frames to assemble")

    images = [Image.open(p) for p in frame_paths]

    # Save as animated WebP.
    # Use high quality lossy encoding (quality=90) for a good balance between
    # file size and visual fidelity. Lossless WebP at 1280x800 is ~2MB which
    # is heavy for a README. At quality=90, typical output is ~700-900KB with
    # near-lossless appearance (far better than GIF's 256-color palette).
    output_path.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        str(output_path),
        format="WEBP",
        save_all=True,
        append_images=images[1:],
        duration=frame_duration_ms,
        loop=0,
        quality=90,
    )

    size_kb = output_path.stat().st_size / 1024
    logger.info(
        f"Generated animation: {output_path} "
        f"({len(images)} frames, {size_kb:.0f} KB)"
    )


def main() -> None:
    """Generate benchmark viewer animation."""
    parser = argparse.ArgumentParser(
        description="Generate an animated WebP of the benchmark viewer"
    )
    parser.add_argument(
        "--benchmark-dir",
        type=Path,
        default=Path("benchmark_results/phase0_multi_domain_v3"),
        help="Path to benchmark results directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("animations/benchmark-viewer.webp"),
        help="Output path for animated WebP",
    )
    parser.add_argument(
        "--frame-duration",
        type=int,
        default=FRAME_DURATION_MS,
        help="Duration per frame in ms (default: 2500)",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=VIEWPORT_WIDTH,
        help="Viewport width (default: 1280)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=VIEWPORT_HEIGHT,
        help="Viewport height (default: 800)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Resolve benchmark dir
    benchmark_dir = args.benchmark_dir.resolve()
    if not benchmark_dir.exists():
        logger.error(f"Benchmark directory not found: {benchmark_dir}")
        sys.exit(1)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Step 1: Generate compact viewer with embedded screenshots
        logger.info("Generating compact viewer HTML...")
        html_path = tmpdir_path / "viewer.html"
        generate_benchmark_viewer(
            benchmark_dir=benchmark_dir,
            output_path=html_path,
            embed_screenshots=True,
            compact=True,
        )
        logger.info(f"Viewer HTML: {html_path} ({html_path.stat().st_size / 1024:.0f} KB)")

        # Step 2: Capture frames
        logger.info("Capturing frames with Playwright...")
        frames_dir = tmpdir_path / "frames"
        frames_dir.mkdir()
        frame_paths = capture_frames(
            html_path, benchmark_dir, frames_dir,
            viewport_width=args.width,
            viewport_height=args.height,
        )
        logger.info(f"Captured {len(frame_paths)} frames")

        # Step 3: Assemble animation
        logger.info("Assembling animated WebP...")
        output_path = args.output.resolve()
        assemble_animation(frame_paths, output_path, args.frame_duration)

    logger.info("Done!")


if __name__ == "__main__":
    main()
