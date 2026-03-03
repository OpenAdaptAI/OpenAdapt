"""Automated screenshot generation for openadapt-desktop documentation.

Uses Playwright to render the HTML pages at multiple viewport sizes and
capture screenshots suitable for the README and documentation.

Usage:
    uv run python scripts/generate_screenshots.py
    uv run python scripts/generate_screenshots.py --output screenshots/
    uv run python scripts/generate_screenshots.py --skip-responsive
    uv run python scripts/generate_screenshots.py --page review

Requires:
    uv sync --extra dev
    uv run playwright install chromium
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


@dataclass
class Viewport:
    """Browser viewport configuration."""

    name: str
    width: int
    height: int


@dataclass
class Scenario:
    """A screenshot scenario defining what to capture."""

    name: str
    description: str
    page: str  # HTML file name (e.g., "index.html")
    viewport: Viewport
    interact: Callable | None = None  # Optional interaction before screenshot
    wait_for: str | None = None  # CSS selector to wait for


VIEWPORTS = {
    "desktop": Viewport("desktop", 1280, 800),
    "tablet": Viewport("tablet", 768, 1024),
    "mobile": Viewport("mobile", 375, 667),
}


def inject_mock_data(page) -> None:
    """Inject mock data into the page to simulate a populated state."""
    page.evaluate("""() => {
        // Simulate recording state
        const statusEl = document.getElementById('status');
        const statusText = document.getElementById('status-text');
        const duration = document.getElementById('duration');
        const storage = document.getElementById('storage');
        const capturesList = document.getElementById('captures-list');

        if (statusText) statusText.textContent = 'Idle';
        if (duration) duration.textContent = '--';
        if (storage) storage.textContent = '2.3 / 50.0 GB';

        // Add mock captures to the list
        if (capturesList) {
            capturesList.innerHTML = `
                <div style="background: #222; border-radius: 8px; padding: 12px; margin: 8px 0;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <div>
                            <strong>Session 2026-03-02 14:30</strong>
                            <div style="color: #888; font-size: 0.85rem;">Duration: 45m 12s &bull; 1.2 GB &bull; 12,847 events</div>
                        </div>
                        <span style="background: #f59e0b; color: #000; padding: 2px 8px; border-radius: 4px; font-size: 0.8rem;">Pending Review</span>
                    </div>
                </div>
                <div style="background: #222; border-radius: 8px; padding: 12px; margin: 8px 0;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <div>
                            <strong>Session 2026-03-02 09:15</strong>
                            <div style="color: #888; font-size: 0.85rem;">Duration: 2h 08m &bull; 3.1 GB &bull; 45,219 events</div>
                        </div>
                        <span style="background: #34d399; color: #000; padding: 2px 8px; border-radius: 4px; font-size: 0.8rem;">Reviewed</span>
                    </div>
                </div>
                <div style="background: #222; border-radius: 8px; padding: 12px; margin: 8px 0;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <div>
                            <strong>Session 2026-03-01 16:42</strong>
                            <div style="color: #888; font-size: 0.85rem;">Duration: 1h 23m &bull; 2.0 GB &bull; 28,103 events</div>
                        </div>
                        <span style="background: #34d399; color: #000; padding: 2px 8px; border-radius: 4px; font-size: 0.8rem;">Uploaded (S3)</span>
                    </div>
                </div>
            `;
        }
    }""")


def inject_recording_state(page) -> None:
    """Simulate an active recording state."""
    page.evaluate("""() => {
        const statusEl = document.getElementById('status');
        const statusText = document.getElementById('status-text');
        const duration = document.getElementById('duration');
        const btn = document.getElementById('btn-record');

        if (statusEl) {
            statusEl.classList.remove('idle');
            statusEl.classList.add('recording');
        }
        if (statusText) statusText.textContent = 'Recording';
        if (duration) duration.textContent = '00:12:34';
        if (btn) {
            btn.textContent = 'Stop Recording';
            btn.style.background = '#444';
        }
    }""")


def inject_review_data(page) -> None:
    """Inject mock review data into the review page."""
    page.evaluate("""() => {
        const pendingList = document.getElementById('pending-list');
        if (pendingList) {
            pendingList.innerHTML = `
                <div style="background: #222; border-radius: 8px; padding: 12px; margin: 8px 0;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <div>
                            <strong>Session 2026-03-02 14:30</strong>
                            <div style="color: #888; font-size: 0.85rem;">45m 12s &bull; 1.2 GB &bull; 3 PII regions detected</div>
                        </div>
                        <div>
                            <span style="background: #f59e0b; color: #000; padding: 2px 8px; border-radius: 4px; font-size: 0.8rem; margin-right: 4px;">Scrubbed</span>
                            <button style="background: #34d399; color: #000; border: none; padding: 4px 12px; border-radius: 4px; cursor: pointer;">Approve</button>
                            <button style="background: #ff5f5f; color: #fff; border: none; padding: 4px 12px; border-radius: 4px; cursor: pointer; margin-left: 4px;">Dismiss</button>
                        </div>
                    </div>
                </div>
                <div style="background: #222; border-radius: 8px; padding: 12px; margin: 8px 0;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <div>
                            <strong>Session 2026-03-02 09:15</strong>
                            <div style="color: #888; font-size: 0.85rem;">2h 08m &bull; 3.1 GB &bull; Not yet scrubbed</div>
                        </div>
                        <div>
                            <span style="background: #e94560; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 0.8rem; margin-right: 4px;">Captured</span>
                            <button style="background: #6366f1; color: #fff; border: none; padding: 4px 12px; border-radius: 4px; cursor: pointer;">Scrub</button>
                            <button style="background: #ff5f5f; color: #fff; border: none; padding: 4px 12px; border-radius: 4px; cursor: pointer; margin-left: 4px;">Dismiss</button>
                        </div>
                    </div>
                </div>
            `;
        }
    }""")


def build_scenarios() -> list[Scenario]:
    """Build all screenshot scenarios."""
    desktop = VIEWPORTS["desktop"]

    scenarios = [
        # Dashboard - idle state with captures
        Scenario(
            name="01_dashboard_idle",
            description="Main dashboard in idle state with recent captures",
            page="index.html",
            viewport=desktop,
            interact=inject_mock_data,
        ),
        # Dashboard - recording state
        Scenario(
            name="02_dashboard_recording",
            description="Main dashboard during active recording",
            page="index.html",
            viewport=desktop,
            interact=lambda p: (inject_mock_data(p), inject_recording_state(p)),
        ),
        # Upload review panel
        Scenario(
            name="03_review_panel",
            description="Upload review panel with pending recordings",
            page="review.html",
            viewport=desktop,
            interact=inject_review_data,
        ),
        # Settings panel
        Scenario(
            name="04_settings",
            description="Settings configuration panel",
            page="settings.html",
            viewport=desktop,
        ),
    ]

    return scenarios


def generate_screenshots(
    output_dir: Path,
    src_dir: Path,
    pages: list[str] | None = None,
    skip_responsive: bool = False,
    save_metadata: bool = True,
) -> list[dict]:
    """Generate screenshots for all scenarios.

    Args:
        output_dir: Directory to save screenshots.
        src_dir: Directory containing HTML source files.
        pages: Optional list of page names to filter (e.g., ["index", "review"]).
        skip_responsive: Skip tablet and mobile viewport screenshots.
        save_metadata: Save metadata JSON alongside screenshots.

    Returns:
        List of metadata dicts for each screenshot.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Run: uv run playwright install chromium")
        raise SystemExit(1)

    output_dir.mkdir(parents=True, exist_ok=True)
    scenarios = build_scenarios()

    # Filter by page name if specified
    if pages:
        scenarios = [s for s in scenarios if any(p in s.page for p in pages)]

    # Add responsive variants
    if not skip_responsive:
        responsive_scenarios = []
        for s in scenarios:
            for vp_name in ["tablet", "mobile"]:
                vp = VIEWPORTS[vp_name]
                responsive_scenarios.append(
                    Scenario(
                        name=f"{s.name}_{vp_name}",
                        description=f"{s.description} ({vp_name})",
                        page=s.page,
                        viewport=vp,
                        interact=s.interact,
                    )
                )
        scenarios.extend(responsive_scenarios)

    results = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)

        for scenario in scenarios:
            page_path = src_dir / scenario.page
            if not page_path.exists():
                print(f"  SKIP {scenario.name}: {page_path} not found")
                continue

            context = browser.new_context(
                viewport={
                    "width": scenario.viewport.width,
                    "height": scenario.viewport.height,
                },
            )
            page = context.new_page()

            file_url = f"file://{page_path.resolve()}"
            page.goto(file_url)
            page.wait_for_load_state("networkidle")

            # Run interaction callback if provided
            if scenario.interact:
                scenario.interact(page)
                page.wait_for_timeout(200)  # Let DOM settle

            # Wait for specific selector if specified
            if scenario.wait_for:
                page.wait_for_selector(scenario.wait_for, timeout=5000)

            # Capture screenshot
            screenshot_path = output_dir / f"{scenario.name}.png"
            page.screenshot(path=str(screenshot_path), full_page=True)

            meta = {
                "name": scenario.name,
                "description": scenario.description,
                "page": scenario.page,
                "viewport": f"{scenario.viewport.width}x{scenario.viewport.height}",
                "path": str(screenshot_path),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            results.append(meta)

            print(f"  OK {scenario.name} ({scenario.viewport.name})")
            context.close()

        browser.close()

    if save_metadata and results:
        metadata_path = output_dir / "screenshots.json"
        metadata_path.write_text(json.dumps(results, indent=2))
        print(f"\nMetadata saved to {metadata_path}")

    return results


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Generate screenshots for documentation")
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=Path("screenshots"),
        help="Output directory for screenshots (default: screenshots/)",
    )
    parser.add_argument(
        "--src",
        type=Path,
        default=Path("src"),
        help="Source directory containing HTML files (default: src/)",
    )
    parser.add_argument(
        "--page",
        type=str,
        action="append",
        help="Only generate for specific page(s) (e.g., --page index --page review)",
    )
    parser.add_argument(
        "--skip-responsive",
        action="store_true",
        help="Skip tablet and mobile viewport screenshots",
    )
    parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="Don't save metadata JSON",
    )
    args = parser.parse_args()

    print(f"Generating screenshots in {args.output}/")
    results = generate_screenshots(
        output_dir=args.output,
        src_dir=args.src,
        pages=args.page,
        skip_responsive=args.skip_responsive,
        save_metadata=not args.no_metadata,
    )
    print(f"\nGenerated {len(results)} screenshots")


if __name__ == "__main__":
    main()
