/**
 * OpenAdapt Viewer - Filter Utilities
 *
 * Provides flexible multi-field filtering with combining logic.
 * Supports exact matching, range filtering, and multiple criteria.
 *
 * @module filters
 */

/**
 * Filter items based on multiple criteria
 *
 * Supports:
 * - Single field filtering: { recording_id: "rec_001" }
 * - Multi-field filtering: { domain: "notepad", status: "passed" }
 * - Array field matching: { tags: ["automation", "demo"] }
 * - Range filtering: { duration: { min: 10, max: 60 } }
 * - Custom predicates: { steps: (steps) => steps.length > 5 }
 *
 * @param {Array<Object>} items - Items to filter
 * @param {Object} filters - Filter criteria object
 * @param {string} combineMode - How to combine filters: 'AND' or 'OR' (default: 'AND')
 * @returns {Array<Object>} Filtered items
 *
 * @example
 * const episodes = [
 *   { name: "Episode 1", domain: "notepad", duration: 30, tags: ["demo"] },
 *   { name: "Episode 2", domain: "paint", duration: 60, tags: ["automation"] }
 * ];
 *
 * // Single filter
 * filterItems(episodes, { domain: "notepad" })
 * // Returns: [{ name: "Episode 1", ... }]
 *
 * // Multi-field AND
 * filterItems(episodes, { domain: "notepad", duration: { min: 20 } })
 * // Returns: [{ name: "Episode 1", ... }]
 *
 * // Multi-field OR
 * filterItems(episodes, { domain: "notepad", tags: ["automation"] }, 'OR')
 * // Returns: both episodes
 */
export function filterItems(items, filters, combineMode = 'AND') {
    if (!filters || Object.keys(filters).length === 0) {
        return items;
    }

    return items.filter(item => {
        const results = Object.entries(filters).map(([field, criteria]) => {
            return matchesCriteria(item, field, criteria);
        });

        return combineMode === 'OR'
            ? results.some(r => r)
            : results.every(r => r);
    });
}

/**
 * Check if an item matches a specific criteria
 *
 * @param {Object} item - Item to check
 * @param {string} field - Field name
 * @param {*} criteria - Criteria to match (value, object, or function)
 * @returns {boolean} True if matches
 */
function matchesCriteria(item, field, criteria) {
    const value = item[field];

    // Function predicate
    if (typeof criteria === 'function') {
        return criteria(value, item);
    }

    // Range filter (e.g., { min: 10, max: 60 })
    if (typeof criteria === 'object' && criteria !== null && !Array.isArray(criteria)) {
        if ('min' in criteria || 'max' in criteria) {
            const numValue = Number(value);
            if (isNaN(numValue)) return false;

            if ('min' in criteria && numValue < criteria.min) return false;
            if ('max' in criteria && numValue > criteria.max) return false;
            return true;
        }

        // Nested object matching
        if (typeof value === 'object' && value !== null) {
            return Object.entries(criteria).every(([k, v]) => value[k] === v);
        }
    }

    // Array field: check if item's array contains any of the criteria values
    if (Array.isArray(value)) {
        if (Array.isArray(criteria)) {
            return criteria.some(c => value.includes(c));
        }
        return value.includes(criteria);
    }

    // Array criteria: check if value is in criteria array
    if (Array.isArray(criteria)) {
        return criteria.includes(value);
    }

    // Exact match
    return value === criteria;
}

/**
 * Create a filter function from criteria
 * Useful for passing to Array.filter()
 *
 * @param {Object} filters - Filter criteria
 * @param {string} combineMode - 'AND' or 'OR'
 * @returns {Function} Filter function
 *
 * @example
 * const episodes = [...];
 * const filterFn = createFilter({ domain: "notepad" });
 * const filtered = episodes.filter(filterFn);
 */
export function createFilter(filters, combineMode = 'AND') {
    return (item) => {
        if (!filters || Object.keys(filters).length === 0) {
            return true;
        }

        const results = Object.entries(filters).map(([field, criteria]) => {
            return matchesCriteria(item, field, criteria);
        });

        return combineMode === 'OR'
            ? results.some(r => r)
            : results.every(r => r);
    };
}

/**
 * Filter by multiple values in a single field
 *
 * @param {Array<Object>} items - Items to filter
 * @param {string} field - Field name
 * @param {Array} values - Allowed values
 * @returns {Array<Object>} Filtered items
 *
 * @example
 * filterByValues(episodes, 'domain', ['notepad', 'paint'])
 * // Returns episodes with domain "notepad" or "paint"
 */
export function filterByValues(items, field, values) {
    if (!values || values.length === 0) {
        return items;
    }

    return items.filter(item => values.includes(item[field]));
}

/**
 * Filter by numeric range
 *
 * @param {Array<Object>} items - Items to filter
 * @param {string} field - Field name
 * @param {number} min - Minimum value (inclusive)
 * @param {number} max - Maximum value (inclusive)
 * @returns {Array<Object>} Filtered items
 *
 * @example
 * filterByRange(episodes, 'duration', 10, 60)
 * // Returns episodes with duration between 10 and 60 seconds
 */
export function filterByRange(items, field, min, max) {
    return items.filter(item => {
        const value = Number(item[field]);
        if (isNaN(value)) return false;
        return value >= min && value <= max;
    });
}

/**
 * Filter by date range
 *
 * @param {Array<Object>} items - Items to filter
 * @param {string} field - Field name (containing ISO date string or timestamp)
 * @param {Date|string|number} startDate - Start date
 * @param {Date|string|number} endDate - End date
 * @returns {Array<Object>} Filtered items
 *
 * @example
 * filterByDateRange(episodes, 'created_at', '2024-01-01', '2024-12-31')
 */
export function filterByDateRange(items, field, startDate, endDate) {
    const start = new Date(startDate).getTime();
    const end = new Date(endDate).getTime();

    return items.filter(item => {
        const value = new Date(item[field]).getTime();
        if (isNaN(value)) return false;
        return value >= start && value <= end;
    });
}

/**
 * Combine multiple filters with different logic
 *
 * @param {Array<Object>} items - Items to filter
 * @param {Array<Object>} filterGroups - Array of {filters, mode} objects
 * @returns {Array<Object>} Filtered items
 *
 * @example
 * combineFilters(episodes, [
 *   { filters: { domain: "notepad" }, mode: 'AND' },
 *   { filters: { status: "passed", tags: ["demo"] }, mode: 'OR' }
 * ])
 */
export function combineFilters(items, filterGroups) {
    let result = items;

    for (const group of filterGroups) {
        result = filterItems(result, group.filters, group.mode || 'AND');
    }

    return result;
}

/**
 * Get unique values for a field (useful for populating filter dropdowns)
 *
 * @param {Array<Object>} items - Items to extract values from
 * @param {string} field - Field name
 * @returns {Array} Unique values sorted alphabetically
 *
 * @example
 * getUniqueValues(episodes, 'domain')
 * // Returns: ["notepad", "paint", "clock"]
 */
export function getUniqueValues(items, field) {
    const values = new Set();

    items.forEach(item => {
        const value = item[field];
        if (Array.isArray(value)) {
            value.forEach(v => values.add(v));
        } else if (value !== null && value !== undefined) {
            values.add(value);
        }
    });

    return Array.from(values).sort();
}

/**
 * Get filter statistics (counts per value)
 *
 * @param {Array<Object>} items - Items to analyze
 * @param {string} field - Field name
 * @returns {Object} Map of value -> count
 *
 * @example
 * getFilterStats(episodes, 'domain')
 * // Returns: { notepad: 15, paint: 12, clock: 8 }
 */
export function getFilterStats(items, field) {
    const stats = {};

    items.forEach(item => {
        const value = item[field];
        if (Array.isArray(value)) {
            value.forEach(v => {
                stats[v] = (stats[v] || 0) + 1;
            });
        } else if (value !== null && value !== undefined) {
            stats[value] = (stats[value] || 0) + 1;
        }
    });

    return stats;
}

/**
 * Create a multi-field filter UI state manager
 *
 * @returns {Object} Filter state manager
 *
 * @example
 * const filterManager = createFilterManager();
 * filterManager.addFilter('domain', 'notepad');
 * filterManager.addFilter('status', 'passed');
 * const filtered = filterManager.apply(episodes);
 * filterManager.clearAll();
 */
export function createFilterManager() {
    const state = {
        filters: {},
        combineMode: 'AND'
    };

    return {
        /**
         * Add or update a filter
         */
        addFilter(field, criteria) {
            state.filters[field] = criteria;
            return this;
        },

        /**
         * Remove a filter
         */
        removeFilter(field) {
            delete state.filters[field];
            return this;
        },

        /**
         * Clear all filters
         */
        clearAll() {
            state.filters = {};
            return this;
        },

        /**
         * Set combine mode
         */
        setCombineMode(mode) {
            state.combineMode = mode;
            return this;
        },

        /**
         * Get current filters
         */
        getFilters() {
            return { ...state.filters };
        },

        /**
         * Apply filters to items
         */
        apply(items) {
            return filterItems(items, state.filters, state.combineMode);
        },

        /**
         * Check if any filters are active
         */
        hasActiveFilters() {
            return Object.keys(state.filters).length > 0;
        },

        /**
         * Get count of active filters
         */
        getFilterCount() {
            return Object.keys(state.filters).length;
        }
    };
}

// For non-module usage (standalone scripts)
if (typeof window !== 'undefined') {
    window.filterItems = filterItems;
    window.createFilter = createFilter;
    window.filterByValues = filterByValues;
    window.filterByRange = filterByRange;
    window.filterByDateRange = filterByDateRange;
    window.combineFilters = combineFilters;
    window.getUniqueValues = getUniqueValues;
    window.getFilterStats = getFilterStats;
    window.createFilterManager = createFilterManager;
}
