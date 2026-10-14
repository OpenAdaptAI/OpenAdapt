# Advanced Search Functionality

## Overview

OpenAdapt viewers use an advanced token-based search algorithm that provides flexible, user-friendly searching across episodes, demos, and other content. The search is designed to be forgiving and intuitive, matching how users naturally think about search.

**Key Problem Solved:** The original search was too strict - searching for "nightshift" wouldn't find "Disable night shift" because of the space. The new search solves this and many other common search frustrations.

## Features

### 1. Case-Insensitive Matching

Search is always case-insensitive:

```
"NightShift" → matches → "disable night shift"
"NOTEPAD"    → matches → "Open notepad"
```

### 2. Token-Based Search

The search breaks both the query and searchable text into tokens (words), then checks if all query tokens match:

```
Query: "nightshift"
Text:  "Disable night shift"

Normalized query tokens:  ["nightshift"]
Normalized text tokens:   ["disable", "night", "shift"]

Match: ✓ (because "nightshift" contains both "night" and "shift")
```

### 3. Token Order Independence

Query tokens can match in any order:

```
"shift night"    → matches → "Disable night shift"
"notepad open"   → matches → "Open notepad application"
"dark mode"      → matches → "Enable mode for dark viewing"
```

### 4. Partial Word Matching

Query tokens match partial words:

```
"nightsh"     → matches → "nightshift schedule"
"note"        → matches → "notepad editor"
"config"      → matches → "configuration settings"
```

### 5. Whitespace Normalization

Punctuation and whitespace are normalized:

```
"night-shift" → normalized to → "night shift"
"note_pad"    → normalized to → "note pad"
```

### 6. Multi-Field Search

Search across multiple fields simultaneously:

```javascript
// Searches name, description, AND steps
advancedSearch(episodes, "nightshift", ['name', 'description', 'steps'])
```

## How It Works

### Algorithm

The search algorithm follows these steps:

1. **Normalize Query**
   - Convert to lowercase
   - Replace punctuation with spaces
   - Collapse multiple spaces
   - Split into tokens

2. **Normalize Searchable Text**
   - Build text from all specified fields
   - Convert to lowercase
   - Replace punctuation with spaces
   - Collapse multiple spaces

3. **Match Tokens**
   - For each query token, check if it matches any search token
   - Match if: token contains query OR query contains token
   - ALL query tokens must have at least one match

4. **Return Results**
   - Return items where all query tokens matched

### Example Walkthrough

**Query:** `"nightshift"`

**Episode:**
```json
{
  "name": "Disable night shift",
  "description": "Turn off the night shift feature",
  "steps": ["Open settings", "Navigate to display", "Toggle off"]
}
```

**Process:**

1. Normalize query: `"nightshift"` → `["nightshift"]`

2. Build search text:
   ```
   "Disable night shift Turn off the night shift feature Open settings Navigate to display Toggle off"
   ```

3. Normalize search text:
   ```
   ["disable", "night", "shift", "turn", "off", "the", "night", "shift",
    "feature", "open", "settings", "navigate", "to", "display", "toggle", "off"]
   ```

4. Match token "nightshift":
   - Check against each search token
   - "night" is in "nightshift" ✓
   - "shift" is in "nightshift" ✓
   - Match found!

5. All query tokens matched → Return this episode

## Implementation

### Standalone HTML Version

For self-contained HTML viewers (like `segmentation_viewer.html`), the search function is inlined:

```javascript
function advancedSearch(items, query, fields = ['name', 'description', 'steps']) {
    if (!query || query.trim() === '') {
        return items;
    }

    // Tokenize and normalize query
    const queryTokens = query
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, ' ')  // Replace punctuation with space
        .replace(/\s+/g, ' ')           // Normalize whitespace
        .trim()
        .split(' ')
        .filter(t => t.length > 0);

    if (queryTokens.length === 0) {
        return items;
    }

    return items.filter(item => {
        // Build searchable text from all specified fields
        const searchText = fields
            .map(field => {
                const value = item[field];
                if (Array.isArray(value)) {
                    // Handle array fields like steps
                    return value.map(v => typeof v === 'string' ? v : v.description || '').join(' ');
                }
                return value || '';
            })
            .join(' ')
            .toLowerCase()
            .replace(/[^a-z0-9\s]/g, ' ')
            .replace(/\s+/g, ' ');

        // All query tokens must match somewhere in the searchable text
        return queryTokens.every(queryToken => {
            const searchTokens = searchText.split(' ');
            return searchTokens.some(searchToken =>
                searchToken.includes(queryToken) || queryToken.includes(searchToken)
            );
        });
    });
}
```

### Module Version

For Python-based viewers or modular JavaScript apps, use the `search.js` module:

```javascript
import { searchItems, highlightMatches } from './src/openadapt_viewer/search.js';

// Basic search
const results = searchItems(episodes, "nightshift", {
    fields: ['name', 'description', 'steps']
});

// With ranking
const rankedResults = searchItems(episodes, "nightshift", {
    fields: ['name', 'description'],
    rankResults: true  // Sort by relevance
});

// With fuzzy matching (typo tolerance)
const fuzzyResults = searchItems(episodes, "nitshift", {
    fields: ['name', 'description'],
    fuzzyThreshold: 2  // Allow up to 2 character edits
});
```

## Usage in Viewers

### Segmentation Viewer

**Location:** `/path/to/openadapt-viewer/segmentation_viewer.html`

**Features:**
- Searches across episode name, description, and steps
- Updates results in real-time as you type
- Works in combination with recording filter

**Usage:**
```html
<input type="text" id="search-input"
       placeholder="Search by name, description, or steps..."
       oninput="filterEpisodes()">
```

### Synthetic Demo Viewer

**Location:** `/path/to/openadapt-viewer/synthetic_demo_viewer.html`

**Features:**
- Searches demo task names and domains
- Updates dropdown options dynamically
- Shows match count
- Works with domain filter

**Usage:**
```html
<input type="text" id="search-input"
       placeholder="Search by task name..."
       oninput="updateTaskList()">
```

## Test Cases

The following test cases validate the search algorithm:

| Query | Text | Should Match | Reason |
|-------|------|--------------|--------|
| `"nightshift"` | `"Disable night shift"` | ✓ Yes | Single word matches space-separated words |
| `"night shift"` | `"Configure nightshift schedule"` | ✓ Yes | Space-separated words match single word |
| `"shift night"` | `"Disable night shift"` | ✓ Yes | Token order independence |
| `"nightsh"` | `"Configure nightshift schedule"` | ✓ Yes | Partial word matching |
| `"disable dark"` | `"Enable dark mode"` | ✗ No | Only 'dark' matches, 'disable' doesn't |
| `"notepad"` | `"Open notepad"` | ✓ Yes | Exact word match |
| `"note pad"` | `"Open notepad"` | ✓ Yes | Space-separated matches compound word |
| `"dark night"` | `"Enable dark mode"` | ✗ No | Only 'dark' matches, 'night' doesn't |

**Interactive Testing:** Open `/path/to/openadapt-viewer/test_search.html` in a browser to test the algorithm interactively.

## Advanced Features (Module Only)

### Fuzzy Matching

The module version supports fuzzy matching using Levenshtein distance:

```javascript
import { searchItems } from './src/openadapt_viewer/search.js';

// Allow typos within 2 character edits
const results = searchItems(episodes, "nitshift", {
    fuzzyThreshold: 2  // Matches "nightshift" (1 character difference)
});
```

### Relevance Scoring

Rank results by relevance:

```javascript
const results = searchItems(episodes, "night shift mode", {
    rankResults: true  // Returns best matches first
});
```

**Scoring:**
- Exact token match: 3 points
- Partial match (substring): 2 points
- Fuzzy match: 1 point

### Match Highlighting

Highlight matched terms in results:

```javascript
import { highlightMatches } from './src/openadapt_viewer/search.js';

const highlighted = highlightMatches("Disable night shift", "nightshift");
// Returns: "Disable <mark>night</mark> <mark>shift</mark>"
```

## Comparison with Reference Implementation

### llm-council PR #139

The reference implementation uses **SQLite FTS5** (Full-Text Search):
- Server-side indexing
- SQL database queries
- Optimized for large datasets
- Built-in snippet generation

### OpenAdapt Approach

Our implementation uses **client-side JavaScript**:
- No server required (standalone HTML)
- Works with `file://` protocol
- Instant search (no network latency)
- Suitable for small-to-medium datasets (100s to 1000s of items)

**Why Different?**
- OpenAdapt viewers are standalone HTML files
- No backend server or database
- Need to work offline and locally
- Simpler deployment (just open the HTML file)

**Similarities:**
- Both are case-insensitive
- Both support token-based matching
- Both prioritize user experience over strict matching

## Performance Considerations

### When to Use

✓ **Good for:**
- Standalone HTML viewers
- Small to medium datasets (< 10,000 items)
- Real-time search-as-you-type
- Offline/local usage

✗ **Not ideal for:**
- Very large datasets (> 10,000 items)
- Complex boolean queries (AND, OR, NOT)
- Phrase search ("exact phrase")
- Database-backed applications

### Optimization Tips

1. **Limit fields searched:**
   ```javascript
   // Faster: search only essential fields
   advancedSearch(items, query, ['name'])

   // Slower: search everything
   advancedSearch(items, query, ['name', 'description', 'steps', 'metadata'])
   ```

2. **Debounce search input:**
   ```javascript
   let searchTimeout;
   searchInput.addEventListener('input', (e) => {
       clearTimeout(searchTimeout);
       searchTimeout = setTimeout(() => {
           filterEpisodes();
       }, 300);  // Wait 300ms after user stops typing
   });
   ```

3. **Pre-normalize data:**
   ```javascript
   // Cache normalized search text
   const processedItems = items.map(item => ({
       ...item,
       _searchText: normalizeText(item.name + ' ' + item.description)
   }));
   ```

## Future Enhancements

Possible improvements for future versions:

1. **Phrase Search**
   - Support quotes: `"exact phrase"` matches only that phrase

2. **Boolean Operators**
   - AND, OR, NOT operators
   - Example: `nightshift AND disable` or `notepad OR wordpad`

3. **Field-Specific Search**
   - Search specific fields: `name:nightshift` or `description:disable`

4. **Search History**
   - Remember recent searches
   - Quick re-run of common queries

5. **Regex Support**
   - Advanced users can use regex patterns
   - Example: `/night.*shift/`

6. **Synonym Support**
   - "disable" matches "turn off"
   - "notepad" matches "text editor"

## Reference

- **Implementation:** `/path/to/openadapt-viewer/src/openadapt_viewer/search.js`
- **Test File:** `/path/to/openadapt-viewer/test_search.html`
- **Inspiration:** [karpathy/llm-council PR #139](https://github.com/karpathy/llm-council/pull/139)

## Contributing

When adding search to new viewers:

1. **Copy the `advancedSearch` function** from an existing viewer
2. **Update field names** to match your data structure
3. **Add search input** to the HTML
4. **Connect input handler** to filter function
5. **Test with various queries** to ensure it works

Example template:

```html
<input type="text" id="search-input"
       placeholder="Search..."
       oninput="handleSearch()">

<script>
function advancedSearch(items, query, fields) {
    // Copy implementation from segmentation_viewer.html
}

function handleSearch() {
    const query = document.getElementById('search-input').value;
    const results = advancedSearch(allItems, query, ['name', 'description']);
    renderResults(results);
}
</script>
```

---

**Last Updated:** January 2026
**Author:** Claude Code (based on requirements and llm-council PR #139)
