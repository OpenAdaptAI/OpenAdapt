# Track 2: Code Comparison - Before vs After

## Overview
This document shows side-by-side comparison of the benchmark viewer implementation before and after refactoring.

---

## 1. Summary Metrics Section

### Before (Inline Template)
```html
<!-- Summary Cards -->
<div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
    <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
        <div class="text-sm text-gray-500 dark:text-gray-400">Total Tasks</div>
        <div class="text-2xl font-bold">{{ run.total_tasks }}</div>
    </div>
    <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
        <div class="text-sm text-gray-500 dark:text-gray-400">Passed</div>
        <div class="text-2xl font-bold text-green-600 dark:text-green-400">{{ run.passed_tasks }}</div>
    </div>
    <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
        <div class="text-sm text-gray-500 dark:text-gray-400">Failed</div>
        <div class="text-2xl font-bold text-red-600 dark:text-red-400">{{ run.failed_tasks }}</div>
    </div>
    <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
        <div class="text-sm text-gray-500 dark:text-gray-400">Success Rate</div>
        <div class="text-2xl font-bold text-primary-600 dark:text-primary-400">{{ "%.1f" | format(run.success_rate * 100) }}%</div>
    </div>
</div>
```
**Lines**: ~20

### After (Component-Based)
```python
page.add_section(
    metrics_grid([
        {"label": "Total Tasks", "value": run.total_tasks},
        {"label": "Passed", "value": run.passed_tasks, "color": "success"},
        {"label": "Failed", "value": run.failed_tasks, "color": "error"},
        {"label": "Success Rate", "value": f"{run.success_rate * 100:.1f}%", "color": "accent"},
    ], columns=4),
    title="Summary",
)
```
**Lines**: ~8

**Improvement**: 60% reduction, much more readable, reusable component

---

## 2. Domain Breakdown Section

### Before (Inline Template)
```html
<!-- Domain Breakdown -->
<div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4 mb-6">
    <h2 class="text-lg font-semibold mb-3">Results by Domain</h2>
    <div class="grid grid-cols-2 md:grid-cols-5 gap-3">
        {% for domain, stats in domain_stats.items() %}
        <div class="p-3 bg-gray-50 dark:bg-gray-700 rounded-lg">
            <div class="text-sm font-medium capitalize">{{ domain }}</div>
            <div class="flex items-center space-x-2 mt-1">
                <span class="text-green-600 dark:text-green-400">{{ stats.passed }}</span>
                <span class="text-gray-400">/</span>
                <span class="text-gray-600 dark:text-gray-300">{{ stats.total }}</span>
                <span class="text-xs text-gray-500">({{ "%.0f" | format(stats.passed / stats.total * 100 if stats.total > 0 else 0) }}%)</span>
            </div>
        </div>
        {% endfor %}
    </div>
</div>
```
**Lines**: ~18

### After (Component-Based)
```python
page.add_section(
    domain_stats_grid(domain_stats),
    title="Results by Domain",
)
```
**Lines**: ~3

**Improvement**: 83% reduction, encapsulated logic in component

---

## 3. Filter Section

### Before (Inline Template)
```html
<!-- Filters -->
<div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4 mb-6">
    <div class="flex flex-wrap gap-4">
        <div>
            <label class="block text-sm font-medium mb-1">Domain</label>
            <select x-model="filterDomain" class="px-3 py-2 border rounded-lg bg-white dark:bg-gray-700 dark:border-gray-600">
                <option value="">All Domains</option>
                {% for domain in domain_stats.keys() %}
                <option value="{{ domain }}">{{ domain | capitalize }}</option>
                {% endfor %}
            </select>
        </div>
        <div>
            <label class="block text-sm font-medium mb-1">Status</label>
            <select x-model="filterStatus" class="px-3 py-2 border rounded-lg bg-white dark:bg-gray-700 dark:border-gray-600">
                <option value="">All</option>
                <option value="passed">Passed</option>
                <option value="failed">Failed</option>
            </select>
        </div>
    </div>
</div>
```
**Lines**: ~21

### After (Component-Based)
```python
domain_options = [{"value": domain, "label": domain.capitalize()} for domain in domain_stats.keys()]
page.add_section(
    filter_bar(
        filters=[
            {"id": "domain", "label": "Domain", "options": domain_options},
            {"id": "status", "label": "Status", "options": [
                {"value": "passed", "label": "Passed"},
                {"value": "failed", "label": "Failed"},
            ]},
        ],
        alpine_data_name="viewer",
    ),
    title="Filters",
)
```
**Lines**: ~13

**Improvement**: 38% reduction, declarative configuration

---

## 4. Header Section

### Before (Inline Template)
```html
<!-- Header -->
<header class="bg-white dark:bg-gray-800 shadow-sm border-b border-gray-200 dark:border-gray-700">
    <div class="container mx-auto px-4 py-3 flex items-center justify-between">
        <div class="flex items-center space-x-3">
            <svg class="w-8 h-8 text-primary-600 dark:text-primary-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                      d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
            </svg>
            <div>
                <h1 class="text-xl font-semibold">{{ run.benchmark_name }}</h1>
                <p class="text-sm text-gray-500 dark:text-gray-400">Model: {{ run.model_id }}</p>
            </div>
        </div>
        <button @click="darkMode = !darkMode; localStorage.setItem('darkMode', darkMode)"
                class="p-2 rounded-lg bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600">
            <svg x-show="darkMode" class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                      d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
            </svg>
            <svg x-show="!darkMode" class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                      d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
            </svg>
        </button>
    </div>
</header>
```
**Lines**: ~27

### After (Component-Based)
```python
page.add_header(
    title=run.benchmark_name,
    subtitle=f"Model: {run.model_id}",
)
```
**Lines**: ~3

**Improvement**: 89% reduction, dark mode toggle included automatically

---

## 5. Alpine.js State Management

### Before (Inline Template)
```javascript
<script>
    function benchmarkViewer() {
        return {
            darkMode: localStorage.getItem('darkMode') === 'true',
            tasks: {{ tasks_data | tojson_safe }},
            selectedTask: null,
            currentStep: 0,
            isPlaying: false,
            playbackSpeed: 1,
            playbackInterval: null,
            filterDomain: '',
            filterStatus: '',

            init() {
                if (this.filteredTasks.length > 0) {
                    this.selectTask(this.filteredTasks[0]);
                }
            },

            get filteredTasks() {
                return this.tasks.filter(task => {
                    if (this.filterDomain && task.domain !== this.filterDomain) return false;
                    if (this.filterStatus === 'passed' && !task.success) return false;
                    if (this.filterStatus === 'failed' && task.success) return false;
                    return true;
                });
            },
            // ... more methods
        }
    }
</script>
```
**Lines**: ~70

### After (Component-Based)
```python
alpine_script = """
    document.addEventListener('alpine:init', () => {
        Alpine.data('viewer', () => ({
            tasks: TASKS_DATA_PLACEHOLDER,
            selectedTask: null,
            // ... state and methods
        }))
    });
""".replace("TASKS_DATA_PLACEHOLDER", tasks_json)
page.add_script(alpine_script)
```
**Lines**: ~70 (similar, but better organized)

**Improvement**: Same length but cleaner separation, easier to test

---

## Overall Statistics

### Template Lines Eliminated:
- **Header**: 27 lines → 3 lines (89% reduction)
- **Summary Metrics**: 20 lines → 8 lines (60% reduction)
- **Domain Breakdown**: 18 lines → 3 lines (83% reduction)
- **Filters**: 21 lines → 13 lines (38% reduction)
- **Total Template**: ~330 lines → ~180 lines of component code (45% reduction)

### Code Quality Improvements:
1. **Readability**: Component composition is self-documenting
2. **Maintainability**: Update component once, affects all viewers
3. **Testability**: Each component can be unit tested
4. **Reusability**: Components can be used in other viewers
5. **Type Safety**: Python type hints on all component functions
6. **Consistency**: All components use standard CSS classes and variables

### Development Benefits:
1. **Faster Development**: Compose new viewers from existing components
2. **Easier Debugging**: Component boundaries make issues easier to isolate
3. **Better Collaboration**: Components can be developed independently
4. **Design Consistency**: Shared CSS ensures uniform appearance
5. **Documentation**: Component docstrings explain usage

---

## Conclusion

The refactor demonstrates clear wins:
- **45% reduction** in template code through component reuse
- **Consistent styling** through shared CSS classes
- **Better architecture** with clear separation of concerns
- **Maintained functionality** - all features work correctly
- **Future-proof** - easy to add new features or create new viewers

The component-based approach makes the codebase more maintainable, testable, and extensible while reducing code duplication across viewers.
