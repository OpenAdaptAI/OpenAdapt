# Testing Guide for OpenAdapt Viewers

**For developers**: How to write, run, and maintain tests for OpenAdapt viewers.

**For Claude Code**: How to interpret test failures and write tests for new features.

## Quick Start

```bash
# Install with dev dependencies
cd /path/to/openadapt-viewer
uv sync --extra dev

# Install Playwright browsers (one-time setup)
uv run playwright install chromium

# Run all tests
uv run pytest tests/ -v

# Run specific test file
uv run pytest tests/component/test_screenshot.py -v

# Run with coverage
uv run pytest tests/ --cov=openadapt_viewer --cov-report=html
open htmlcov/index.html
```

## Writing Tests

### Test Structure

All tests follow the Arrange-Act-Assert (AAA) pattern:

```python
def test_screenshot_with_overlay(page):
    # ARRANGE: Set up test data
    from openadapt_viewer.components import screenshot_display
    overlays = [{"type": "click", "x": 0.5, "y": 0.3}]

    # ACT: Perform the action
    html = screenshot_display("test.png", overlays=overlays)
    page.set_content(html)

    # ASSERT: Verify the result
    assert page.locator(".oa-overlay-click").is_visible()
    assert page.locator(".oa-overlay-click").count() == 1
```

### Unit Tests (Pure Functions)

**Location**: `tests/unit/`

**Purpose**: Test pure functions, data transformations, utilities.

**Example**:

```python
# tests/unit/test_formatters.py
from openadapt_viewer.utils import format_duration, format_timestamp

def test_format_duration_seconds():
    assert format_duration(5.3) == "5.3s"

def test_format_duration_minutes():
    assert format_duration(65) == "1m 5s"

def test_format_duration_hours():
    assert format_duration(3665) == "1h 1m 5s"

def test_format_duration_zero():
    assert format_duration(0) == "0s"

@pytest.mark.parametrize("seconds,expected", [
    (1, "1.0s"),
    (60, "1m 0s"),
    (3600, "1h 0m 0s"),
])
def test_format_duration_parametrized(seconds, expected):
    assert format_duration(seconds) == expected
```

**Guidelines**:
- Test edge cases (empty, zero, negative, max)
- Use parametrize for similar cases
- No Playwright/DOM needed
- Fast (<10ms per test)

### Component Tests (UI Elements)

**Location**: `tests/component/`

**Purpose**: Test individual UI components in isolation.

**Example**:

```python
# tests/component/test_screenshot.py
from playwright.sync_api import Page
from openadapt_viewer.components import screenshot_display

def test_screenshot_basic_rendering(page: Page):
    html = screenshot_display("test.png")
    page.set_content(html)

    assert page.locator(".oa-screenshot-container").is_visible()
    assert page.locator("img").get_attribute("src") == "test.png"

def test_screenshot_with_caption(page: Page):
    html = screenshot_display("test.png", caption="Step 5")
    page.set_content(html)

    caption = page.locator(".oa-screenshot-caption")
    assert caption.is_visible()
    assert caption.text_content() == "Step 5"

def test_screenshot_with_multiple_overlays(page: Page):
    overlays = [
        {"type": "click", "x": 0.2, "y": 0.3, "label": "H", "variant": "human"},
        {"type": "click", "x": 0.7, "y": 0.8, "label": "AI", "variant": "predicted"},
    ]
    html = screenshot_display("test.png", overlays=overlays)
    page.set_content(html)

    assert page.locator(".oa-overlay-click").count() == 2
    assert page.locator(".oa-overlay-human").count() == 1
    assert page.locator(".oa-overlay-predicted").count() == 1
```

**Guidelines**:
- Use `page.set_content(html)` for components
- Test rendering, not internal logic
- Verify CSS classes, visibility, content
- Use fixtures for complex test data

### Integration Tests (Multiple Components)

**Location**: `tests/integration/`

**Purpose**: Test multiple components working together, workflows, data flow.

**Example**:

```python
# tests/integration/test_benchmark_workflow.py
def test_benchmark_viewer_full_workflow(page, sample_benchmark_data):
    from openadapt_viewer.viewers.benchmark import generate_benchmark_html

    # Generate full viewer
    viewer_path = generate_benchmark_html(
        run_data=sample_benchmark_data,
        output_path="/tmp/test_viewer.html"
    )

    # Load in browser
    page.goto(f"file://{viewer_path}")
    page.wait_for_selector(".task-list-item")

    # Verify initial state
    assert page.locator(".task-list-item").count() == 10

    # Click first task
    page.locator(".task-list-item").first.click()

    # Verify detail panel shows
    assert page.locator(".task-detail").is_visible()
    assert page.locator(".screenshot-img").is_visible()

    # Test playback controls
    page.locator("button:has-text('Play')").click()
    page.wait_for_timeout(500)  # Wait for playback to advance

    # Verify state changed
    assert page.locator("button:has-text('Pause')").is_visible()
```

**Guidelines**:
- Test realistic user workflows
- Use fixtures to generate full pages
- Test data flow between components
- Add waits for async operations
- Test error handling

### Visual Regression Tests

**Location**: `tests/visual/`

**Purpose**: Detect unintended layout/styling changes.

**Example**:

```python
# tests/visual/test_visual_regression.py
from playwright.sync_api import Page, expect

def test_benchmark_list_layout(page, viewer_path):
    page.goto(f"file://{viewer_path}")
    page.wait_for_selector(".task-list-item")

    # Screenshot comparison with baseline
    expect(page.locator(".task-list")).to_have_screenshot(
        "benchmark-task-list.png",
        max_diff_pixels=100,  # Allow minor antialiasing differences
    )

def test_responsive_mobile_view(page, viewer_path):
    # Set mobile viewport
    page.set_viewport_size({"width": 375, "height": 667})
    page.goto(f"file://{viewer_path}")
    page.wait_for_selector(".task-list-item")

    expect(page).to_have_screenshot("mobile-view.png")
```

**Guidelines**:
- Update baselines when changes are intentional: `pytest --update-snapshots`
- Use `max_diff_pixels` for minor differences
- Test different viewports for responsive design
- Skip by default (slow): `@pytest.mark.skip("Manual visual check")`

## Using Fixtures

Fixtures provide reusable test data and setup.

### Built-in Fixtures

```python
def test_with_builtin_fixtures(page, tmp_path):
    # page: Playwright Page object (from pytest-playwright)
    page.goto("https://example.com")

    # tmp_path: Temporary directory (from pytest)
    output_file = tmp_path / "output.html"
    output_file.write_text("<html></html>")
```

### Custom Fixtures

```python
# tests/conftest.py
import pytest

@pytest.fixture
def sample_task():
    """Single task for testing."""
    return {
        "task_id": "task_001",
        "instruction": "Open Notepad",
        "domain": "office",
    }

@pytest.fixture
def sample_benchmark_run(sample_task):
    """Full benchmark run with multiple tasks."""
    return {
        "run_id": "test-001",
        "tasks": [sample_task] * 10,
        "executions": [...],
    }

# Use in tests
def test_with_fixtures(sample_task, sample_benchmark_run):
    assert sample_task["task_id"] == "task_001"
    assert len(sample_benchmark_run["tasks"]) == 10
```

### Fixture Scopes

```python
@pytest.fixture(scope="session")  # Shared across all tests (setup once)
def browser():
    ...

@pytest.fixture(scope="module")   # Shared within test file
def data_loader():
    ...

@pytest.fixture(scope="function")  # Default: new for each test
def temp_file():
    ...
```

## Running Tests

### Basic Commands

```bash
# All tests
uv run pytest tests/ -v

# Specific file
uv run pytest tests/component/test_screenshot.py -v

# Specific test
uv run pytest tests/component/test_screenshot.py::test_screenshot_basic_rendering -v

# Pattern matching
uv run pytest tests/ -k "screenshot" -v  # All tests with "screenshot" in name
```

### Speed Optimization

```bash
# Parallel execution (faster)
uv run pytest tests/ -n auto -v

# Stop on first failure
uv run pytest tests/ -x

# Run only failed tests from last run
uv run pytest tests/ --lf

# Show print statements
uv run pytest tests/ -v -s
```

### Coverage

```bash
# Run with coverage
uv run pytest tests/ --cov=openadapt_viewer --cov-report=html

# View coverage report
open htmlcov/index.html

# Coverage for specific module
uv run pytest tests/component/ --cov=openadapt_viewer.components
```

### Test Selection

```bash
# By marker
uv run pytest -m "not slow" -v          # Skip slow tests
uv run pytest -m "integration" -v       # Only integration tests

# By category
uv run pytest tests/unit/ -v            # Only unit tests
uv run pytest tests/component/ -v       # Only component tests
uv run pytest tests/integration/ -v     # Only integration tests
```

## Debugging Tests

### Show Output

```bash
# Show print statements and logs
uv run pytest tests/ -v -s

# Show local variables on failure
uv run pytest tests/ -v -l

# Full traceback
uv run pytest tests/ -v --tb=long
```

### Interactive Debugging

```python
def test_something(page):
    # Add breakpoint
    import pdb; pdb.set_trace()

    # Or use Playwright inspector
    page.pause()  # Opens Playwright inspector

    html = screenshot_display("test.png")
    page.set_content(html)
```

### Playwright Debug Mode

```bash
# Run with headed browser (see what's happening)
PWDEBUG=1 uv run pytest tests/component/test_screenshot.py -v

# Slow down execution
uv run pytest tests/ --slowmo=1000  # 1 second delay between actions
```

### Screenshot on Failure

```python
def test_something(page, tmp_path):
    try:
        page.goto("file:///test.html")
        assert page.locator(".missing").is_visible()
    except AssertionError:
        # Save screenshot for debugging
        page.screenshot(path=str(tmp_path / "failure.png"))
        raise
```

## Common Patterns

### Testing Alpine.js Components

Alpine.js components need special handling:

```python
def test_alpine_reactive_state(page):
    html = """
    <div x-data="{ count: 0 }">
        <span x-text="count"></span>
        <button @click="count++">Increment</button>
    </div>
    """
    page.set_content(html)

    # Wait for Alpine to initialize
    page.wait_for_function("window.Alpine !== undefined")

    # Test initial state
    assert page.locator("span").text_content() == "0"

    # Test reactivity
    page.locator("button").click()
    assert page.locator("span").text_content() == "1"
```

### Testing Async Operations

```python
def test_async_data_loading(page):
    page.goto("file:///viewer.html")

    # Wait for async data to load
    page.wait_for_selector(".task-list-item")

    # Or wait for specific condition
    page.wait_for_function("window.dataLoaded === true")

    # Or wait for network
    page.wait_for_load_state("networkidle")
```

### Testing Error States

```python
def test_error_handling(page):
    # Provide invalid data
    page.goto("file:///viewer.html")
    page.evaluate("window.loadData({ invalid: true })")

    # Verify error shown
    assert page.locator(".error-message").is_visible()
    assert "Invalid data format" in page.locator(".error-message").text_content()
```

### Testing Interactions

```python
def test_click_interactions(page):
    page.set_content(html)

    # Click
    page.locator("button").click()

    # Double click
    page.locator("button").dblclick()

    # Right click
    page.locator("button").click(button="right")

    # Hover
    page.locator("button").hover()

def test_form_interactions(page):
    # Type text
    page.locator("input").fill("test text")

    # Select dropdown
    page.select_option("select", "option1")

    # Check checkbox
    page.locator("input[type=checkbox]").check()

    # Upload file
    page.locator("input[type=file]").set_input_files("test.png")
```

## Best Practices

### 1. Use Semantic Selectors

```python
# GOOD: Semantic selectors
page.get_by_role("button", name="Submit")
page.get_by_label("Username")
page.get_by_text("Welcome")

# BAD: Brittle selectors
page.locator("#submit-btn-123")
page.locator("div > div > button:nth-child(3)")
```

### 2. Add Explicit Waits

```python
# GOOD: Wait for elements
page.wait_for_selector(".task-list-item")
page.locator(".task-list-item").first.click()

# BAD: Immediate action (might fail if not loaded)
page.locator(".task-list-item").first.click()
```

### 3. Test User Behavior, Not Implementation

```python
# GOOD: Test what user sees/does
assert page.locator(".task-status").text_content() == "Passed"
page.locator("button:has-text('Play')").click()

# BAD: Test internal state
assert page.evaluate("window.taskStatus === 'passed'")
assert page.evaluate("window.playbackState.playing === true")
```

### 4. Keep Tests Independent

```python
# GOOD: Each test is self-contained
def test_task_list():
    page.goto("file:///viewer.html")
    # Test task list

def test_task_detail():
    page.goto("file:///viewer.html")
    # Test task detail

# BAD: Tests depend on each other
def test_1_load_data():
    global loaded_data
    loaded_data = load_data()

def test_2_use_data():  # Depends on test_1
    assert loaded_data is not None
```

### 5. Use Fixtures for Common Setup

```python
# GOOD: Reusable fixture
@pytest.fixture
def loaded_viewer(page, sample_data):
    page.goto("file:///viewer.html")
    page.evaluate(f"window.loadData({sample_data})")
    page.wait_for_selector(".task-list-item")
    return page

def test_with_fixture(loaded_viewer):
    assert loaded_viewer.locator(".task-list-item").count() > 0

# BAD: Duplicate setup
def test_1():
    page.goto("file:///viewer.html")
    page.evaluate(...)
    page.wait_for_selector(...)
    # test

def test_2():
    page.goto("file:///viewer.html")
    page.evaluate(...)
    page.wait_for_selector(...)
    # test
```

## Troubleshooting

### Test Fails Locally but Passes in CI (or vice versa)

**Cause**: Different environments, timing issues

**Solution**:
- Add explicit waits: `page.wait_for_selector(".element")`
- Use deterministic test data (no `random`, fixed timestamps)
- Check viewport size consistency
- Ensure fonts/assets load before screenshots

### Flaky Tests

**Cause**: Race conditions, timing issues, network requests

**Solution**:
- Add waits for async operations
- Use `page.wait_for_load_state("networkidle")`
- Avoid hardcoded `wait_for_timeout()` (use conditions instead)
- Mock external requests

### Playwright Can't Find Element

**Cause**: Element not loaded, wrong selector, timing

**Solution**:
- Add explicit wait: `page.wait_for_selector(".element")`
- Check Alpine.js init: `page.wait_for_function("window.Alpine")`
- Use better selectors: `page.get_by_role("button")`
- Debug: `page.pause()` to inspect page

### Visual Regression Tests Fail Unexpectedly

**Cause**: Fonts not loaded, animations, antialiasing differences

**Solution**:
- Wait for fonts: `page.wait_for_load_state("networkidle")`
- Disable animations in test mode
- Increase threshold: `max_diff_pixels=100`
- Use consistent browser version
- Update baseline if change is intentional: `pytest --update-snapshots`

### Tests Are Too Slow

**Cause**: Sequential execution, unnecessary waits, large page loads

**Solution**:
- Run in parallel: `pytest -n auto`
- Use fixture caching: `scope="session"`
- Skip slow tests in dev: `@pytest.mark.slow`
- Mock data instead of loading from disk
- Use smaller test datasets

## For Claude Code

### Interpreting Test Failures

When a test fails, the output shows:

```
FAILED tests/component/test_screenshot.py::test_screenshot_with_overlay - AssertionError: assert False
    page = <Page url='about:blank'>

    def test_screenshot_with_overlay(page):
        html = screenshot_display("test.png", overlays=[...])
        page.set_content(html)
>       assert page.locator(".oa-overlay-click").is_visible()
E       AssertionError: assert False
E        +  where False = <method 'is_visible' of 'Locator' objects>()
E        +    where <method 'is_visible' of 'Locator' objects> = <Locator frame=<Frame name= url='about:blank'> selector='.oa-overlay-click'>.is_visible
```

**Read this as**:
1. **What failed**: `test_screenshot_with_overlay`
2. **Where**: Line with `>` (the assertion)
3. **Why**: Overlay with class `.oa-overlay-click` is not visible
4. **Fix**: Check if `screenshot_display()` generates the overlay markup correctly

### Writing Tests for New Features

**Pattern**: Start with what the user sees/does

1. **What does the user see?**
   ```python
   assert page.locator(".new-feature").is_visible()
   ```

2. **What does the user do?**
   ```python
   page.locator("button").click()
   ```

3. **What should happen?**
   ```python
   assert page.locator(".result").text_content() == "Expected"
   ```

**Example**: Adding a "Copy to Clipboard" button

```python
def test_copy_button_copies_text_to_clipboard(page):
    # ARRANGE: Set up component with copyable text
    html = generate_code_block("print('hello')", show_copy_button=True)
    page.set_content(html)

    # ACT: User clicks copy button
    page.locator("button:has-text('Copy')").click()

    # ASSERT: Clipboard contains text
    clipboard_text = page.evaluate("navigator.clipboard.readText()")
    assert clipboard_text == "print('hello')"

    # ASSERT: User sees feedback
    assert page.locator(".copy-success").is_visible()
```

### Fixing Failing Tests

**Steps**:

1. **Read the error message** to understand what failed
2. **Run the test locally** to reproduce
3. **Add `page.pause()`** to inspect the page at failure point
4. **Check the component code** that generates the HTML
5. **Fix the bug** in the component
6. **Verify the test passes**
7. **Run the full suite** to ensure no regressions

**Example**:

```python
# Test fails: screenshot overlay not appearing
def test_screenshot_with_overlay(page):
    html = screenshot_display("test.png", overlays=[{"type": "click", "x": 0.5}])
    page.set_content(html)

    page.pause()  # Add this to inspect

    assert page.locator(".oa-overlay-click").is_visible()
```

Run test, inspect page in Playwright Inspector, find that overlay div is not being created. Fix `screenshot_display()` to generate overlay markup. Test passes.

## Resources

- [Playwright Python Docs](https://playwright.dev/python/docs/intro)
- [pytest Documentation](https://docs.pytest.org/)
- [pytest-playwright Plugin](https://github.com/microsoft/playwright-pytest)
- [Playwright Best Practices](https://playwright.dev/docs/best-practices)
- [OpenAdapt Testing Strategy](../TESTING_STRATEGY.md)

---

**Last Updated**: January 17, 2026
**Maintainer**: OpenAdapt Team
