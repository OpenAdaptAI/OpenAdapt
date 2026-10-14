# Track 2: Components Used in Benchmark Viewer Refactor

## Component Library Usage Report

This document catalogs all components and CSS classes from the openadapt-viewer library that were used in the benchmark viewer refactor.

---

## Python Components

### 1. PageBuilder (`builders/page_builder.py`)
**Purpose**: High-level page construction with fluent API

**Methods Used**:
- `PageBuilder(title, include_alpine=True)` - Initialize page
- `add_header(title, subtitle)` - Add page header with dark mode toggle
- `add_section(content, title)` - Add content sections
- `add_css(css)` - Add custom CSS
- `add_script(script)` - Add JavaScript
- `render()` - Generate final HTML

**Features Leveraged**:
- Automatic dark mode toggle in header
- Core CSS inclusion from `styles/core.css`
- Alpine.js CDN integration
- Responsive container layout
- Automatic footer generation

---

### 2. metrics_grid (`components/metrics.py`)
**Purpose**: Display statistics in card grid format

**Function**: `metrics_grid(cards, columns=4)`

**Usage**:
```python
metrics_grid([
    {"label": "Total Tasks", "value": run.total_tasks},
    {"label": "Passed", "value": run.passed_tasks, "color": "success"},
    {"label": "Failed", "value": run.failed_tasks, "color": "error"},
    {"label": "Success Rate", "value": f"{run.success_rate * 100:.1f}%", "color": "accent"},
], columns=4)
```

**CSS Classes Generated**:
- `oa-metrics-grid` - Grid container
- `oa-metrics-card` - Individual metric card
- `oa-metrics-card-label` - Metric label
- `oa-metrics-card-value` - Metric value
- `oa-metrics-success`, `oa-metrics-error`, `oa-metrics-accent` - Color variants

---

### 3. domain_stats_grid (`components/metrics.py`)
**Purpose**: Display domain statistics with pass/fail breakdown

**Function**: `domain_stats_grid(domain_stats)`

**Usage**:
```python
domain_stats_grid({
    "office": {"passed": 5, "failed": 2, "total": 7},
    "browser": {"passed": 3, "failed": 1, "total": 4},
})
```

**CSS Classes Generated**:
- `oa-domain-stats-grid` - Grid container
- `oa-domain-stat-item` - Individual domain item

---

### 4. filter_bar (`components/filters.py`)
**Purpose**: Filter dropdowns with Alpine.js integration

**Function**: `filter_bar(filters, alpine_data_name)`

**Usage**:
```python
filter_bar(
    filters=[
        {"id": "domain", "label": "Domain", "options": domain_options},
        {"id": "status", "label": "Status", "options": [
            {"value": "passed", "label": "Passed"},
            {"value": "failed", "label": "Failed"},
        ]},
    ],
    alpine_data_name="viewer",
)
```

**CSS Classes Generated**:
- `oa-filter-bar` - Filter container
- `oa-filter-group` - Individual filter group
- `oa-filter-label` - Filter label
- `oa-filter-dropdown` - Dropdown select

**Features**:
- Automatic Alpine.js `x-model` binding
- "All {label}s" default option
- Responsive flex layout

---

## CSS Classes Used

### List Components (`styles/core.css`)

#### oa-list
**Purpose**: Container for selectable list of items

**Classes Used**:
- `oa-list` - List container
- `oa-list-header` - List header section
- `oa-list-title` - Header title
- `oa-list-subtitle` - Header subtitle (shows count)
- `oa-list-items` - Scrollable items container
- `oa-list-item` - Individual list item
- `oa-list-item-selected` - Selected item highlight
- `oa-list-item-content` - Item content wrapper
- `oa-list-item-title` - Item title
- `oa-list-item-subtitle` - Item subtitle

**Features**:
- Auto-scrolling with max-height
- Hover effects
- Selection highlighting with accent color
- Border-left indicator for selected items

---

### Badge Components (`styles/core.css`)

#### oa-badge
**Purpose**: Status indicators (pass/fail, etc.)

**Classes Used**:
- `oa-badge` - Base badge class
- `oa-badge-success` - Green badge for passed/success
- `oa-badge-error` - Red badge for failed/error
- `oa-badge-warning` - Orange badge for warnings
- `oa-badge-info` - Blue badge for info

**Features**:
- Pill-shaped with rounded corners
- Color-coded backgrounds and text
- Semantic status colors
- Consistent sizing

---

### Playback Controls (`styles/core.css`)

#### oa-playback-controls
**Purpose**: Media playback controls for step navigation

**Classes Used**:
- `oa-playback-controls` - Controls container
- `oa-playback-btn` - Control button
- `oa-playback-btn-primary` - Primary button (play/pause)
- `oa-playback-counter` - Step counter display
- `oa-playback-speed` - Speed selector dropdown

**Features**:
- Icon buttons with hover effects
- Disabled state styling
- Primary button accent color
- Responsive flex layout

**Buttons Generated**:
- Rewind (go to start)
- Previous step
- Play/Pause
- Next step
- Fast forward (go to end)

---

### Timeline (`styles/core.css`)

#### oa-timeline
**Purpose**: Progress bar with click-to-seek

**Classes Used**:
- `oa-timeline` - Timeline container
- `oa-timeline-track` - Progress track background
- `oa-timeline-progress` - Filled progress indicator

**Features**:
- Click to seek functionality
- Smooth progress transitions
- Gradient accent colors
- Pointer cursor on hover

---

### Action Display (`styles/core.css`)

#### oa-action
**Purpose**: Display action types and details

**Classes Used**:
- `oa-action` - Action container
- `oa-action-badge` - Action type badge
- `oa-action-click`, `oa-action-type`, etc. - Type-specific colors
- `oa-action-details` - Action parameters in monospace

**Features**:
- Color-coded by action type
- Monospace for technical details
- Uppercase badge text
- Icon-style formatting

---

## CSS Variables Used

### Colors
```css
--oa-bg-primary: #0a0a0f          /* Main background */
--oa-bg-secondary: #12121a        /* Card/section backgrounds */
--oa-bg-tertiary: #1a1a24         /* Nested element backgrounds */
--oa-border-color: rgba(255, 255, 255, 0.06)  /* Borders */

--oa-text-primary: #f0f0f0        /* Main text */
--oa-text-secondary: #888         /* Secondary text */
--oa-text-muted: #555             /* Muted text */

--oa-accent: #00d4aa              /* Primary accent (teal) */
--oa-accent-dim: rgba(0, 212, 170, 0.15)  /* Accent background */
--oa-accent-secondary: #a78bfa    /* Secondary accent (purple) */

--oa-success: #34d399             /* Success/passed color */
--oa-success-bg: rgba(52, 211, 153, 0.15)  /* Success background */
--oa-error: #ff5f5f               /* Error/failed color */
--oa-error-bg: rgba(255, 95, 95, 0.15)  /* Error background */
--oa-warning: #f59e0b             /* Warning color */
--oa-info: #3b82f6                /* Info color */
```

### Spacing
```css
--oa-space-xs: 4px
--oa-space-sm: 8px
--oa-space-md: 16px
--oa-space-lg: 24px
--oa-space-xl: 32px
```

### Border Radius
```css
--oa-border-radius: 8px
--oa-border-radius-lg: 12px
```

### Typography
```css
--oa-font-sans: -apple-system, BlinkMacSystemFont, "Inter", sans-serif
--oa-font-mono: "SF Mono", Monaco, Consolas, monospace
--oa-font-size-xs: 0.75rem
--oa-font-size-sm: 0.85rem
--oa-font-size-md: 1rem
--oa-font-size-lg: 1.125rem
--oa-font-size-xl: 1.5rem
```

### Transitions
```css
--oa-transition-fast: 0.15s ease
--oa-transition-normal: 0.2s ease
--oa-transition-slow: 0.3s ease
```

---

## Custom CSS Added

### Grid Layout
```css
.oa-task-viewer {
    display: grid;
    grid-template-columns: 1fr 2fr;
    gap: var(--oa-space-lg);
}

@media (max-width: 1024px) {
    .oa-task-viewer {
        grid-template-columns: 1fr;
    }
}
```

### Screenshot Container
```css
.screenshot-container {
    aspect-ratio: 16/9;
    background: var(--oa-bg-tertiary);
    border-radius: var(--oa-border-radius);
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
}

.screenshot-container img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
}
```

---

## Alpine.js Integration

### State Management
```javascript
Alpine.data('viewer', () => ({
    tasks: [...],
    selectedTask: null,
    currentStep: 0,
    isPlaying: false,
    playbackSpeed: 1,
    playbackInterval: null,
    filters: {
        domain: '',
        status: '',
    },

    // Methods: init, selectTask, prevStep, nextStep,
    //          togglePlayback, startPlayback, stopPlayback
    // Computed: filteredTasks
}))
```

### Binding Examples
```html
<!-- Filter binding -->
<select x-model="filters.domain">

<!-- List item binding -->
<div :class="{'oa-list-item-selected': selectedTask?.task_id === task.task_id}">

<!-- Progress binding -->
<div :style="'width: ' + ((currentStep + 1) / selectedTask.steps.length * 100) + '%'">

<!-- Conditional rendering -->
<template x-if="selectedTask.error">
<template x-show="selectedTask.steps.length > 0">

<!-- Event handlers -->
@click="selectTask(task)"
@click="togglePlayback()"
@change="if (isPlaying) { stopPlayback(); startPlayback(); }"
```

---

## Component Count Summary

### Python Components: 4
1. PageBuilder
2. metrics_grid
3. domain_stats_grid
4. filter_bar

### CSS Component Classes: 7 systems
1. List (oa-list-*)
2. Badge (oa-badge-*)
3. Playback Controls (oa-playback-*)
4. Timeline (oa-timeline-*)
5. Action Display (oa-action-*)
6. Metrics (oa-metrics-*)
7. Filters (oa-filter-*)

### CSS Variables: 30+
- Colors: 15
- Spacing: 5
- Typography: 7
- Borders: 2
- Transitions: 3

### Custom Additions: 2
1. Grid layout for task viewer
2. Screenshot container styling

---

## Reusability Analysis

### Components That Can Be Reused in Other Viewers

#### Training Dashboard:
- ✅ PageBuilder (page structure)
- ✅ metrics_grid (training metrics: loss, accuracy, epoch)
- ✅ filter_bar (filter by model, dataset)
- ✅ oa-badge (training status)
- ✅ CSS variables (consistent theming)

#### Capture Playback:
- ✅ PageBuilder (page structure)
- ✅ oa-playback-controls (video playback)
- ✅ oa-timeline (progress tracking)
- ✅ oa-action-* (action display)
- ✅ screenshot-container (frame display)

#### Search Results:
- ✅ PageBuilder (page structure)
- ✅ oa-list-* (result list)
- ✅ filter_bar (filter results)
- ✅ oa-badge (relevance scores)
- ✅ CSS variables (consistent theming)

#### Comparison View:
- ✅ PageBuilder (page structure)
- ✅ metrics_grid (comparison metrics)
- ✅ oa-badge (status indicators)
- ✅ screenshot-container (side-by-side screenshots)
- ✅ CSS variables (consistent theming)

---

## Component Library Coverage

### What We Didn't Need to Create:
- ✅ Header with dark mode toggle (built into PageBuilder)
- ✅ Metrics cards and grid layout (metrics_grid component)
- ✅ Domain statistics grid (domain_stats_grid component)
- ✅ Filter dropdowns with Alpine binding (filter_bar component)
- ✅ Status badges (oa-badge classes)
- ✅ List with selection (oa-list-* classes)
- ✅ Playback controls (oa-playback-* classes)
- ✅ Progress timeline (oa-timeline-* classes)
- ✅ Action display (oa-action-* classes)
- ✅ All CSS variables and theming

### What We Created Custom:
- Task viewer grid layout (2-column responsive grid)
- Screenshot container (16:9 aspect ratio container)
- Alpine.js state management (specific to benchmark viewer)

**Coverage**: ~90% of UI came from component library

---

## Conclusion

The benchmark viewer refactor demonstrates comprehensive use of the component library:
- **4 Python components** for high-level page structure and common UI patterns
- **7 CSS component systems** for consistent, reusable styling
- **30+ CSS variables** for easy theming and customization
- **90% coverage** from existing components (only 10% custom code needed)

This proves the component library provides strong foundations for building viewers quickly while maintaining consistency and quality across the application.
