# Testing Quick Reference

**Quick commands and patterns for daily testing work.**

## Quick Commands

```bash
# Run all tests
uv run pytest tests/ -v

# Run fast tests only (skip slow integration/visual)
uv run pytest tests/test_components/ tests/test_cli.py tests/test_data.py -v

# Run specific test file
uv run pytest tests/test_components/test_screenshot.py -v

# Run specific test
uv run pytest tests/test_components/test_screenshot.py::TestScreenshotDisplay::test_basic_screenshot -v

# Run with coverage
uv run pytest tests/ --cov=openadapt_viewer --cov-report=html
open htmlcov/index.html

# Run in parallel (faster)
uv run pytest tests/ -n auto

# Stop on first failure
uv run pytest tests/ -x

# Show print statements
uv run pytest tests/ -v -s

# Debug with headed browser
PWDEBUG=1 uv run pytest tests/test_components/test_screenshot.py -v
```

## Test Patterns

### Component Test Template

```python
from playwright.sync_api import Page

def test_my_component_basic(page: Page):
    """Test basic rendering of my component."""
    from openadapt_viewer.components import my_component

    # Generate HTML
    html = my_component(data="test")

    # Load in browser
    page.set_content(html)

    # Verify rendering
    assert page.locator(".oa-my-component").is_visible()
    assert page.locator(".oa-my-component").text_content() == "test"
```

### Integration Test Template

```python
def test_full_workflow(page: Page, tmp_path):
    """Test complete user workflow."""
    from openadapt_viewer.viewers.benchmark import generate_benchmark_html

    # Generate viewer
    viewer_path = generate_benchmark_html(
        run_data=sample_data,
        output_path=tmp_path / "viewer.html"
    )

    # Load in browser
    page.goto(f"file://{viewer_path}")
    page.wait_for_selector(".task-item")

    # User actions
    page.locator(".task-item").first.click()
    assert page.locator(".task-detail").is_visible()
```

### Fixture Template

```python
# In tests/conftest.py
@pytest.fixture
def my_test_data():
    """Provide test data for tests."""
    return {
        "id": "test-001",
        "value": "test",
    }

@pytest.fixture
def my_viewer_html(tmp_path, my_test_data):
    """Generate viewer HTML for testing."""
    output = tmp_path / "viewer.html"
    generate_my_viewer(data=my_test_data, output_path=output)
    return output
```

## Common Assertions

```python
# Visibility
assert page.locator(".element").is_visible()
assert not page.locator(".element").is_visible()

# Text content
assert page.locator(".element").text_content() == "Expected"
assert "substring" in page.locator(".element").text_content()

# Count
assert page.locator(".item").count() == 5

# Attributes
assert page.locator("img").get_attribute("src") == "image.png"

# CSS classes
assert page.locator(".element").has_class("active")

# Playwright expect (with auto-retry)
from playwright.sync_api import expect
expect(page.locator(".element")).to_be_visible()
expect(page.locator(".element")).to_have_text("Expected")
expect(page.locator(".element")).to_have_count(5)
```

## Waiting Strategies

```python
# Wait for element
page.wait_for_selector(".element")

# Wait for condition
page.wait_for_function("window.dataLoaded === true")

# Wait for Alpine.js
page.wait_for_function("window.Alpine !== undefined")

# Wait for network idle
page.wait_for_load_state("networkidle")

# Explicit timeout
page.wait_for_timeout(500)  # Use sparingly, prefer selectors
```

## Debugging

```python
# Add breakpoint
def test_something(page):
    import pdb; pdb.set_trace()

# Pause and inspect in Playwright
def test_something(page):
    page.pause()  # Opens Playwright inspector

# Screenshot on failure
try:
    assert page.locator(".element").is_visible()
except AssertionError:
    page.screenshot(path="failure.png")
    raise
```

## Selectors

```python
# CSS selector
page.locator(".class-name")
page.locator("#id")
page.locator("button")

# Semantic selectors (preferred)
page.get_by_role("button", name="Submit")
page.get_by_label("Username")
page.get_by_text("Welcome")
page.get_by_placeholder("Enter name")

# Combined
page.locator(".task-item").first
page.locator(".task-item").nth(2)
page.locator(".task-item").filter(has_text="specific")
```

## Actions

```python
# Click
page.locator("button").click()

# Type
page.locator("input").fill("text")
page.locator("input").type("text", delay=100)  # Slower typing

# Select dropdown
page.select_option("select", "option1")

# Check/uncheck
page.locator("input[type=checkbox]").check()
page.locator("input[type=checkbox]").uncheck()

# Hover
page.locator("button").hover()

# Keyboard
page.keyboard.press("ArrowRight")
page.keyboard.press("Space")
```

## Test Markers

```python
# Mark test as slow
@pytest.mark.slow
def test_large_dataset():
    ...

# Skip test
@pytest.mark.skip("Not implemented yet")
def test_feature():
    ...

# Skip conditionally
@pytest.mark.skipif(condition, reason="...")
def test_feature():
    ...

# Parametrize
@pytest.mark.parametrize("value,expected", [
    (1, "1"),
    (10, "10"),
])
def test_format(value, expected):
    assert format_value(value) == expected
```

## CI/CD Quick Check

```bash
# Before committing (fast)
uv run pytest tests/test_components/ -v

# Before pushing (comprehensive)
uv run pytest tests/ -v --cov=openadapt_viewer

# Full CI simulation
uv sync --extra dev
uv run playwright install chromium
uv run pytest tests/ -v --cov=openadapt_viewer --cov-report=html
```

## Troubleshooting Checklist

**Test fails intermittently**:
- [ ] Add explicit `wait_for_selector()`
- [ ] Remove hardcoded `wait_for_timeout()`
- [ ] Use deterministic test data (no `random`)
- [ ] Check Alpine.js initialization

**Can't find element**:
- [ ] Add `page.wait_for_selector(".element")`
- [ ] Try semantic selector: `page.get_by_role("button")`
- [ ] Use `page.pause()` to inspect
- [ ] Check element actually exists in HTML

**Visual regression fails**:
- [ ] Update baseline: `pytest --update-snapshots`
- [ ] Increase threshold: `max_diff_pixels=100`
- [ ] Wait for fonts: `page.wait_for_load_state("networkidle")`
- [ ] Disable animations in test mode

**Tests too slow**:
- [ ] Run in parallel: `pytest -n auto`
- [ ] Use fixture caching: `scope="session"`
- [ ] Skip slow tests: `pytest -m "not slow"`

## Resources

- [TESTING_STRATEGY.md](../TESTING_STRATEGY.md) - Full strategy
- [TESTING_GUIDE.md](TESTING_GUIDE.md) - Complete guide
- [Playwright Docs](https://playwright.dev/python/docs/intro)
- [pytest Docs](https://docs.pytest.org/)

---

**Last Updated**: January 17, 2026
