# UI Testing Implementation Summary

**Date**: January 17, 2026
**Status**: Implementation Complete
**Project**: openadapt-viewer

## What Was Delivered

### 1. Comprehensive Testing Strategy Document

**File**: [`TESTING_STRATEGY.md`](TESTING_STRATEGY.md)

**Contents**:
- Complete testing architecture (layered testing pyramid)
- Tool selection rationale (Playwright + pytest)
- Test organization structure
- Specific test cases for each viewer type
- Development workflow (TDD)
- CI/CD integration
- Performance optimization strategies
- Troubleshooting guide
- Success criteria and metrics
- Future improvements roadmap

**Key Decisions Documented**:
- Why Playwright over alternatives (Selenium, Cypress, Puppeteer)
- Why pytest as test framework
- Test granularity: When to use unit vs component vs integration tests
- Visual regression approach with screenshot comparison
- Alpine.js testing strategy

### 2. Practical Testing Guide

**File**: [`docs/TESTING_GUIDE.md`](docs/TESTING_GUIDE.md)

**Contents**:
- Quick start commands
- How to write tests (unit, component, integration, visual)
- Using fixtures for test data
- Running and debugging tests
- Common patterns (Alpine.js, async, errors, interactions)
- Best practices
- Troubleshooting common issues
- Guide for Claude Code on interpreting failures and writing tests

**Target Audiences**:
- Developers: Practical how-to guide
- Claude Code: Clear patterns for AI-assisted testing

### 3. Updated CLAUDE.md

**File**: [`CLAUDE.md`](CLAUDE.md) (Testing section added)

**Changes**:
- Added prominent testing section
- Quick testing commands
- Testing philosophy
- Specific guidance for Claude Code
- Links to comprehensive documentation

### 4. Example Integration Test Suite

**File**: [`tests/integration/test_benchmark_workflow_example.py`](tests/integration/test_benchmark_workflow_example.py)

**Contents**:
- 15+ comprehensive integration tests
- Demonstrates real user workflows
- Tests for:
  - Initial load and summary display
  - Domain and status filtering
  - Task selection and detail panel
  - Playback controls and navigation
  - Step list interaction
  - Action details display
  - Error handling for failed tasks
  - Accessibility (keyboard navigation)
  - Progress bar updates

**Purpose**: Serves as reference implementation showing how to write good integration tests.

## Testing Architecture Overview

### Layered Testing Pyramid

```
          /\
         /E2E\         5% - Full workflows (slow, high-level)
        /-----\
       /Integ \        15% - Multiple components
      /--------\
     /Component\       50% - Individual UI components
    /------------\
   /Unit & Logic \     30% - Pure functions
  /----------------\
```

### Test Types

| Type | Location | Purpose | Speed | Examples |
|------|----------|---------|-------|----------|
| Unit | `tests/unit/` | Pure functions, data transformations | <10ms | `test_format_duration()`, `test_parse_action()` |
| Component | `tests/component/` | Individual UI elements | ~500ms | `test_screenshot_with_overlays()`, `test_playback_controls()` |
| Integration | `tests/integration/` | Multiple components, workflows | ~2s | `test_benchmark_viewer_full_workflow()` |
| Visual | `tests/visual/` | Screenshot comparison | ~2s | `test_benchmark_list_layout()` |

### Tool Stack

- **Playwright**: Modern browser automation (Python)
- **pytest**: Test framework
- **pytest-playwright**: Playwright fixtures for pytest
- **pytest-html**: HTML test reports
- **pytest-xdist**: Parallel execution
- **pytest-cov**: Code coverage

### Why Playwright?

**Chosen over Selenium, Cypress, Puppeteer**:

1. ✅ Python native (no context switching)
2. ✅ Modern API with auto-waiting
3. ✅ Fast parallel execution
4. ✅ Cross-browser (Chrome, Firefox, WebKit)
5. ✅ File:// URL support (can test standalone HTML)
6. ✅ Component isolation testing
7. ✅ Built-in visual regression (screenshot comparison)
8. ✅ Active development with 2026 improvements

**2026 Improvements**:
- Smarter locators with AI assistance
- Enhanced HTML reporter
- Better component isolation
- Improved trace viewer for debugging

## Key Features

### 1. Component Isolation Testing

Test individual components without full page load:

```python
def test_screenshot_with_overlay(page):
    from openadapt_viewer.components import screenshot_display

    html = screenshot_display(
        "test.png",
        overlays=[{"type": "click", "x": 0.5, "y": 0.3}]
    )
    page.set_content(html)

    assert page.locator(".oa-overlay-click").is_visible()
```

### 2. Integration Testing

Test full workflows with real user interactions:

```python
def test_benchmark_viewer_workflow(page, benchmark_viewer_html):
    page.goto(f"file://{benchmark_viewer_html}")
    page.wait_for_selector(".task-item")

    # Click task
    page.locator(".task-item").first.click()

    # Verify detail panel
    assert page.locator(".task-detail").is_visible()

    # Test playback
    page.locator("button:has-text('Play')").click()
    assert page.locator("button:has-text('Pause')").is_visible()
```

### 3. Visual Regression

Detect unintended layout/styling changes:

```python
def test_benchmark_layout(page, viewer_path):
    page.goto(f"file://{viewer_path}")
    page.wait_for_selector(".task-list-item")

    expect(page.locator(".task-list")).to_have_screenshot(
        "benchmark-task-list.png",
        max_diff_pixels=100
    )
```

### 4. Alpine.js Testing

Special handling for reactive components:

```python
def test_alpine_reactive_filter(page):
    page.set_content(html_with_alpine)

    # Wait for Alpine initialization
    page.wait_for_function("window.Alpine !== undefined")

    # Test reactivity
    page.select_option("select", "success")
    assert page.locator("[x-text='filter']").text_content() == "success"
```

### 5. Fixture System

Reusable test data and setup:

```python
@pytest.fixture
def sample_benchmark_run():
    """Generate realistic benchmark data."""
    return {
        "run_id": "test-001",
        "tasks": [...],
        "executions": [...],
    }

@pytest.fixture
def benchmark_viewer_html(tmp_path, sample_benchmark_run):
    """Generate viewer HTML for testing."""
    output_path = tmp_path / "viewer.html"
    generate_benchmark_html(run_data=sample_benchmark_run, output_path=output_path)
    return output_path
```

## Development Workflow

### TDD Workflow (Recommended)

1. **Write failing test** (Red)
   ```bash
   uv run pytest tests/component/test_new_feature.py -v
   # FAILED
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

4. **Full suite before commit**
   ```bash
   uv run pytest tests/ -v
   # All PASSED
   ```

### Integration with Claude Code

**Benefits**:
1. **Clear success criteria**: Tests define what "working" means
2. **Immediate feedback**: Claude knows right away if it broke something
3. **Guided debugging**: Test failures point to exact problem
4. **Confidence**: Can refactor knowing tests catch issues

**Prompting Patterns**:
```
"Write a test for the screenshot component that verifies overlays appear"

"Make this test pass: test_playback_controls_pause_resume"

"The test test_filter_workflow is failing. Fix the filter component."

"Add visual regression tests for the benchmark viewer"
```

## Quick Start Guide

### Setup (One-time)

```bash
cd /path/to/openadapt-viewer

# Install with dev dependencies
uv sync --extra dev

# Install Playwright browsers
uv run playwright install chromium
```

### Running Tests

```bash
# All tests
uv run pytest tests/ -v

# By category
uv run pytest tests/unit/ -v           # Fast unit tests
uv run pytest tests/component/ -v      # Component tests
uv run pytest tests/integration/ -v    # Integration tests

# Specific file
uv run pytest tests/component/test_screenshot.py -v

# With coverage
uv run pytest tests/ --cov=openadapt_viewer --cov-report=html
open htmlcov/index.html

# Parallel (faster)
uv run pytest tests/ -n auto -v
```

### Writing a New Test

```python
# tests/component/test_my_feature.py
from playwright.sync_api import Page

def test_my_feature_renders(page: Page):
    """Test that my feature renders correctly."""
    from openadapt_viewer.components import my_feature

    # ARRANGE: Set up test data
    html = my_feature(data="test")

    # ACT: Render in browser
    page.set_content(html)

    # ASSERT: Verify behavior
    assert page.locator(".oa-my-feature").is_visible()
    assert page.locator(".oa-my-feature").text_content() == "test"
```

### Debugging Tests

```bash
# Show print statements
uv run pytest tests/ -v -s

# Show local variables on failure
uv run pytest tests/ -v -l

# Run with headed browser (see what's happening)
PWDEBUG=1 uv run pytest tests/component/test_screenshot.py -v

# Add breakpoint in test
def test_something(page):
    import pdb; pdb.set_trace()
    # or
    page.pause()  # Opens Playwright inspector
```

## CI/CD Integration

### GitHub Actions Workflow

```yaml
name: Test Viewers
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: uv sync --extra dev
      - run: uv run playwright install --with-deps chromium
      - run: uv run pytest tests/unit/ -v
      - run: uv run pytest tests/component/ -v
      - run: uv run pytest tests/integration/ -v
```

### Local CI Simulation

```bash
# Run full CI locally before pushing
cd /path/to/openadapt-viewer

# Fresh install
uv sync --extra dev
uv run playwright install chromium

# Run all tests with coverage
uv run pytest tests/ -v --cov=openadapt_viewer --cov-report=html

# Check coverage (aim for >80%)
open htmlcov/index.html

# Run parallel (faster)
uv run pytest tests/ -n auto -v
```

## Test Coverage Goals

| Metric | Target | Current |
|--------|--------|---------|
| Line coverage | >80% | TBD (run tests to measure) |
| Branch coverage | >70% | TBD |
| Test count | 100+ | ~50 (existing + example) |
| Test execution time | <60s | ~20s (existing) |
| Flakiness rate | <2% | TBD |

## Success Metrics

### Quality Metrics

- **Test execution time**: <60s for full suite
- **Flakiness rate**: <2% (tests should be deterministic)
- **Bug detection rate**: >90% (tests should catch most regressions)
- **Coverage**: >80% line coverage for component code

### Developer Experience Metrics

- **Setup time**: <5 minutes from clone to running tests
- **Feedback loop**: <10s for unit tests, <60s for integration
- **Test failure clarity**: Failures point to exact problem
- **Maintenance burden**: Tests shouldn't break with minor refactors

## Common Patterns

### Testing Alpine.js Components

```python
def test_alpine_reactive_state(page):
    html = """<div x-data="{ count: 0 }">
        <span x-text="count"></span>
        <button @click="count++">+</button>
    </div>"""
    page.set_content(html)

    # Wait for Alpine
    page.wait_for_function("window.Alpine !== undefined")

    # Test reactivity
    assert page.locator("span").text_content() == "0"
    page.locator("button").click()
    assert page.locator("span").text_content() == "1"
```

### Testing Async Operations

```python
def test_async_data_loading(page):
    page.goto("file:///viewer.html")

    # Wait for data to load
    page.wait_for_selector(".task-list-item")

    # Or wait for specific condition
    page.wait_for_function("window.dataLoaded === true")
```

### Testing Error States

```python
def test_error_handling(page):
    page.goto("file:///viewer.html")
    page.evaluate("window.loadData({ invalid: true })")

    # Verify error shown
    assert page.locator(".error-message").is_visible()
    assert "Invalid data" in page.locator(".error-message").text_content()
```

## Troubleshooting

### Test Fails in CI but Passes Locally

**Solution**:
- Add explicit waits: `page.wait_for_selector(".element")`
- Use deterministic test data (no `random`, fixed timestamps)
- Check viewport size consistency
- Wait for fonts/assets to load

### Flaky Tests

**Solution**:
- Replace `wait_for_timeout()` with `wait_for_selector()`
- Use `page.wait_for_load_state("networkidle")`
- Ensure Alpine.js initialized: `page.wait_for_function("window.Alpine")`
- Increase visual regression threshold: `max_diff_pixels=100`

### Playwright Can't Find Element

**Solution**:
- Add explicit wait: `page.wait_for_selector(".element")`
- Use better selectors: `page.get_by_role("button", name="Submit")`
- Debug: `page.pause()` to inspect page
- Check Alpine.js init: `page.wait_for_function("window.Alpine")`

## Future Improvements

### Short-term (Q1 2026)

- [ ] Add performance tests (load 1000+ tasks)
- [ ] Expand visual regression to all viewers
- [ ] Add accessibility tests (ARIA, keyboard navigation)
- [ ] Document testing patterns in examples

### Medium-term (Q2 2026)

- [ ] Cross-browser testing (Firefox, Safari)
- [ ] Mutation testing (verify tests catch bugs)
- [ ] Screenshot diffing UI
- [ ] Performance profiling

### Long-term (Q3+ 2026)

- [ ] E2E testing with real data
- [ ] Automated test generation
- [ ] Property-based testing
- [ ] Test visualization dashboard

## References

### Official Documentation

- [TESTING_STRATEGY.md](TESTING_STRATEGY.md) - Comprehensive strategy
- [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md) - Practical guide
- [Playwright Python Docs](https://playwright.dev/python/docs/intro)
- [pytest Documentation](https://docs.pytest.org/)

### Best Practices

- [15 Best Practices for Playwright testing in 2026](https://www.browserstack.com/guide/playwright-best-practices)
- [9 Playwright Best Practices and Pitfalls to Avoid](https://betterstack.com/community/guides/testing/playwright-best-practices/)
- [Playwright Best Practices (Official)](https://playwright.dev/docs/best-practices)

### Tools

- [pytest-playwright Plugin](https://github.com/microsoft/playwright-pytest)
- [pytest-html](https://github.com/pytest-dev/pytest-html) - HTML test reports
- [pytest-xdist](https://github.com/pytest-dev/pytest-xdist) - Parallel execution
- [pytest-cov](https://github.com/pytest-dev/pytest-cov) - Code coverage

## Conclusion

This implementation provides:

1. **Systematic regression prevention**: Automated tests catch breaks before they happen
2. **Fast development feedback**: Know immediately if changes broke something
3. **Component isolation**: Test each piece independently
4. **AI-friendly structure**: Clear patterns Claude Code can maintain
5. **Developer confidence**: Make changes without fear

**Key Achievement**: Transformed unpleasant, buggy viewer development into a confident, systematic process with automated safety nets.

**Next Steps**:
1. Run existing tests to establish baseline coverage
2. Add tests for new features using TDD workflow
3. Set up CI/CD integration
4. Expand visual regression testing
5. Measure and improve coverage over time

---

**Document Version**: 1.0
**Date**: January 17, 2026
**Author**: OpenAdapt Team with Claude Code
**Status**: Implementation Complete
