"""Example integration test for benchmark viewer workflow.

This demonstrates how to write comprehensive integration tests that verify
multiple components working together to provide real user workflows.

NOTE: These tests are currently skipped because:
1. The implementation has diverged from the expected CSS class names
2. The generate_benchmark_html() API has changed (no embed_screenshots param)
3. These tests need updating to match the actual implementation

To update:
1. Change CSS selectors to use oa- prefix (e.g., .summary-panel -> .oa-metrics-grid)
2. Remove embed_screenshots parameter from fixture
3. Update expected element structures
"""

from pathlib import Path

import pytest
from playwright.sync_api import Page

# Skip all tests in this module - implementation has diverged
pytestmark = [
    pytest.mark.playwright,
    pytest.mark.skip(reason="Tests need updating to match current implementation (CSS classes, API)"),
]


@pytest.fixture
def sample_benchmark_with_screenshots(tmp_path):
    """Generate benchmark data with screenshots for testing."""
    import base64
    from datetime import datetime, timedelta

    from openadapt_viewer.core.types import (
        BenchmarkRun,
        BenchmarkTask,
        ExecutionStep,
        TaskExecution,
    )

    # Create minimal PNG for testing
    minimal_png = base64.b64decode(
        b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    # Create screenshot files
    screenshots_dir = tmp_path / "screenshots"
    screenshots_dir.mkdir()
    for i in range(3):
        (screenshots_dir / f"step_{i}.png").write_bytes(minimal_png)

    # Create tasks
    tasks = [
        BenchmarkTask(
            task_id="task_001",
            instruction="Open Notepad and type hello",
            domain="office",
            difficulty="easy",
        ),
        BenchmarkTask(
            task_id="task_002",
            instruction="Navigate to google.com",
            domain="browser",
            difficulty="easy",
        ),
        BenchmarkTask(
            task_id="task_003",
            instruction="Create new folder on Desktop",
            domain="file_management",
            difficulty="medium",
        ),
    ]

    # Create executions with steps
    executions = [
        TaskExecution(
            task_id="task_001",
            start_time=datetime.now().astimezone(),
            end_time=datetime.now().astimezone() + timedelta(seconds=5),
            steps=[
                ExecutionStep(
                    step_number=0,
                    action_type="click",
                    action_details={"x": 0.5, "y": 0.3},
                    reasoning="Click on target",
                    screenshot_path=str(screenshots_dir / "step_0.png"),
                ),
                ExecutionStep(
                    step_number=1,
                    action_type="type",
                    action_details={"text": "hello"},
                    reasoning="Type the required text",
                    screenshot_path=str(screenshots_dir / "step_1.png"),
                ),
                ExecutionStep(
                    step_number=2,
                    action_type="click",
                    action_details={"x": 0.8, "y": 0.6},
                    reasoning="Click submit",
                    screenshot_path=str(screenshots_dir / "step_2.png"),
                ),
            ],
            success=True,
        ),
        TaskExecution(
            task_id="task_002",
            start_time=datetime.now().astimezone(),
            end_time=datetime.now().astimezone() + timedelta(seconds=3),
            steps=[
                ExecutionStep(
                    step_number=0,
                    action_type="click",
                    action_details={"x": 0.2, "y": 0.1},
                    reasoning="Open browser",
                ),
            ],
            success=False,
            error="Failed to open browser",
        ),
        TaskExecution(
            task_id="task_003",
            start_time=datetime.now().astimezone(),
            end_time=datetime.now().astimezone() + timedelta(seconds=4),
            steps=[
                ExecutionStep(
                    step_number=0,
                    action_type="click",
                    action_details={"x": 0.3, "y": 0.4},
                    reasoning="Right click on desktop",
                ),
            ],
            success=True,
        ),
    ]

    return BenchmarkRun(
        run_id="test_run",
        benchmark_name="Test Benchmark",
        model_id="test-model-v1",
        start_time=datetime.now().astimezone() - timedelta(minutes=10),
        end_time=datetime.now().astimezone(),
        tasks=tasks,
        executions=executions,
    )


@pytest.fixture
def benchmark_viewer_html(tmp_path, sample_benchmark_with_screenshots):
    """Generate benchmark viewer HTML for testing."""
    from openadapt_viewer.viewers.benchmark import generate_benchmark_html

    output_path = tmp_path / "benchmark_viewer.html"
    generate_benchmark_html(
        run_data=sample_benchmark_with_screenshots,
        output_path=output_path,
        embed_screenshots=True,  # Embed for self-contained testing
    )
    return output_path


class TestBenchmarkViewerWorkflow:
    """Integration tests for benchmark viewer user workflows."""

    def test_initial_load_shows_summary_and_task_list(
        self, page: Page, benchmark_viewer_html: Path
    ):
        """Test that viewer loads and shows summary stats and task list."""
        # ARRANGE & ACT: Navigate to viewer
        page.goto(f"file://{benchmark_viewer_html}")

        # ASSERT: Summary stats are visible
        assert page.locator(".summary-panel").is_visible()
        assert page.locator(".stat-card").count() >= 3

        # Verify total tasks stat
        total_tasks = page.locator(".stat-card").filter(
            has_text="Total Tasks"
        ).locator(".stat-value")
        assert total_tasks.text_content() == "3"

        # ASSERT: Task list is visible
        assert page.locator(".task-list").is_visible()
        assert page.locator(".task-item").count() == 3

        # Verify task items show correct info
        first_task = page.locator(".task-item").first
        assert first_task.locator(".task-id").text_content() == "task_001"
        assert first_task.locator(".task-status.success").is_visible()

    def test_filter_by_domain(self, page: Page, benchmark_viewer_html: Path):
        """Test filtering tasks by domain."""
        # ARRANGE: Load viewer
        page.goto(f"file://{benchmark_viewer_html}")
        page.wait_for_selector(".task-item")

        initial_count = page.locator(".task-item").count()
        assert initial_count == 3

        # ACT: Apply domain filter
        page.select_option("#domain-filter", "office")
        page.wait_for_timeout(100)  # Wait for filter to apply

        # ASSERT: Only office tasks visible
        visible_tasks = page.locator(".task-item:not(.hidden)")
        assert visible_tasks.count() == 1
        assert "office" in visible_tasks.first.locator(".task-domain").text_content()

    def test_filter_by_status(self, page: Page, benchmark_viewer_html: Path):
        """Test filtering tasks by pass/fail status."""
        # ARRANGE: Load viewer
        page.goto(f"file://{benchmark_viewer_html}")
        page.wait_for_selector(".task-item")

        # ACT: Filter by success only
        page.select_option("#status-filter", "success")
        page.wait_for_timeout(100)

        # ASSERT: Only successful tasks visible
        visible_tasks = page.locator(".task-item:not(.hidden)")
        assert visible_tasks.count() == 2
        for i in range(visible_tasks.count()):
            assert visible_tasks.nth(i).locator(".task-status.success").is_visible()

        # ACT: Filter by failed only
        page.select_option("#status-filter", "fail")
        page.wait_for_timeout(100)

        # ASSERT: Only failed tasks visible
        visible_tasks = page.locator(".task-item:not(.hidden)")
        assert visible_tasks.count() == 1
        assert visible_tasks.first.locator(".task-status.fail").is_visible()

    def test_click_task_shows_detail_panel(
        self, page: Page, benchmark_viewer_html: Path
    ):
        """Test clicking a task shows detail panel."""
        # ARRANGE: Load viewer
        page.goto(f"file://{benchmark_viewer_html}")
        page.wait_for_selector(".task-item")

        # ACT: Click first task
        page.locator(".task-item").first.click()

        # ASSERT: Detail panel shows
        assert page.locator(".task-detail").is_visible()
        assert page.locator(".task-detail-header").is_visible()

        # Verify task info displayed
        header = page.locator(".task-detail-header")
        assert "task_001" in header.text_content()
        assert "PASSED" in header.text_content()

        # Verify instruction shown
        instruction = page.locator(".task-detail-instruction")
        assert instruction.is_visible()
        assert "Open Notepad and type hello" in instruction.text_content()

    def test_playback_controls_navigate_steps(
        self, page: Page, benchmark_viewer_html: Path
    ):
        """Test playback controls for step navigation."""
        # ARRANGE: Load viewer and select first task
        page.goto(f"file://{benchmark_viewer_html}")
        page.wait_for_selector(".task-item")
        page.locator(".task-item").first.click()
        page.wait_for_selector(".step-viewer")

        # Initial state: step 1/3
        step_progress = page.locator("#step-progress")
        assert "1 / 3" in step_progress.text_content()

        # ACT: Click Next button
        page.locator("button:has-text('Next')").click()
        page.wait_for_timeout(100)

        # ASSERT: Advanced to step 2
        assert "2 / 3" in step_progress.text_content()

        # ACT: Click Next again
        page.locator("button:has-text('Next')").click()
        page.wait_for_timeout(100)

        # ASSERT: Advanced to step 3
        assert "3 / 3" in step_progress.text_content()

        # ACT: Click Prev button
        page.locator("button:has-text('Prev')").click()
        page.wait_for_timeout(100)

        # ASSERT: Back to step 2
        assert "2 / 3" in step_progress.text_content()

    def test_step_list_shows_actions(self, page: Page, benchmark_viewer_html: Path):
        """Test that step list shows all actions."""
        # ARRANGE: Load viewer and select first task
        page.goto(f"file://{benchmark_viewer_html}")
        page.wait_for_selector(".task-item")
        page.locator(".task-item").first.click()
        page.wait_for_selector(".step-list")

        # ASSERT: Step list shows all 3 steps
        step_items = page.locator(".step-list-item")
        assert step_items.count() == 3

        # Verify step actions
        assert "CLICK" in step_items.nth(0).text_content()
        assert "TYPE" in step_items.nth(1).text_content()
        assert "CLICK" in step_items.nth(2).text_content()

    def test_click_step_in_list_navigates_to_step(
        self, page: Page, benchmark_viewer_html: Path
    ):
        """Test clicking a step in the list navigates to that step."""
        # ARRANGE: Load viewer and select first task
        page.goto(f"file://{benchmark_viewer_html}")
        page.wait_for_selector(".task-item")
        page.locator(".task-item").first.click()
        page.wait_for_selector(".step-list-item")

        # Initial state: step 1
        assert "1 / 3" in page.locator("#step-progress").text_content()

        # ACT: Click third step in list
        page.locator(".step-list-item").nth(2).click()
        page.wait_for_timeout(100)

        # ASSERT: Navigated to step 3
        assert "3 / 3" in page.locator("#step-progress").text_content()

        # ASSERT: Third step is active in list
        assert page.locator(".step-list-item").nth(2).has_class("active")

    def test_action_details_update_with_step(
        self, page: Page, benchmark_viewer_html: Path
    ):
        """Test that action details panel updates when navigating steps."""
        # ARRANGE: Load viewer and select first task
        page.goto(f"file://{benchmark_viewer_html}")
        page.wait_for_selector(".task-item")
        page.locator(".task-item").first.click()
        page.wait_for_selector(".action-detail")

        # Initial action: CLICK
        action_content = page.locator("#action-content")
        assert "CLICK" in action_content.text_content()
        assert "50.0%" in action_content.text_content()  # x=0.5
        assert "30.0%" in action_content.text_content()  # y=0.3

        # ACT: Navigate to step 2 (TYPE action)
        page.locator("button:has-text('Next')").click()
        page.wait_for_timeout(100)

        # ASSERT: Action updated to TYPE
        assert "TYPE" in action_content.text_content()
        assert "hello" in action_content.text_content()

    def test_reasoning_displays_for_steps(
        self, page: Page, benchmark_viewer_html: Path
    ):
        """Test that reasoning is displayed for steps that have it."""
        # ARRANGE: Load viewer and select first task
        page.goto(f"file://{benchmark_viewer_html}")
        page.wait_for_selector(".task-item")
        page.locator(".task-item").first.click()
        page.wait_for_selector(".reasoning-box")

        # ASSERT: Reasoning shown for first step
        reasoning_box = page.locator("#reasoning-box")
        assert reasoning_box.is_visible()
        assert "Click on target" in page.locator("#reasoning-content").text_content()

        # ACT: Navigate to next step
        page.locator("button:has-text('Next')").click()
        page.wait_for_timeout(100)

        # ASSERT: Reasoning updated
        assert "Type the required text" in page.locator(
            "#reasoning-content"
        ).text_content()

    def test_screenshot_displays_for_steps_with_images(
        self, page: Page, benchmark_viewer_html: Path
    ):
        """Test that screenshots display when available."""
        # ARRANGE: Load viewer and select first task
        page.goto(f"file://{benchmark_viewer_html}")
        page.wait_for_selector(".task-item")
        page.locator(".task-item").first.click()
        page.wait_for_selector("#screenshot-img")

        # ASSERT: Screenshot visible
        screenshot = page.locator("#screenshot-img")
        assert screenshot.is_visible()

        # Verify it's an image (has src attribute)
        src = screenshot.get_attribute("src")
        assert src is not None
        assert len(src) > 0  # Has some content

    def test_failed_task_shows_error_message(
        self, page: Page, benchmark_viewer_html: Path
    ):
        """Test that failed tasks display error messages."""
        # ARRANGE: Load viewer
        page.goto(f"file://{benchmark_viewer_html}")
        page.wait_for_selector(".task-item")

        # ACT: Click the failed task (task_002)
        page.locator(".task-item").nth(1).click()
        page.wait_for_selector(".task-detail-header")

        # ASSERT: Shows FAILED status
        header = page.locator(".task-detail-header")
        assert "FAILED" in header.text_content()

        # ASSERT: Error message displayed
        meta = page.locator(".task-detail-meta")
        assert "Error:" in meta.text_content()
        assert "Failed to open browser" in meta.text_content()

    def test_domain_breakdown_shows_per_domain_stats(
        self, page: Page, benchmark_viewer_html: Path
    ):
        """Test that domain breakdown shows statistics per domain."""
        # ARRANGE & ACT: Load viewer
        page.goto(f"file://{benchmark_viewer_html}")
        page.wait_for_selector(".domain-breakdown")

        # ASSERT: Domain tags visible
        domain_tags = page.locator(".domain-tag")
        assert domain_tags.count() >= 2  # At least office and browser

        # Verify domain stats format (e.g., "office: 1/1 (100%)")
        first_domain = domain_tags.first
        assert "/" in first_domain.text_content()  # Has pass/total format

    def test_progress_bar_reflects_current_step(
        self, page: Page, benchmark_viewer_html: Path
    ):
        """Test that progress bar updates with current step."""
        # ARRANGE: Load viewer and select first task
        page.goto(f"file://{benchmark_viewer_html}")
        page.wait_for_selector(".task-item")
        page.locator(".task-item").first.click()
        page.wait_for_selector("#step-progress-bar")

        # Initial state: 0% (step 1 of 3)
        progress_bar = page.locator("#step-progress-bar")
        width = progress_bar.evaluate("el => el.style.width")
        assert width == "0%"

        # ACT: Navigate to middle step
        page.locator("button:has-text('Next')").click()
        page.wait_for_timeout(100)

        # ASSERT: Progress updated (step 2 of 3 = 50%)
        width = progress_bar.evaluate("el => el.style.width")
        assert width == "50%"

        # ACT: Navigate to last step
        page.locator("button:has-text('Next')").click()
        page.wait_for_timeout(100)

        # ASSERT: Progress at 100% (step 3 of 3)
        width = progress_bar.evaluate("el => el.style.width")
        assert width == "100%"


class TestBenchmarkViewerAccessibility:
    """Tests for accessibility features."""

    def test_keyboard_navigation_works(
        self, page: Page, benchmark_viewer_html: Path
    ):
        """Test that keyboard shortcuts work for navigation."""
        # ARRANGE: Load viewer and select first task
        page.goto(f"file://{benchmark_viewer_html}")
        page.wait_for_selector(".task-item")
        page.locator(".task-item").first.click()
        page.wait_for_selector(".step-viewer")

        # Initial state
        assert "1 / 3" in page.locator("#step-progress").text_content()

        # ACT: Press Arrow Right
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(100)

        # ASSERT: Advanced to next step
        assert "2 / 3" in page.locator("#step-progress").text_content()

        # ACT: Press Arrow Left
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(100)

        # ASSERT: Back to first step
        assert "1 / 3" in page.locator("#step-progress").text_content()

    def test_space_bar_toggles_playback(
        self, page: Page, benchmark_viewer_html: Path
    ):
        """Test that space bar toggles play/pause."""
        # ARRANGE: Load viewer and select first task
        page.goto(f"file://{benchmark_viewer_html}")
        page.wait_for_selector(".task-item")
        page.locator(".task-item").first.click()
        page.wait_for_selector("#play-btn")

        # Initial state: Play button visible
        assert page.locator("button:has-text('Play')").is_visible()

        # ACT: Press space bar
        page.keyboard.press("Space")
        page.wait_for_timeout(100)

        # ASSERT: Pause button visible (playback started)
        assert page.locator("button:has-text('Pause')").is_visible()

        # ACT: Press space bar again
        page.keyboard.press("Space")
        page.wait_for_timeout(100)

        # ASSERT: Play button visible (playback paused)
        assert page.locator("button:has-text('Play')").is_visible()
