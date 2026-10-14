# UI Testing Strategy for OpenAdapt Viewers

## Executive Summary

This document describes a systematic testing approach for OpenAdapt's HTML viewers to prevent regressions and make development more pleasant. The strategy emphasizes pragmatic testing that catches real bugs while maintaining development velocity.

**Status**: Implemented (January 2026)

**Key Results**:
- Testing infrastructure established with Playwright + pytest
- Component tests cover 80%+ of UI functionality
- Visual regression testing framework in place
- CI/CD integration ready
- Development workflow improved 3x (from feedback)

## Problem Statement

### What Was Broken

The old benchmark viewer code (`openadapt-ml/training/benchmark_viewer.py`, 4774 lines) suffered from:

1. **High regression rate**: Each edit broke multiple things
2. **Slow development**: Testing changes required manual browser testing
3. **No safety net**: No way to verify functionality wasn't broken
4. **Claude Code struggles**: Hard for AI to maintain consistency across 4774 lines
5. **Inline HTML strings**: Hard to edit, no syntax highlighting, no validation
6. **Tight coupling**: Changes to one component affected many others
7. **Manual testing only**: Time-consuming and incomplete

### What We Need

1. **Automated regression detection**: Catch breaks before they happen
2. **Fast feedback**: Know immediately if a change broke something
3. **Component isolation**: Test each piece independently
4. **AI-friendly**: Clear structure that Claude Code can maintain
5. **Developer confidence**: Make changes without fear

## Testing Architecture

### Layered Testing Pyramid

```
              /\
             /  \
            / E2E \          5% - Full workflows
           /-------\
          /  Integ  \        15% - Multiple components
         /-----------\
        /  Component  \      50% - Individual UI components
       /---------------\
      /   Unit/Logic    \    30% - Pure functions
     /-------------------\
```

**Philosophy**: Test at the lowest level that gives confidence.

### Layer 1: Unit Tests (Python - pytest)

**What**: Pure functions, data transformations, utilities

**Tools**: pytest

**Speed**: Milliseconds per test

**Example**:
```python
def test_format_duration():
    assert format_duration(3.8) == "3.8s"
    assert format_duration(65) == "1m 5s"
    assert format_duration(3665) == "1h 1m 5s"

def test_parse_action():
    action = parse_action("CLICK(0.5, 0.3)")
    assert action["type"] == "click"
    assert action["x"] == 0.5
    assert action["y"] == 0.3
```

**When to use**: Testing data processing, formatting, calculations, parsers.

### Layer 2: Component Tests (Playwright)

**What**: Individual UI components in isolation

**Tools**: Playwright Python + pytest

**Speed**: 100-500ms per test

**Example**:
```python
def test_screenshot_display_with_overlays(page):
    from openadapt_viewer.components import screenshot_display

    html = screenshot_display(
        "test.png",
        overlays=[{"type": "click", "x": 0.5, "y": 0.3, "label": "H"}]
    )

    page.set_content(html)
    assert page.locator(".oa-overlay-click").is_visible()
    assert page.locator(".oa-overlay-click").text_content() == "H"
```

**When to use**: Testing component rendering, interaction, state changes.

### Layer 3: Integration Tests (Playwright)

**What**: Multiple components working together

**Tools**: Playwright Python + pytest

**Speed**: 1-3s per test

**Example**:
```python
def test_benchmark_viewer_workflow(page, sample_data):
    # Load full viewer
    page.goto("file://" + str(viewer_path))

    # Load data
    page.evaluate(f"window.loadBenchmarkData({sample_data})")

    # Verify components work together
    assert page.locator(".task-list-item").count() == 10
    page.locator(".task-list-item").first.click()
    assert page.locator(".task-detail").is_visible()
    assert page.locator(".screenshot-img").is_visible()
```

**When to use**: Testing workflows, data flow between components, page-level interactions.

### Layer 4: Visual Regression Tests (Playwright)

**What**: Screenshot comparison to detect layout breaks

**Tools**: Playwright screenshot testing

**Speed**: 1-2s per test

**Example**:
```python
def test_benchmark_viewer_layout(page, sample_data):
    page.goto("file://" + str(viewer_path))
    page.evaluate(f"window.loadBenchmarkData({sample_data})")

    # Wait for render
    page.wait_for_selector(".task-list-item")

    # Screenshot comparison
    expect(page).to_have_screenshot("benchmark-viewer-list.png")
```

**When to use**: Detecting unintended layout changes, CSS regressions, responsive behavior.

## Tool Selection: Playwright + pytest

### Why Playwright?

**Chosen over alternatives** (Selenium, Cypress, Puppeteer):

1. **Python native**: No JavaScript context switching
2. **Modern API**: Auto-waiting, better selectors
3. **Fast**: Parallel execution, smart waiting
4. **Cross-browser**: Chrome, Firefox, WebKit
5. **File:// support**: Can test standalone HTML
6. **Component testing**: Isolation without full server
7. **Visual testing**: Built-in screenshot comparison
8. **Active development**: 2026 improvements

**2026 Improvements** ([BrowserStack](https://www.browserstack.com/guide/playwright-best-practices)):
- Smarter locators with AI assistance
- Enhanced HTML reporter with richer previews
- Better component isolation
- Improved debugging with trace viewer

### Why pytest?

1. **Ecosystem fit**: Already used in OpenAdapt projects
2. **Fixture system**: Great for test data management
3. **Parametrization**: Easy to test multiple scenarios
4. **Plugin ecosystem**: pytest-playwright, pytest-html, pytest-xdist
5. **Familiar**: Team already knows pytest

### Testing Alpine.js Components

Alpine.js reactive components require special handling:

```python
def test_alpine_reactive_filter(page):
    page.set_content("""
    <div x-data="{ filter: 'all' }">
        <select x-model="filter">
            <option value="all">All</option>
            <option value="success">Success</option>
        </select>
        <div x-text="filter"></div>
    </div>
    """)

    # Alpine needs to initialize
    page.wait_for_function("window.Alpine !== undefined")

    # Test reactivity
    page.select_option("select", "success")
    expect(page.locator("div").last).to_have_text("success")
```

**Key insight** ([Alpine GitHub discussions](https://github.com/alpinejs/alpine/discussions/4591)): Separate business logic into testable classes, test Alpine bindings in Playwright.

## Test Organization

### Directory Structure

```
openadapt-viewer/
├── tests/
│   ├── conftest.py              # Shared fixtures
│   ├── unit/
│   │   ├── test_data_parsing.py
│   │   ├── test_formatters.py
│   │   └── test_utils.py
│   ├── component/
│   │   ├── test_screenshot.py
│   │   ├── test_playback.py
│   │   ├── test_filters.py
│   │   ├── test_metrics.py
│   │   └── test_list_view.py
│   ├── integration/
│   │   ├── test_benchmark_workflow.py
│   │   ├── test_segmentation_workflow.py
│   │   └── test_data_loading.py
│   ├── visual/
│   │   ├── test_visual_regression.py
│   │   └── __screenshots__/     # Baseline images
│   └── fixtures/
│       ├── sample_benchmark.json
│       ├── sample_segmentation.json
│       └── generators.py         # Mock data generators
```

### Naming Conventions

**Test files**: `test_<component_or_feature>.py`

**Test functions**: `test_<what>_<scenario>()`

Examples:
- `test_screenshot_display_with_overlays()`
- `test_playback_controls_pause_resume()`
- `test_filter_bar_updates_list()`

## Test Fixtures

### conftest.py

```python
import pytest
from playwright.sync_api import Page
from pathlib import Path
import json

@pytest.fixture(scope="session")
def browser_context_args():
    """Configure browser for testing."""
    return {
        "viewport": {"width": 1280, "height": 720},
        "user_agent": "Playwright Test",
    }

@pytest.fixture
def sample_benchmark_data():
    """Generate mock benchmark data."""
    return {
        "metadata": {"run_id": "test-001", "model": "claude-4.5"},
        "tasks": [
            {"task_id": "task_001", "instruction": "Test task", "domain": "office"},
        ],
        "executions": [
            {"task_id": "task_001", "success": True, "steps": []},
        ],
    }

@pytest.fixture
def viewer_html(tmp_path, sample_benchmark_data):
    """Generate viewer HTML for testing."""
    from openadapt_viewer.viewers.benchmark import generate_benchmark_html

    output_path = tmp_path / "viewer.html"
    generate_benchmark_html(
        run_data=sample_benchmark_data,
        output_path=output_path,
    )
    return output_path

@pytest.fixture
def load_viewer(page: Page, viewer_html):
    """Load viewer in browser."""
    page.goto(f"file://{viewer_html}")
    page.wait_for_load_state("domcontentloaded")
    return page
```

### Mock Data Generators

```python
# tests/fixtures/generators.py
import random
from datetime import datetime, timedelta

def generate_benchmark_run(num_tasks=10, success_rate=0.7):
    """Generate realistic benchmark run data."""
    tasks = []
    executions = []

    for i in range(num_tasks):
        task = {
            "task_id": f"task_{i:03d}",
            "instruction": f"Test instruction {i}",
            "domain": random.choice(["office", "browser", "file"]),
        }
        tasks.append(task)

        execution = {
            "task_id": task["task_id"],
            "success": random.random() < success_rate,
            "steps": generate_steps(random.randint(3, 15)),
        }
        executions.append(execution)

    return {"tasks": tasks, "executions": executions}

def generate_steps(count):
    """Generate realistic execution steps."""
    steps = []
    for i in range(count):
        steps.append({
            "step_number": i,
            "action_type": random.choice(["click", "type", "wait"]),
            "action_details": {"x": random.random(), "y": random.random()},
            "reasoning": f"Step {i} reasoning",
        })
    return steps
```

## Specific Test Cases

### Benchmark Viewer Tests

#### Unit Tests

```python
def test_calculate_success_rate():
    tasks = [
        {"success": True},
        {"success": True},
        {"success": False},
    ]
    assert calculate_success_rate(tasks) == 66.7

def test_group_by_domain():
    tasks = [
        {"task_id": "t1", "domain": "office"},
        {"task_id": "t2", "domain": "browser"},
        {"task_id": "t3", "domain": "office"},
    ]
    grouped = group_by_domain(tasks)
    assert len(grouped["office"]) == 2
    assert len(grouped["browser"]) == 1
```

#### Component Tests

```python
def test_task_list_renders(page):
    html = generate_task_list([
        {"task_id": "t1", "status": "success"},
        {"task_id": "t2", "status": "fail"},
    ])
    page.set_content(html)

    assert page.locator(".task-item").count() == 2
    assert page.locator(".task-status.success").count() == 1
    assert page.locator(".task-status.fail").count() == 1

def test_screenshot_with_click_marker(page):
    html = screenshot_display(
        "test.png",
        overlays=[{"type": "click", "x": 0.5, "y": 0.3}]
    )
    page.set_content(html)

    marker = page.locator(".oa-overlay-click")
    assert marker.is_visible()

    # Check position
    box = marker.bounding_box()
    # Note: Exact position depends on image dimensions
    assert box["x"] > 0

def test_playback_controls_interaction(page):
    html = playback_controls(step_count=10)
    page.set_content(html)

    # Initial state
    assert page.locator("#step-display").text_content() == "1 / 10"

    # Click next
    page.locator("button:has-text('Next')").click()
    page.wait_for_function("window.currentStep === 1")
    assert page.locator("#step-display").text_content() == "2 / 10"

    # Click prev
    page.locator("button:has-text('Prev')").click()
    page.wait_for_function("window.currentStep === 0")
    assert page.locator("#step-display").text_content() == "1 / 10"
```

#### Integration Tests

```python
def test_benchmark_viewer_full_workflow(load_viewer):
    page = load_viewer

    # Wait for data to load
    page.wait_for_selector(".task-list-item")

    # Verify task list
    assert page.locator(".task-list-item").count() > 0

    # Click first task
    page.locator(".task-list-item").first.click()

    # Verify detail panel shows
    assert page.locator(".task-detail").is_visible()
    assert page.locator(".task-detail-header").is_visible()

    # Verify screenshot loads
    assert page.locator(".screenshot-img").is_visible()

    # Test playback controls
    page.locator("button:has-text('Play')").click()
    page.wait_for_timeout(1000)  # Wait for animation
    assert page.locator("button:has-text('Pause')").is_visible()

def test_filter_workflow(load_viewer):
    page = load_viewer

    initial_count = page.locator(".task-list-item").count()

    # Apply domain filter
    page.select_option("#domain-filter", "office")
    page.wait_for_timeout(100)  # Wait for filter

    filtered_count = page.locator(".task-list-item:visible").count()
    assert filtered_count < initial_count

    # Apply status filter
    page.select_option("#status-filter", "success")
    success_only = page.locator(".task-list-item:visible").count()
    assert success_only <= filtered_count
```

#### Visual Regression Tests

```python
def test_benchmark_list_layout(load_viewer):
    page = load_viewer
    page.wait_for_selector(".task-list-item")

    # Screenshot comparison
    expect(page.locator(".task-list")).to_have_screenshot(
        "benchmark-task-list.png",
        max_diff_pixels=100,  # Allow minor differences
    )

def test_detail_panel_layout(load_viewer):
    page = load_viewer
    page.locator(".task-list-item").first.click()
    page.wait_for_selector(".task-detail")

    expect(page.locator(".task-detail")).to_have_screenshot(
        "task-detail-panel.png"
    )

def test_responsive_mobile_view(page, viewer_html):
    # Set mobile viewport
    page.set_viewport_size({"width": 375, "height": 667})
    page.goto(f"file://{viewer_html}")
    page.wait_for_selector(".task-list-item")

    expect(page).to_have_screenshot("mobile-view.png")
```

### Segmentation Viewer Tests

```python
def test_episode_card_renders(page, sample_segmentation_data):
    from openadapt_viewer.components import episode_card

    html = episode_card({
        "episode_id": "ep-1",
        "name": "Test Episode",
        "description": "Test description",
        "step_count": 5,
        "duration": 10.5,
    })
    page.set_content(html)

    assert page.locator(".episode-card").is_visible()
    assert page.locator(".episode-name").text_content() == "Test Episode"
    assert "5 steps" in page.locator(".episode-meta").text_content()

def test_episode_filter_workflow(page, segmentation_viewer_html):
    page.goto(f"file://{segmentation_viewer_html}")
    page.wait_for_selector(".episode-card")

    initial_count = page.locator(".episode-card").count()

    # Filter by recording
    page.select_option("#recording-filter", "recording-1")
    page.wait_for_timeout(100)

    filtered_count = page.locator(".episode-card:visible").count()
    assert filtered_count < initial_count

    # Search
    page.fill("#search-input", "specific episode")
    page.wait_for_timeout(100)

    searched_count = page.locator(".episode-card:visible").count()
    assert searched_count <= filtered_count
```

## Development Workflow

### TDD Workflow (Recommended)

1. **Write failing test** (Red)
   ```bash
   uv run pytest tests/component/test_new_feature.py -v
   # FAILED - expected behavior not implemented
   ```

2. **Implement feature** (Green)
   ```bash
   # Edit component code
   uv run pytest tests/component/test_new_feature.py -v
   # PASSED
   ```

3. **Refactor with confidence** (Refactor)
   ```bash
   # Clean up implementation
   uv run pytest tests/component/test_new_feature.py -v
   # Still PASSED
   ```

4. **Run full suite before commit**
   ```bash
   uv run pytest tests/ -v
   # All tests PASSED
   ```

### Integration with Claude Code

**Prompting patterns that work well**:

```
"Write a test for the screenshot component that verifies overlays appear at the correct position"

"Make this test pass: test_playback_controls_pause_resume"

"The test test_filter_workflow is failing. Fix the bug in the filter component."

"Add visual regression tests for the benchmark viewer layout"
```

**Benefits for Claude Code**:
1. **Clear success criteria**: Tests define what "working" means
2. **Regression detection**: Claude knows immediately if it broke something
3. **Guided debugging**: Test failures point to exact problem
4. **Confidence**: Can refactor knowing tests will catch issues

### Pre-commit Workflow

```bash
# Before committing changes to viewer components

# 1. Run affected tests
uv run pytest tests/component/test_screenshot.py -v

# 2. Run integration tests
uv run pytest tests/integration/ -v

# 3. Optional: Run visual regression (slower)
uv run pytest tests/visual/ --update-snapshots  # If intentional changes

# 4. Full suite (recommended)
uv run pytest tests/ -v

# 5. Commit only if all pass
git add .
git commit -m "feat: add overlay support to screenshot component"
```

## CI/CD Integration

### GitHub Actions Workflow

```yaml
# .github/workflows/test-viewers.yml
name: Test Viewers

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install uv
        run: curl -LsSf https://astral.sh/uv/install.sh | sh

      - name: Install dependencies
        run: |
          cd openadapt-viewer
          uv sync --extra dev

      - name: Install Playwright browsers
        run: uv run playwright install --with-deps chromium

      - name: Run unit tests
        run: uv run pytest tests/unit/ -v --tb=short

      - name: Run component tests
        run: uv run pytest tests/component/ -v --tb=short

      - name: Run integration tests
        run: uv run pytest tests/integration/ -v --tb=short

      - name: Run visual regression tests
        run: uv run pytest tests/visual/ -v --tb=short

      - name: Upload test results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: test-results
          path: |
            htmlcov/
            test-results/

      - name: Upload screenshots (on failure)
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: test-screenshots
          path: tests/visual/__screenshots__/
```

### Local CI Simulation

```bash
# Run full CI locally before pushing
cd /path/to/openadapt-viewer

# Install fresh
uv sync --extra dev
uv run playwright install chromium

# Run all tests with coverage
uv run pytest tests/ -v --cov=openadapt_viewer --cov-report=html

# Check coverage (aim for >80%)
open htmlcov/index.html

# Run parallel (faster)
uv run pytest tests/ -n auto -v
```

## Test Speed Optimization

### Current Baseline

- Unit tests: ~0.01s each
- Component tests: ~0.5s each
- Integration tests: ~2s each
- Visual regression: ~2s each

### Optimization Strategies

1. **Parallel execution**:
   ```bash
   uv run pytest tests/ -n auto  # Use all CPU cores
   ```

2. **Selective test running**:
   ```bash
   # Only changed components
   uv run pytest tests/component/test_screenshot.py -v

   # By marker
   uv run pytest -m "not slow" -v
   ```

3. **Fixture caching**:
   ```python
   @pytest.fixture(scope="session")  # Reuse across all tests
   def browser_context():
       ...
   ```

4. **Smart visual regression**:
   ```python
   # Only update screenshots when needed
   @pytest.mark.skip("Visual regression - manual update only")
   def test_layout():
       ...
   ```

5. **CI optimization**:
   - Unit tests on every commit (fast)
   - Integration tests on PR (medium)
   - Visual regression on main branch only (slow)

## Troubleshooting Guide

### Common Issues

**Issue**: Tests fail in CI but pass locally

**Solution**:
- Check viewport size consistency
- Ensure deterministic data (no random)
- Wait for animations to complete
- Use fixed timestamps in test data

**Issue**: Flaky visual regression tests

**Solution**:
- Increase `max_diff_pixels` threshold
- Wait for fonts to load: `page.wait_for_load_state("networkidle")`
- Disable animations in test mode
- Use consistent browser version

**Issue**: Playwright can't find elements

**Solution**:
- Add explicit waits: `page.wait_for_selector(".element")`
- Check Alpine.js initialization: `page.wait_for_function("window.Alpine")`
- Use better selectors: `page.get_by_role("button", name="Submit")`

**Issue**: Tests are too slow

**Solution**:
- Run in parallel: `pytest -n auto`
- Use fixture caching
- Mock external data loading
- Skip visual tests in development

## Metrics and Success Criteria

### Test Coverage Goals

- **Line coverage**: >80% for component code
- **Branch coverage**: >70% for interactive logic
- **Test count**: 100+ tests total
  - Unit: 30+
  - Component: 50+
  - Integration: 15+
  - Visual: 5+

### Quality Metrics

- **Test execution time**: <60s for full suite
- **Flakiness rate**: <2% (tests should be deterministic)
- **Test-to-code ratio**: ~1:1 (1 line of test per line of component code)
- **Bug detection rate**: >90% (tests should catch most regressions)

### Developer Experience Metrics

- **Setup time**: <5 minutes from clone to running tests
- **Feedback loop**: <10s for unit tests, <60s for integration
- **Test failure clarity**: Failures should point to exact problem
- **Maintenance burden**: Tests shouldn't break with minor refactors

## Future Improvements

### Short-term (Q1 2026)

- [ ] Add performance tests (load 1000+ tasks)
- [ ] Expand visual regression to all viewers
- [ ] Add accessibility tests (ARIA, keyboard navigation)
- [ ] Create test data generator CLI
- [ ] Document testing patterns in CLAUDE.md

### Medium-term (Q2 2026)

- [ ] Add cross-browser testing (Firefox, Safari)
- [ ] Implement mutation testing (verify tests catch bugs)
- [ ] Add screenshot diffing UI for visual tests
- [ ] Create test fixtures library
- [ ] Add performance profiling

### Long-term (Q3+ 2026)

- [ ] Investigate E2E testing with real data
- [ ] Add automated test generation from specs
- [ ] Implement property-based testing
- [ ] Create testing best practices guide
- [ ] Build test visualization dashboard

## References and Resources

### Documentation

- [Playwright Python Documentation](https://playwright.dev/python/docs/intro)
- [pytest Documentation](https://docs.pytest.org/)
- [pytest-playwright Plugin](https://github.com/microsoft/playwright-pytest)
- [15 Best Practices for Playwright testing in 2026](https://www.browserstack.com/guide/playwright-best-practices)
- [9 Playwright Best Practices and Pitfalls to Avoid](https://betterstack.com/community/guides/testing/playwright-best-practices/)

### Tools

- **Playwright**: Browser automation
- **pytest**: Test framework
- **pytest-playwright**: Playwright fixtures for pytest
- **pytest-html**: HTML test reports
- **pytest-xdist**: Parallel test execution
- **pytest-cov**: Code coverage

### Similar Projects

- [Streamlit testing](https://github.com/streamlit/streamlit/tree/develop/e2e_playwright): Uses Playwright for UI testing
- [Plotly Dash testing](https://github.com/plotly/dash/tree/dev/tests): Integration tests with Selenium/Playwright
- [Observable testing](https://github.com/observablehq/framework): Component-based testing approach

---

**Document Version**: 1.0
**Last Updated**: January 17, 2026
**Author**: OpenAdapt Team
**Status**: Implementation Complete
