# OpenAdapt Viewer JavaScript Utilities

Shared JavaScript libraries for OpenAdapt viewers. These utilities provide common functionality for search, filtering, and formatting across all viewers.

## Overview

The JavaScript utilities are designed to be:

- **Reusable**: Use across segmentation, synthetic, retrieval, and custom viewers
- **Well-documented**: Comprehensive JSDoc comments and examples
- **Flexible**: Works as ES6 modules or standalone scripts
- **Tested**: Used in production viewers with real data

## Files

### 1. `search.js` - Advanced Search

Token-based search with flexible matching:

- **Case-insensitive**: "NightShift" finds "night shift"
- **Token-based**: "nightshift" finds "Disable night shift" (normalizes spaces)
- **Token order independent**: "shift night" finds "night shift"
- **Partial matching**: "nightsh" finds "nightshift"
- **Multi-field**: Search across name, description, steps, etc.

**Functions:**
- `advancedSearch(items, query, fields)` - Main search function
- `simpleSearch(items, query, fields)` - Basic substring search
- `fuzzySearch(items, query, fields, maxDistance)` - Levenshtein distance matching
- `highlightMatches(text, query)` - Highlight matching tokens in HTML

**Example:**
```javascript
const results = advancedSearch(episodes, "nightshift", ['name', 'description', 'steps']);
// Finds "Disable night shift" even with different spacing
```

### 2. `filters.js` - Multi-Field Filtering

Flexible filtering with combining logic:

- **Single field**: `{ domain: "notepad" }`
- **Multi-field**: `{ domain: "notepad", status: "passed" }`
- **Range filtering**: `{ duration: { min: 10, max: 60 } }`
- **Array matching**: `{ tags: ["automation", "demo"] }`
- **Custom predicates**: `{ steps: (steps) => steps.length > 5 }`
- **Combine modes**: AND / OR logic

**Functions:**
- `filterItems(items, filters, combineMode)` - Main filter function
- `createFilter(filters, combineMode)` - Create reusable filter function
- `filterByValues(items, field, values)` - Filter by multiple values
- `filterByRange(items, field, min, max)` - Numeric range filtering
- `filterByDateRange(items, field, start, end)` - Date range filtering
- `getUniqueValues(items, field)` - Get unique values (for dropdowns)
- `getFilterStats(items, field)` - Get value counts
- `createFilterManager()` - Stateful filter manager

**Example:**
```javascript
const results = filterItems(episodes, {
  domain: "notepad",
  duration: { min: 20, max: 60 }
}, 'AND');
```

### 3. `utils.js` - Common Utilities

Format, data loading, and event handling utilities:

**Formatting:**
- `formatDuration(seconds, compact)` - "45.5s", "1m 5s", "1:23:45"
- `formatTimestamp(timestamp, options)` - "Jan 1, 2024, 12:00 AM"
- `formatRelativeTime(timestamp)` - "2 hours ago", "in 1 day"
- `formatNumber(num, decimals)` - "1,234,567", "1,234.57"
- `formatPercentage(value, isDecimal, decimals)` - "85.6%"
- `formatFileSize(bytes, decimals)` - "1.46 MB", "1.00 GB"

**Data Loading:**
- `loadJSON(path)` - Load JSON from URL
- `loadJSONSafe(path, fallback)` - Load with error handling
- `loadMultipleJSON(paths)` - Load multiple files in parallel
- `loadImage(src)` - Preload image

**Event Handling:**
- `debounce(func, wait)` - Debounce function calls
- `throttle(func, limit)` - Throttle function calls
- `waitForElement(selector, timeout)` - Wait for DOM element

**DOM Utilities:**
- `escapeHTML(text)` - Prevent XSS
- `createElement(tag, attrs, children)` - Create element with attributes

**Helpers:**
- `deepClone(obj)` - Deep clone object
- `getNestedProperty(obj, path, default)` - Safe nested property access
- `groupBy(items, field)` - Group array by field
- `sortBy(items, field, ascending)` - Sort array by field

**Example:**
```javascript
formatDuration(65)        // "1m 5s"
formatDuration(65, true)  // "1:05"
formatPercentage(0.856)   // "85.6%"
```

## Usage

### With PageBuilder (Recommended)

Enable utilities when creating a PageBuilder:

```python
from openadapt_viewer.builders import PageBuilder

builder = PageBuilder(
    title="My Viewer",
    include_search_js=True,
    include_filter_js=True,
    include_utils_js=True,
)

# JS functions are now available in your custom scripts
builder.add_script("""
    const results = advancedSearch(episodes, query, ['name', 'description']);
    const formatted = formatDuration(episode.duration);
""")
```

### As ES6 Modules

If using a build system (webpack, vite, etc.):

```javascript
import { advancedSearch } from './search.js';
import { filterItems, createFilterManager } from './filters.js';
import { formatDuration, loadJSON } from './utils.js';

const results = advancedSearch(episodes, "nightshift", ['name', 'description']);
const filtered = filterItems(results, { domain: "system" });
const duration = formatDuration(episode.duration);
```

### As Standalone Scripts

In HTML without a build system:

```html
<script src="src/openadapt_viewer/js/search.js"></script>
<script src="src/openadapt_viewer/js/filters.js"></script>
<script src="src/openadapt_viewer/js/utils.js"></script>

<script>
    // Functions are available on window
    const results = window.advancedSearch(episodes, query, ['name']);
    const duration = window.formatDuration(45.5);
</script>
```

### Inline (for standalone HTML files)

Copy the function directly from the source:

```html
<script>
    // Copy from search.js
    function advancedSearch(items, query, fields = ['name', 'description']) {
        // ... implementation
    }
</script>
```

## Integration Examples

### Search + Filter + Format

```javascript
// Search episodes
const searchResults = advancedSearch(episodes, searchQuery, ['name', 'description', 'steps']);

// Filter results
const filteredResults = filterItems(searchResults, {
    domain: selectedDomain,
    duration: { min: 10, max: 60 }
}, 'AND');

// Display results
filteredResults.forEach(episode => {
    const card = `
        <div class="episode-card">
            <h3>${episode.name}</h3>
            <p>${episode.description}</p>
            <span>Duration: ${formatDuration(episode.duration)}</span>
            <span>Created: ${formatRelativeTime(episode.created_at)}</span>
        </div>
    `;
    container.innerHTML += card;
});
```

### Filter Manager with State

```javascript
const filterManager = createFilterManager();

// Add filters
filterManager.addFilter('domain', 'notepad');
filterManager.addFilter('duration', { min: 20, max: 60 });

// Apply to data
const results = filterManager.apply(episodes);

// Update filters
filterManager.removeFilter('duration');
filterManager.addFilter('status', 'passed');

// Check state
if (filterManager.hasActiveFilters()) {
    console.log(`Active filters: ${filterManager.getFilterCount()}`);
}

// Clear all
filterManager.clearAll();
```

### Debounced Search

```javascript
const handleSearch = debounce((query) => {
    const results = advancedSearch(episodes, query, ['name', 'description']);
    renderResults(results);
}, 300);

searchInput.addEventListener('input', (e) => handleSearch(e.target.value));
```

## Testing

See the example viewer:

```bash
# Generate example viewer
uv run python examples/js_utilities_example.py

# Open in browser
open js_utilities_demo.html
```

The example demonstrates:
1. Advanced search with real-time filtering
2. Multi-field filtering with domain and duration
3. Format utilities for duration, timestamp, percentage
4. Combined search + filter + format workflow

## Real-World Usage

These utilities are used in:

- **Segmentation Viewer** (`segmentation_viewer.html`): Search and filter episodes
- **Synthetic Demo Viewer** (`synthetic_demo_viewer.html`): Search WAA demos
- **Retrieval Viewer** (`retrieval_viewer.html`): Filter search results

## Best Practices

### 1. Choose the Right Search Function

- **advancedSearch**: Most cases (flexible, forgiving)
- **simpleSearch**: When performance is critical and substring matching is enough
- **fuzzySearch**: When users may have typos (slower)

### 2. Optimize Filter Performance

```javascript
// Good: Combine filters once
const results = filterItems(episodes, {
    domain: "notepad",
    duration: { min: 20, max: 60 },
    status: "passed"
});

// Bad: Chain multiple filter calls (slower)
let results = filterItems(episodes, { domain: "notepad" });
results = filterItems(results, { duration: { min: 20, max: 60 } });
results = filterItems(results, { status: "passed" });
```

### 3. Use Debounce for Search Inputs

```javascript
// Good: Debounce search to avoid excessive filtering
const handleSearch = debounce((query) => {
    const results = advancedSearch(episodes, query);
    renderResults(results);
}, 300);

// Bad: Filter on every keystroke (causes lag)
searchInput.addEventListener('input', (e) => {
    const results = advancedSearch(episodes, e.target.value);
    renderResults(results);
});
```

### 4. Format Consistently

```javascript
// Good: Use formatDuration for all durations
episode.duration_text = formatDuration(episode.duration);

// Bad: Mix different formats
episode.duration_text = episode.duration < 60
    ? `${episode.duration.toFixed(1)}s`
    : `${Math.floor(episode.duration / 60)}m ${Math.floor(episode.duration % 60)}s`;
```

## Migration Guide

### From Inline Functions to Utilities

**Before:**
```javascript
// Inline search in segmentation_viewer.html
function advancedSearch(items, query, fields) {
    // ... 50 lines of code
}
```

**After:**
```python
# In Python
builder = PageBuilder(include_search_js=True)

# JavaScript code stays the same
const results = advancedSearch(episodes, query, ['name', 'description']);
```

### From Custom Formatting to Utils

**Before:**
```javascript
function formatDuration(seconds) {
    if (seconds < 60) return seconds.toFixed(1) + 's';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}m ${secs}s`;
}
```

**After:**
```python
builder = PageBuilder(include_utils_js=True)
```

## Contributing

When adding new utilities:

1. **Add JSDoc comments** with examples
2. **Support both module and standalone usage** (check `typeof window !== 'undefined'`)
3. **Write examples** in `examples/js_utilities_example.py`
4. **Test in real viewers** (segmentation, synthetic, retrieval)
5. **Update this README**

## License

Part of the OpenAdapt project. Same license as parent repository.
