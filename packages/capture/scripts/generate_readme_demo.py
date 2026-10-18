#!/usr/bin/env python3
"""Generate demo GIF and HTML viewer from a real capture for README.

This script performs an actual capture and generates:
1. An animated GIF with event overlays for README embedding
2. An interactive HTML viewer for detailed exploration

Usage:
    uv run python scripts/generate_readme_demo.py --duration 10

Note: Requires accessibility permissions for input capture.
"""

import argparse
import shutil
import tempfile
import time
from pathlib import Path


def run_capture(duration: float, capture_dir: Path) -> None:
    """Run a real capture.

    Args:
        duration: Capture duration in seconds.
        capture_dir: Directory to save capture.
    """
    from openadapt_capture import Recorder

    print(f"Starting {duration}s capture...")
    print("Move your mouse, click, and type to generate events!")
    print()

    with Recorder(
        capture_dir,
        task_description="README demo capture",
        capture_video=True,
        capture_mouse_moves=False,  # Too noisy for demo
    ) as recorder:
        start = time.time()
        while time.time() - start < duration:
            elapsed = time.time() - start
            remaining = duration - elapsed
            print(f"\r  Recording... {remaining:.1f}s remaining, {recorder.event_count} events", end="", flush=True)
            time.sleep(0.1)
        print()

    print(f"Captured {recorder.event_count} events")


def generate_demo_gif(capture_dir: Path, output_path: Path) -> None:
    """Generate demo GIF from capture.

    Args:
        capture_dir: Path to capture directory.
        output_path: Output path for GIF.
    """
    from openadapt_capture.visualize import create_demo

    print("Generating demo GIF...")
    create_demo(
        capture_dir,
        output=output_path,
        fps=8,
        max_duration=15,
        show_events=True,
        show_labels=True,
        show_timestamp=True,
        scale=0.5,
    )
    print(f"  Saved: {output_path}")


def generate_html_viewer(capture_dir: Path, output_path: Path) -> None:
    """Generate HTML viewer from capture.

    Args:
        capture_dir: Path to capture directory.
        output_path: Output path for HTML.
    """
    from openadapt_capture.visualize import create_html

    print("Generating HTML viewer...")
    create_html(
        capture_dir,
        output=output_path,
        max_events=100,
        include_audio=True,
        frame_scale=0.75,
    )
    print(f"  Saved: {output_path}")


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Generate README demo")
    parser.add_argument(
        "--duration", "-d",
        type=float,
        default=10.0,
        help="Capture duration in seconds (default: 10)",
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=str,
        default=None,
        help="Output directory (default: docs/images)",
    )
    parser.add_argument(
        "--keep-capture",
        action="store_true",
        help="Keep the capture directory after generating outputs",
    )
    args = parser.parse_args()

    # Determine output directory
    script_dir = Path(__file__).parent
    project_dir = script_dir.parent
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = project_dir / "docs" / "images"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Create temp capture directory
    capture_dir = Path(tempfile.mkdtemp(prefix="readme_demo_"))

    print("=" * 60)
    print("README Demo Generator")
    print("=" * 60)
    print()

    try:
        # Run capture
        run_capture(args.duration, capture_dir)
        print()

        # Generate outputs
        generate_demo_gif(capture_dir, output_dir / "demo.gif")
        generate_html_viewer(capture_dir, output_dir / "viewer.html")

        print()
        print("Done!")
        print()
        print("Add to README.md:")
        print()
        print("```markdown")
        print("## Demo")
        print()
        print("![Recording Demo](docs/images/demo.gif)")
        print()
        print("For interactive exploration, open [viewer.html](docs/images/viewer.html).")
        print("```")

    finally:
        # Cleanup
        if not args.keep_capture:
            print()
            print(f"Cleaning up {capture_dir}...")
            shutil.rmtree(capture_dir, ignore_errors=True)
        else:
            print()
            print(f"Capture saved at: {capture_dir}")


if __name__ == "__main__":
    main()
