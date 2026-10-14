#!/usr/bin/env python3
"""Generate screenshots of segmentation viewer for documentation and testing.

This script automatically captures different UI states of the segmentation viewer:
- Initial empty state
- Episode list with thumbnails
- Episode details expanded
- Search functionality
- Filter controls
- Responsive layouts (desktop, tablet, mobile)

Usage:
    # Generate all screenshots
    uv run python scripts/generate_segmentation_screenshots.py

    # Custom output directory
    uv run python scripts/generate_segmentation_screenshots.py --output screenshots/

    # Specific viewport
    uv run python scripts/generate_segmentation_screenshots.py --viewport mobile

    # Skip certain scenarios
    uv run python scripts/generate_segmentation_screenshots.py --skip-responsive
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Add src to path for imports
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    from playwright.sync_api import Error as PlaywrightError
except ImportError:
    # playwright is an optional extra. generate_all_screenshots() raises a
    # RuntimeError with install instructions before any handler below can run,
    # so this alias only exists to keep the module importable (the test suite
    # imports it to check --help and --check-deps).
    PlaywrightError = Exception


@dataclass
class ScreenshotScenario:
    """Configuration for a screenshot scenario."""

    name: str
    description: str
    viewport_width: int
    viewport_height: int
    full_page: bool = False
    interact: Callable | None = None
    wait_for_selector: str | None = None
    wait_timeout: int = 1000


class SegmentationScreenshotGenerator:
    """Generate screenshots of segmentation viewer in various states."""

    def __init__(self, output_dir: Path, viewer_path: Path, test_data_path: Path):
        """Initialize the generator.

        Args:
            output_dir: Directory to save screenshots
            viewer_path: Path to segmentation_viewer.html
            test_data_path: Path to test episodes JSON
        """
        self.output_dir = output_dir
        self.viewer_path = viewer_path
        self.test_data_path = test_data_path
        self.screenshots_generated = []
        # Scenarios that raised. A run in which every scenario failed used
        # to still exit 0 and print "completed successfully".
        self.scenarios_failed: list[str] = []

        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_all_screenshots(self, skip_responsive: bool = False) -> list[Path]:
        """Generate all screenshot scenarios.

        Args:
            skip_responsive: Skip tablet/mobile responsive screenshots

        Returns:
            List of paths to generated screenshots
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                f"Playwright not installed: {e}\n"
                "Install with: uv pip install playwright && uv run playwright install chromium"
            ) from e

        print(f"Generating screenshots for: {self.viewer_path.name}")
        print(f"Using test data: {self.test_data_path.name}")
        print(f"Output directory: {self.output_dir}\n")

        with sync_playwright() as p:
            browser = p.chromium.launch()

            try:
                # Desktop screenshots
                self._generate_desktop_screenshots(browser)

                # Responsive screenshots
                if not skip_responsive:
                    self._generate_responsive_screenshots(browser)

            finally:
                browser.close()

        print(f"\nGenerated {len(self.screenshots_generated)} screenshots")
        return self.screenshots_generated

    def _generate_desktop_screenshots(self, browser):
        """Generate desktop viewport screenshots."""
        print("=" * 70)
        print("DESKTOP SCREENSHOTS (1920x1080)")
        print("=" * 70 + "\n")

        scenarios = [
            ScreenshotScenario(
                name="01_initial_empty",
                description="Initial empty state before loading data",
                viewport_width=1920,
                viewport_height=1080,
                wait_timeout=500,
            ),
            ScreenshotScenario(
                name="02_episodes_loaded",
                description="Episode list with all episodes visible",
                viewport_width=1920,
                viewport_height=1080,
                interact=self._load_test_data,
                wait_for_selector=".episode-card",
                wait_timeout=2000,
            ),
            ScreenshotScenario(
                name="03_episode_thumbnails",
                description="Episode cards showing thumbnails and metadata",
                viewport_width=1920,
                viewport_height=1080,
                interact=self._load_test_data,
                wait_for_selector=".episode-thumbnail img",
                wait_timeout=2000,
            ),
            ScreenshotScenario(
                name="04_episode_details_expanded",
                description="First episode expanded with details",
                viewport_width=1920,
                viewport_height=1080,
                interact=self._load_and_expand_first_episode,
                wait_for_selector=".episode-details.visible",
                wait_timeout=2000,
            ),
            ScreenshotScenario(
                name="05_key_frames_gallery",
                description="Key frames gallery in episode details",
                viewport_width=1920,
                viewport_height=1080,
                interact=self._load_and_expand_first_episode,
                wait_for_selector=".key-frames-grid",
                wait_timeout=2000,
            ),
            ScreenshotScenario(
                name="06_search_empty",
                description="Search input before typing",
                viewport_width=1920,
                viewport_height=1080,
                interact=lambda page: self._focus_search(page),
                wait_for_selector="#search-input",
            ),
            ScreenshotScenario(
                name="07_search_filtered",
                description="Search results filtered by query",
                viewport_width=1920,
                viewport_height=1080,
                interact=self._search_nightshift,
                wait_for_selector=".episode-card",
                wait_timeout=1500,
            ),
            ScreenshotScenario(
                name="08_recording_filter",
                description="Recording filter dropdown expanded",
                viewport_width=1920,
                viewport_height=1080,
                interact=self._show_recording_filter,
                wait_for_selector="#recording-filter",
            ),
            ScreenshotScenario(
                name="09_full_page",
                description="Full page view with all content",
                viewport_width=1920,
                viewport_height=1080,
                full_page=True,
                interact=self._load_test_data,
                wait_for_selector=".episode-card",
                wait_timeout=2000,
            ),
        ]

        self._execute_scenarios(browser, scenarios)

    def _generate_responsive_screenshots(self, browser):
        """Generate responsive viewport screenshots."""
        print("\n" + "=" * 70)
        print("RESPONSIVE SCREENSHOTS")
        print("=" * 70 + "\n")

        scenarios = [
            # Tablet
            ScreenshotScenario(
                name="10_tablet_list",
                description="Tablet view - Episode list",
                viewport_width=768,
                viewport_height=1024,
                interact=self._load_test_data,
                wait_for_selector=".episode-card",
                wait_timeout=2000,
            ),
            ScreenshotScenario(
                name="11_tablet_details",
                description="Tablet view - Episode details",
                viewport_width=768,
                viewport_height=1024,
                interact=self._load_and_expand_first_episode,
                wait_for_selector=".episode-details.visible",
                wait_timeout=2000,
            ),
            # Mobile
            ScreenshotScenario(
                name="12_mobile_list",
                description="Mobile view - Episode list",
                viewport_width=375,
                viewport_height=667,
                interact=self._load_test_data,
                wait_for_selector=".episode-card",
                wait_timeout=2000,
            ),
            ScreenshotScenario(
                name="13_mobile_details",
                description="Mobile view - Episode details",
                viewport_width=375,
                viewport_height=667,
                interact=self._load_and_expand_first_episode,
                wait_for_selector=".episode-details.visible",
                wait_timeout=2000,
            ),
        ]

        self._execute_scenarios(browser, scenarios)

    def _execute_scenarios(self, browser, scenarios: list[ScreenshotScenario]):
        """Execute a list of screenshot scenarios.

        Args:
            browser: Playwright browser instance
            scenarios: List of scenarios to execute
        """
        for i, scenario in enumerate(scenarios, 1):
            print(f"[{i}/{len(scenarios)}] {scenario.description}")
            print(f"    Viewport: {scenario.viewport_width}x{scenario.viewport_height}")

            page = browser.new_page(
                viewport={"width": scenario.viewport_width, "height": scenario.viewport_height}
            )

            try:
                # Load viewer
                file_url = f"file://{self.viewer_path.absolute()}"
                page.goto(file_url, wait_until="networkidle")

                # Wait for initial render
                page.wait_for_timeout(scenario.wait_timeout)

                # Execute interaction if provided
                if scenario.interact:
                    scenario.interact(page)

                # Wait for specific selector if provided
                if scenario.wait_for_selector:
                    try:
                        page.wait_for_selector(scenario.wait_for_selector, timeout=5000)
                    except PlaywrightError as e:
                        print(f"    Warning: Selector '{scenario.wait_for_selector}' not found: {e}")

                # Additional wait after interaction
                page.wait_for_timeout(500)

                # Take screenshot
                output_path = self.output_dir / f"{scenario.name}.png"
                page.screenshot(path=str(output_path), full_page=scenario.full_page)

                size_kb = output_path.stat().st_size / 1024
                print(f"    Saved: {output_path.name} ({size_kb:.1f} KB)")

                self.screenshots_generated.append(output_path)

            except Exception as e:  # noqa: BLE001
                # Deliberately blind: one bad scenario must not abandon the
                # other twelve. The failure is now recorded so main() can exit
                # non-zero instead of reporting success.
                print(f"    Error: {e}")
                self.scenarios_failed.append(scenario.name)

            finally:
                page.close()

    # Interaction helpers

    def _load_test_data(self, page):
        """Load test data into the viewer."""
        # Read test data
        with open(self.test_data_path) as f:
            test_data = json.load(f)

        # Inject data into page
        page.evaluate(
            """(data) => {
            if (typeof loadAndDisplayData === 'function') {
                loadAndDisplayData(data);
            } else if (typeof window.loadData === 'function') {
                window.loadData(data);
            } else {
                console.error('No data loading function found');
            }
        }""",
            test_data,
        )

    def _load_and_expand_first_episode(self, page):
        """Load data and expand first episode."""
        self._load_test_data(page)
        page.wait_for_timeout(500)

        # Click first episode card
        try:
            page.click(".episode-card:first-child")
            page.wait_for_timeout(500)
        except PlaywrightError as e:
            print(f"    Warning: Could not click first episode: {e}")

    def _focus_search(self, page):
        """Load data and focus search input."""
        self._load_test_data(page)
        page.wait_for_timeout(500)

        # Focus search input
        try:
            page.click("#search-input")
        except PlaywrightError as e:
            # Was `except Exception: pass`. A failed click still produced a
            # screenshot named "search focused" showing an unfocused search box
            # and reported it as generated.
            print(f"    Warning: Could not focus search input: {e}")

    def _search_nightshift(self, page):
        """Load data and search for 'nightshift'."""
        self._load_test_data(page)
        page.wait_for_timeout(500)

        # Type search query
        try:
            page.fill("#search-input", "nightshift")
            page.wait_for_timeout(500)
        except PlaywrightError as e:
            print(f"    Warning: Could not search: {e}")

    def _show_recording_filter(self, page):
        """Load data and show recording filter."""
        self._load_test_data(page)
        page.wait_for_timeout(500)

        # Click recording filter dropdown
        try:
            page.click("#recording-filter")
        except PlaywrightError as e:
            # Was `except Exception: pass`. Same failure mode as _focus_search:
            # a screenshot named "recording filter open" showing a closed
            # dropdown, reported as generated.
            print(f"    Warning: Could not open recording filter: {e}")

    def generate_metadata(self) -> dict[str, Any]:
        """Generate metadata about the screenshot generation.

        Returns:
            Metadata dictionary
        """
        from datetime import datetime

        return {
            "generated_at": datetime.now().astimezone().isoformat(),
            "viewer_path": str(self.viewer_path),
            "test_data_path": str(self.test_data_path),
            "output_dir": str(self.output_dir),
            "screenshot_count": len(self.screenshots_generated),
            "screenshots": [
                {
                    "path": str(path),
                    "filename": path.name,
                    "size_bytes": path.stat().st_size,
                }
                for path in self.screenshots_generated
            ],
        }


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate screenshots of segmentation viewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Generate all screenshots
    uv run python scripts/generate_segmentation_screenshots.py

    # Custom output directory
    uv run python scripts/generate_segmentation_screenshots.py --output docs/images/

    # Skip responsive screenshots
    uv run python scripts/generate_segmentation_screenshots.py --skip-responsive
        """,
    )

    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=REPO_ROOT / "screenshots" / "segmentation",
        help="Output directory for screenshots (default: screenshots/segmentation/)",
    )
    parser.add_argument(
        "--viewer",
        type=Path,
        default=REPO_ROOT / "segmentation_viewer.html",
        help="Path to segmentation viewer HTML (default: segmentation_viewer.html)",
    )
    parser.add_argument(
        "--test-data",
        type=Path,
        default=REPO_ROOT / "test_episodes.json",
        help="Path to test episodes JSON (default: test_episodes.json)",
    )
    parser.add_argument(
        "--skip-responsive",
        action="store_true",
        help="Skip responsive (tablet/mobile) screenshots",
    )
    parser.add_argument(
        "--save-metadata",
        action="store_true",
        help="Save metadata JSON alongside screenshots",
    )
    parser.add_argument(
        "--check-deps",
        action="store_true",
        help="Check dependencies and exit",
    )

    args = parser.parse_args()

    # Check dependencies
    if args.check_deps:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401

            print("✓ Playwright available")
            return 0
        except ImportError:
            print("✗ Playwright not installed")
            print("Install with: uv pip install playwright && uv run playwright install chromium")
            return 1

    # Validate paths
    if not args.viewer.exists():
        print(f"Error: Viewer not found: {args.viewer}", file=sys.stderr)
        return 1

    if not args.test_data.exists():
        print(f"Error: Test data not found: {args.test_data}", file=sys.stderr)
        return 1

    # Generate screenshots
    try:
        generator = SegmentationScreenshotGenerator(
            output_dir=args.output,
            viewer_path=args.viewer,
            test_data_path=args.test_data,
        )

        screenshots = generator.generate_all_screenshots(skip_responsive=args.skip_responsive)

        # Save metadata if requested
        if args.save_metadata:
            metadata = generator.generate_metadata()
            metadata_path = args.output / "metadata.json"
            with open(metadata_path, "w") as f:
                json.dump(metadata, f, indent=2)
            print(f"\nSaved metadata: {metadata_path}")

        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        print(f"\nGenerated {len(screenshots)} screenshots in: {args.output}")
        print("\nScreenshots:")
        for path in screenshots:
            print(f"  - {path.name}")

        if generator.scenarios_failed:
            # Previously this returned 0 however many scenarios raised, so a run
            # that captured nothing still printed "completed successfully" and
            # `openadapt-viewer screenshots` exited 0.
            print(
                f"\n✗ {len(generator.scenarios_failed)} scenario(s) failed: "
                f"{', '.join(generator.scenarios_failed)}",
                file=sys.stderr,
            )
            return 1

        print("\n✓ Screenshot generation completed successfully!")
        return 0

    except Exception as e:  # noqa: BLE001
        # Top of the process: turn any unexpected failure into an exit status
        # and a traceback rather than an unhandled crash.
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
