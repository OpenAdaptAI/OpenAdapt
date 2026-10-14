#!/usr/bin/env python3
"""Generate screenshots of openadapt-viewer for README documentation.

This script:
1. Loads recorded captures from openadapt-capture
2. Generates interactive HTML viewers using openadapt-viewer
3. Takes screenshots of the HTML using Playwright
4. Saves screenshots to docs/images/

Captures used:
- turn-off-nightshift: 22 screenshots showing macOS Night Shift workflow
- demo_new: 14 screenshots of demo workflow
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

# Add parent directory to path for imports
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


class ScreenshotGenerationError(Exception):
    """Base exception for screenshot generation errors."""



def check_dependencies() -> dict[str, bool]:
    """Check if required dependencies are available.

    Returns:
        Dict mapping dependency names to availability status.
    """
    deps = {}

    # Check openadapt_capture
    try:
        import openadapt_capture  # noqa: F401

        deps["openadapt_capture"] = True
    except ImportError as e:
        deps["openadapt_capture"] = False
        print(f"Warning: openadapt_capture not available: {e}")

    # Check playwright
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401

        deps["playwright"] = True
    except ImportError as e:
        deps["playwright"] = False
        print(f"Warning: playwright not available: {e}")

    return deps


def generate_viewer_html(
    capture_path: Path,
    output_path: Path,
    max_events: int | None = None,
) -> Path:
    """Generate HTML viewer from a capture directory.

    Args:
        capture_path: Path to capture directory with capture.db
        output_path: Where to save the generated HTML
        max_events: Maximum events to include (None for all)

    Returns:
        Path to generated HTML file

    Raises:
        ScreenshotGenerationError: If HTML generation fails
    """
    try:
        from openadapt_capture.capture import CaptureSession
        from openadapt_capture.visualize.html import create_html
    except ImportError as e:
        raise ScreenshotGenerationError(
            f"Failed to import openadapt_capture: {e}\n"
            "Install with: cd ../openadapt-capture && uv pip install -e ."
        ) from e

    try:
        print(f"Loading capture from: {capture_path}")
        capture = CaptureSession.load(capture_path)
        print(f"  - Capture ID: {capture.id}")
        print(f"  - Duration: {capture.duration:.2f}s" if capture.duration else "  - Duration: N/A")
        print(f"  - Platform: {capture.platform}")

        # Count actions
        actions = list(capture.actions())
        print(f"  - Total actions: {len(actions)}")

        if max_events and len(actions) > max_events:
            print(f"  - Limiting to {max_events} events")

        print(f"Generating HTML viewer: {output_path}")
        create_html(
            capture,
            output=output_path,
            max_events=max_events,
            include_audio=True,
            frame_scale=0.5,  # Reduce size for faster loading
            frame_quality=75,
        )

        if not output_path.exists():
            raise ScreenshotGenerationError(f"HTML file not created: {output_path}")

        print(f"  - Generated: {output_path} ({output_path.stat().st_size / 1024 / 1024:.1f} MB)")
        return output_path

    except FileNotFoundError as e:
        raise ScreenshotGenerationError(f"Capture not found: {capture_path}\n{e}") from e
    except Exception as e:
        raise ScreenshotGenerationError(f"Failed to generate HTML: {e}") from e
    finally:
        if "capture" in locals():
            capture.close()


def take_screenshot(
    html_path: Path,
    output_path: Path,
    viewport_width: int = 1400,
    viewport_height: int = 900,
    full_page: bool = False,
) -> Path:
    """Take a screenshot of an HTML file using Playwright.

    Args:
        html_path: Path to HTML file
        output_path: Where to save the screenshot
        viewport_width: Browser viewport width
        viewport_height: Browser viewport height
        full_page: Whether to capture full page or just viewport

    Returns:
        Path to screenshot file

    Raises:
        ScreenshotGenerationError: If screenshot fails
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ScreenshotGenerationError(
            f"Playwright not installed: {e}\n"
            "Install with: uv pip install playwright && uv run playwright install chromium"
        ) from e

    try:
        print(f"Taking screenshot: {html_path.name}")
        print(f"  - Viewport: {viewport_width}x{viewport_height}")
        print(f"  - Full page: {full_page}")

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(
                viewport={"width": viewport_width, "height": viewport_height}
            )

            # Load HTML file
            file_url = f"file://{html_path.absolute()}"
            page.goto(file_url, wait_until="networkidle")

            # Wait for content to render
            page.wait_for_timeout(1000)

            # Take screenshot
            output_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output_path), full_page=full_page)

            browser.close()

        if not output_path.exists():
            raise ScreenshotGenerationError(f"Screenshot not created: {output_path}")

        print(f"  - Saved: {output_path} ({output_path.stat().st_size / 1024:.1f} KB)")
        return output_path

    except Exception as e:
        raise ScreenshotGenerationError(f"Failed to take screenshot: {e}") from e


def take_multiple_screenshots(
    html_path: Path,
    output_dir: Path,
    base_name: str,
    scenarios: list[dict[str, Any]],
) -> list[Path]:
    """Take multiple screenshots with different configurations.

    Args:
        html_path: Path to HTML file
        output_dir: Directory to save screenshots
        base_name: Base name for screenshot files
        scenarios: List of scenario configs with keys:
            - suffix: Filename suffix
            - description: Human-readable description
            - viewport_width: Browser width
            - viewport_height: Browser height
            - full_page: Whether to capture full page
            - interact: Optional function to interact with page before screenshot

    Returns:
        List of paths to generated screenshots

    Raises:
        ScreenshotGenerationError: If screenshot fails
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ScreenshotGenerationError(
            f"Playwright not installed: {e}\n"
            "Install with: uv pip install playwright && uv run playwright install chromium"
        ) from e

    screenshots = []

    try:
        print(f"Taking {len(scenarios)} screenshots from: {html_path.name}")

        with sync_playwright() as p:
            browser = p.chromium.launch()

            for i, scenario in enumerate(scenarios, 1):
                suffix = scenario.get("suffix", f"_{i}")
                description = scenario.get("description", f"Screenshot {i}")
                viewport_width = scenario.get("viewport_width", 1400)
                viewport_height = scenario.get("viewport_height", 900)
                full_page = scenario.get("full_page", False)
                interact_fn = scenario.get("interact")

                output_path = output_dir / f"{base_name}{suffix}.png"

                print(f"  [{i}/{len(scenarios)}] {description}")
                print(f"      Viewport: {viewport_width}x{viewport_height}, Full page: {full_page}")

                page = browser.new_page(
                    viewport={"width": viewport_width, "height": viewport_height}
                )

                # Load HTML file
                file_url = f"file://{html_path.absolute()}"
                page.goto(file_url, wait_until="networkidle")

                # Wait for initial render
                page.wait_for_timeout(1000)

                # Custom interaction if provided
                if interact_fn:
                    interact_fn(page)
                    page.wait_for_timeout(500)

                # Take screenshot
                output_path.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output_path), full_page=full_page)

                page.close()

                if not output_path.exists():
                    raise ScreenshotGenerationError(f"Screenshot not created: {output_path}")

                size_kb = output_path.stat().st_size / 1024
                print(f"      Saved: {output_path.name} ({size_kb:.1f} KB)")
                screenshots.append(output_path)

            browser.close()

        return screenshots

    except Exception as e:
        raise ScreenshotGenerationError(f"Failed to take screenshots: {e}") from e


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate screenshots for openadapt-viewer README"
    )
    parser.add_argument(
        "--capture-dir",
        type=Path,
        default=Path(
            os.environ.get("OPENADAPT_CAPTURE_DIR", REPO_ROOT.parent / "openadapt-capture")
        ),
        help=(
            "Path to the openadapt-capture checkout holding the demo captures. "
            "Defaults to a sibling checkout, or $OPENADAPT_CAPTURE_DIR."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "docs" / "images",
        help="Output directory for screenshots",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=50,
        help="Maximum events to include in viewer (default: 50)",
    )
    parser.add_argument(
        "--skip-html",
        action="store_true",
        help="Skip HTML generation (use existing files)",
    )
    parser.add_argument(
        "--skip-screenshots",
        action="store_true",
        help="Skip screenshot generation (only generate HTML)",
    )
    parser.add_argument(
        "--check-deps",
        action="store_true",
        help="Check dependencies and exit",
    )

    args = parser.parse_args()

    # Check dependencies
    deps = check_dependencies()

    if args.check_deps:
        print("\nDependency Status:")
        for dep, available in deps.items():
            status = "✓ Available" if available else "✗ Missing"
            print(f"  {dep}: {status}")
        return 0 if all(deps.values()) else 1

    if not deps.get("openadapt_capture"):
        print("\nError: openadapt_capture is required but not installed")
        print("Install with: cd ../openadapt-capture && uv pip install -e .")
        return 1

    if not args.skip_screenshots and not deps.get("playwright"):
        print("\nError: playwright is required for screenshots")
        print("Install with: uv pip install playwright && uv run playwright install chromium")
        return 1

    # Define captures to process
    captures = [
        {
            "path": args.capture_dir / "turn-off-nightshift",
            "name": "turn-off-nightshift",
            "description": "Turn off Night Shift in macOS System Settings",
        },
        {
            "path": args.capture_dir / "demo_new",
            "name": "demo_new",
            "description": "Demo workflow",
        },
    ]

    # Create output directories
    args.output_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = REPO_ROOT / "temp"
    temp_dir.mkdir(exist_ok=True)

    print(f"\nOutput directory: {args.output_dir}")
    print(f"Temporary directory: {temp_dir}\n")

    generated_html = []
    generated_screenshots = []
    errors = []

    # Step 1: Generate HTML viewers
    if not args.skip_html:
        print("=" * 80)
        print("STEP 1: Generate HTML Viewers")
        print("=" * 80)

        for capture in captures:
            try:
                html_path = temp_dir / f"{capture['name']}_viewer.html"
                generate_viewer_html(
                    capture["path"],
                    html_path,
                    max_events=args.max_events,
                )
                generated_html.append({"capture": capture, "html_path": html_path})
            except ScreenshotGenerationError as e:
                error_msg = f"Failed to generate HTML for {capture['name']}: {e}"
                print(f"\nERROR: {error_msg}\n")
                errors.append(error_msg)
    else:
        print("Skipping HTML generation (--skip-html)")
        # Look for existing HTML files
        for capture in captures:
            html_path = temp_dir / f"{capture['name']}_viewer.html"
            if html_path.exists():
                generated_html.append({"capture": capture, "html_path": html_path})
                print(f"Using existing HTML: {html_path}")
            else:
                error_msg = f"HTML file not found: {html_path}"
                print(f"Warning: {error_msg}")
                errors.append(error_msg)

    # Step 2: Take screenshots
    if not args.skip_screenshots and generated_html:
        print("\n" + "=" * 80)
        print("STEP 2: Take Screenshots")
        print("=" * 80 + "\n")

        for item in generated_html:
            capture = item["capture"]
            html_path = item["html_path"]
            base_name = capture["name"]

            try:
                # Define screenshot scenarios
                scenarios = [
                    {
                        "suffix": "_full",
                        "description": "Full viewer interface",
                        "viewport_width": 1400,
                        "viewport_height": 900,
                        "full_page": False,
                    },
                    {
                        "suffix": "_controls",
                        "description": "Playback controls and timeline",
                        "viewport_width": 1400,
                        "viewport_height": 600,
                        "full_page": False,
                        "interact": lambda page: page.evaluate(
                            "document.querySelector('.viewer-section').scrollIntoView()"
                        ),
                    },
                    {
                        "suffix": "_events",
                        "description": "Event list and details panel",
                        "viewport_width": 800,
                        "viewport_height": 900,
                        "full_page": False,
                        "interact": lambda page: page.evaluate(
                            "document.querySelector('.sidebar').scrollIntoView()"
                        ),
                    },
                ]

                screenshots = take_multiple_screenshots(
                    html_path,
                    args.output_dir,
                    base_name,
                    scenarios,
                )
                generated_screenshots.extend(screenshots)

            except ScreenshotGenerationError as e:
                error_msg = f"Failed to screenshot {capture['name']}: {e}"
                print(f"\nERROR: {error_msg}\n")
                errors.append(error_msg)

    # Print summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"\nGenerated HTML files: {len(generated_html)}")
    for item in generated_html:
        print(f"  - {item['html_path']}")

    print(f"\nGenerated screenshots: {len(generated_screenshots)}")
    for path in generated_screenshots:
        print(f"  - {path}")

    if errors:
        print(f"\nErrors encountered: {len(errors)}")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("\n✓ Screenshot generation completed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
