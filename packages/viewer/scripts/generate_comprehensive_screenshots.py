#!/usr/bin/env python3
"""Generate comprehensive screenshots for all openadapt-viewer HTML files.

This script generates screenshots for:
1. Capture viewer (turn-off-nightshift, demo_new)
2. Segmentation viewer
3. Synthetic demo viewer
4. Benchmark viewer
5. Retrieval viewer

Each viewer gets multiple screenshots showing different states and features.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

# Add parent directory to path for imports
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    from playwright.sync_api import Error as PlaywrightError
except ImportError:
    # playwright is an optional extra; main() refuses to run without it. The
    # alias only keeps this module importable.
    PlaywrightError = Exception

# Scenarios that raised during this process, so main() can exit non-zero.
FAILED_SCENARIOS: list[str] = []


class ScreenshotConfig:
    """Configuration for a single screenshot scenario."""

    def __init__(
        self,
        name: str,
        description: str,
        viewport_width: int = 1400,
        viewport_height: int = 900,
        full_page: bool = False,
        interact: Callable | None = None,
        wait_after_load: int = 1000,
        wait_after_interact: int = 500,
    ):
        self.name = name
        self.description = description
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.full_page = full_page
        self.interact = interact
        self.wait_after_load = wait_after_load
        self.wait_after_interact = wait_after_interact


# Screenshot scenarios for each viewer type
SCREENSHOT_SCENARIOS = {
    "capture_viewer": [
        ScreenshotConfig(
            name="full",
            description="Complete viewer interface with all panels",
            viewport_height=900,
        ),
        ScreenshotConfig(
            name="controls",
            description="Playback controls and timeline focus",
            viewport_height=600,
            interact=lambda page: page.evaluate(
                "document.querySelector('.playback-controls')?.scrollIntoView({block: 'center'})"
            ),
        ),
        ScreenshotConfig(
            name="events",
            description="Event list and details panel",
            viewport_width=800,
            interact=lambda page: page.evaluate(
                "document.querySelector('.sidebar')?.scrollIntoView()"
            ),
        ),
    ],
    "segmentation_viewer": [
        ScreenshotConfig(
            name="overview",
            description="Episode library with thumbnails and list",
        ),
        ScreenshotConfig(
            name="episode_detail",
            description="Selected episode showing key frames and steps",
            interact=lambda page: (
                page.click(".episode-item") if page.query_selector(".episode-item") else None
            ),
        ),
        ScreenshotConfig(
            name="search_active",
            description="Search functionality filtering episodes by keyword",
            interact=lambda page: (
                page.fill("#search-input", "navigate")
                if page.query_selector("#search-input")
                else None
            ),
        ),
        ScreenshotConfig(
            name="recording_filter",
            description="Filter dropdown showing episodes from specific recording",
            interact=lambda page: (
                page.select_option("#recording-filter", index=0)
                if page.query_selector("#recording-filter")
                else None
            ),
        ),
        ScreenshotConfig(
            name="key_frames",
            description="Episode key frames gallery with action labels",
            viewport_height=700,
            interact=lambda page: (
                page.click(".episode-item"),
                page.evaluate(
                    "document.querySelector('.key-frames-section')?.scrollIntoView()"
                ),
            )[1] if page.query_selector(".episode-item") else None,
        ),
    ],
    "synthetic_demo_viewer": [
        ScreenshotConfig(
            name="overview",
            description="Demo library with domain filters and task selector",
        ),
        ScreenshotConfig(
            name="domain_filter",
            description="Filtered view showing only notepad demos",
            interact=lambda page: (
                page.click('button[data-domain="notepad"]')
                if page.query_selector('button[data-domain="notepad"]')
                else None
            ),
        ),
        ScreenshotConfig(
            name="demo_detail",
            description="Selected demo with syntax-highlighted steps",
            interact=lambda page: (
                page.click(".task-button") if page.query_selector(".task-button") else None
            ),
        ),
        ScreenshotConfig(
            name="prompt_panel",
            description="API prompt usage showing how demo is included",
            viewport_height=700,
            interact=lambda page: (
                page.click(".task-button"),
                page.evaluate("document.querySelector('#prompt-panel')?.scrollIntoView()"),
            )[1] if page.query_selector(".task-button") else None,
        ),
        ScreenshotConfig(
            name="impact_section",
            description="Side-by-side accuracy comparison (33% vs 100%)",
            viewport_height=600,
            interact=lambda page: page.evaluate(
                "document.querySelector('.impact-section')?.scrollIntoView()"
            ),
        ),
    ],
    "benchmark_viewer": [
        ScreenshotConfig(
            name="summary",
            description="Overall benchmark metrics and domain breakdown",
        ),
        ScreenshotConfig(
            name="task_list",
            description="Filterable task list with pass/fail status",
            interact=lambda page: page.evaluate(
                "document.querySelector('.tasks-section')?.scrollIntoView()"
            ),
        ),
        ScreenshotConfig(
            name="task_detail",
            description="Step-by-step replay of a task execution",
            interact=lambda page: (
                page.click(".task-item") if page.query_selector(".task-item") else None
            ),
        ),
        ScreenshotConfig(
            name="domain_breakdown",
            description="Success rate by domain with visual breakdown",
            viewport_height=600,
            interact=lambda page: page.evaluate(
                "document.querySelector('.domain-stats')?.scrollIntoView()"
            ),
        ),
    ],
}


def check_dependencies() -> dict[str, bool]:
    """Check if required dependencies are available."""
    deps = {}

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401

        deps["playwright"] = True
    except ImportError:
        deps["playwright"] = False
        print("Warning: playwright not available")

    return deps


def generate_screenshot(
    html_path: Path,
    output_path: Path,
    config: ScreenshotConfig,
) -> Path:
    """Generate a single screenshot with specified configuration.

    Args:
        html_path: Path to HTML file to screenshot
        output_path: Where to save the screenshot
        config: Screenshot configuration

    Returns:
        Path to generated screenshot
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            f"Playwright not installed: {e}\n"
            "Install with: uv pip install playwright && uv run playwright install chromium"
        ) from e

    print(f"  [{config.name}] {config.description}")
    print(f"    Viewport: {config.viewport_width}x{config.viewport_height}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": config.viewport_width, "height": config.viewport_height}
        )

        # Load HTML file
        file_url = f"file://{html_path.absolute()}"
        page.goto(file_url, wait_until="networkidle")

        # Wait for initial render
        page.wait_for_timeout(config.wait_after_load)

        # Custom interaction if provided
        if config.interact:
            try:
                config.interact(page)
                page.wait_for_timeout(config.wait_after_interact)
            except PlaywrightError as e:
                print(f"    Warning: Interaction failed: {e}")

        # Take screenshot
        output_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output_path), full_page=config.full_page)

        browser.close()

    size_kb = output_path.stat().st_size / 1024
    print(f"    Saved: {output_path.name} ({size_kb:.1f} KB)")

    return output_path


def generate_viewer_screenshots(
    viewer_html: Path,
    output_dir: Path,
    viewer_type: str,
    scenarios: list[ScreenshotConfig],
) -> list[Path]:
    """Generate all screenshots for a viewer.

    Args:
        viewer_html: Path to viewer HTML file
        output_dir: Output directory for screenshots
        viewer_type: Type of viewer (for naming)
        scenarios: List of screenshot scenarios

    Returns:
        List of generated screenshot paths
    """
    if not viewer_html.exists():
        print(f"Warning: {viewer_html} not found, skipping")
        return []

    print(f"\n{viewer_type.upper().replace('_', ' ')}")
    print(f"HTML: {viewer_html}")
    print(f"Scenarios: {len(scenarios)}")

    screenshots = []

    for i, scenario in enumerate(scenarios, 1):
        output_path = output_dir / f"{viewer_type}_{scenario.name}.png"

        try:
            screenshot_path = generate_screenshot(viewer_html, output_path, scenario)
            screenshots.append(screenshot_path)
        except Exception as e:  # noqa: BLE001
            # Deliberately blind: one bad scenario must not abandon the rest of
            # the batch. The failure is recorded so main() exits non-zero
            # instead of printing a success line.
            print(f"  ERROR: Failed to generate {scenario.name}: {e}")
            FAILED_SCENARIOS.append(f"{viewer_type}_{scenario.name}")

    return screenshots


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate comprehensive screenshots for all openadapt-viewer HTML files"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "docs" / "images",
        help="Output directory for screenshots",
    )
    parser.add_argument(
        "--viewers",
        nargs="+",
        choices=[
            "capture",
            "segmentation",
            "synthetic",
            "benchmark",
            "all",
        ],
        default=["all"],
        help="Which viewers to screenshot (default: all)",
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

    if not deps.get("playwright"):
        print("\nError: playwright is required")
        print("Install with: uv pip install playwright && uv run playwright install chromium")
        return 1

    # Determine which viewers to process
    if "all" in args.viewers:
        viewers_to_process = ["capture", "segmentation", "synthetic", "benchmark"]
    else:
        viewers_to_process = args.viewers

    # Create output directory
    args.output_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nOutput directory: {args.output_dir}")

    # Map viewer names to HTML files
    viewer_files = {
        "capture": [
            REPO_ROOT / "temp" / "turn-off-nightshift_viewer.html",
            REPO_ROOT / "temp" / "demo_new_viewer.html",
        ],
        "segmentation": [REPO_ROOT / "segmentation_viewer.html"],
        "synthetic": [REPO_ROOT / "synthetic_demo_viewer.html"],
        "benchmark": [REPO_ROOT / "benchmark_viewer.html"],
    }

    all_screenshots = []
    total_scenarios = 0

    # Generate screenshots for each viewer
    for viewer_type in viewers_to_process:
        if viewer_type not in viewer_files:
            print(f"\nWarning: Unknown viewer type: {viewer_type}")
            continue

        html_files = viewer_files[viewer_type]
        scenarios = SCREENSHOT_SCENARIOS.get(
            f"{viewer_type}_viewer", SCREENSHOT_SCENARIOS.get("capture_viewer", [])
        )

        total_scenarios += len(scenarios) * len(html_files)

        for html_file in html_files:
            # For capture viewer, use recording name in output
            if viewer_type == "capture":
                recording_name = html_file.stem.replace("_viewer", "")
                output_prefix = recording_name
                scenario_type = "capture_viewer"
            else:
                output_prefix = viewer_type
                scenario_type = f"{viewer_type}_viewer"

            viewer_scenarios = SCREENSHOT_SCENARIOS.get(scenario_type, [])

            screenshots = generate_viewer_screenshots(
                html_file,
                args.output_dir,
                output_prefix,
                viewer_scenarios,
            )
            all_screenshots.extend(screenshots)

    # Print summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"\nExpected scenarios: {total_scenarios}")
    print(f"Generated screenshots: {len(all_screenshots)}")

    if all_screenshots:
        print("\nScreenshots:")
        for path in sorted(all_screenshots):
            size_kb = path.stat().st_size / 1024
            print(f"  - {path.name} ({size_kb:.1f} KB)")

        total_size_mb = sum(p.stat().st_size for p in all_screenshots) / 1024 / 1024
        print(f"\nTotal size: {total_size_mb:.2f} MB")

    if FAILED_SCENARIOS:
        # Previously this returned 0 regardless, so a run in which every
        # scenario raised still printed a success line and exited 0. A viewer
        # HTML that is simply absent is still a skip, not a failure.
        print(
            f"\n✗ {len(FAILED_SCENARIOS)} scenario(s) failed: "
            f"{', '.join(FAILED_SCENARIOS)}",
            file=sys.stderr,
        )
        return 1

    print("\n✓ Screenshot generation completed!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
