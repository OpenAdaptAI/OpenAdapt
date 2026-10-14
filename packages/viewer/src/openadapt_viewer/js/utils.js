/**
 * OpenAdapt Viewer - Common Utilities
 *
 * Provides utility functions for formatting, data loading, and event handling.
 * Shared across all OpenAdapt viewers.
 *
 * @module utils
 */

/* ==========================================================================
   Formatting Utilities
   ========================================================================== */

/**
 * Format duration in seconds to human-readable string
 *
 * @param {number} seconds - Duration in seconds
 * @param {boolean} compact - Use compact format (default: false)
 * @returns {string} Formatted duration
 *
 * @example
 * formatDuration(65)       // "1m 5s"
 * formatDuration(65, true) // "1:05"
 * formatDuration(3665)     // "1h 1m 5s"
 * formatDuration(45.7)     // "45.7s"
 */
export function formatDuration(seconds, compact = false) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) {
        return 'N/A';
    }

    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;

    if (compact) {
        // Compact format: "1:23:45" or "12:34"
        if (hours > 0) {
            return `${hours}:${String(minutes).padStart(2, '0')}:${String(Math.floor(secs)).padStart(2, '0')}`;
        }
        return `${minutes}:${String(Math.floor(secs)).padStart(2, '0')}`;
    }

    // Human-readable format: "1h 23m 45s"
    const parts = [];
    if (hours > 0) parts.push(`${hours}h`);
    if (minutes > 0) parts.push(`${minutes}m`);

    if (secs > 0 || parts.length === 0) {
        // Show decimal for sub-minute durations
        const secStr = seconds < 60 ? secs.toFixed(1) : Math.floor(secs);
        parts.push(`${secStr}s`);
    }

    return parts.join(' ');
}

/**
 * Format timestamp to human-readable date/time
 *
 * @param {number|string|Date} timestamp - Unix timestamp, ISO string, or Date object
 * @param {Object} options - Formatting options
 * @returns {string} Formatted timestamp
 *
 * @example
 * formatTimestamp(1704067200000)
 * // "Jan 1, 2024, 12:00 AM"
 *
 * formatTimestamp("2024-01-01T00:00:00Z", { dateStyle: 'full' })
 * // "Monday, January 1, 2024"
 */
export function formatTimestamp(timestamp, options = {}) {
    if (!timestamp) return 'N/A';

    try {
        const date = new Date(timestamp);
        if (isNaN(date.getTime())) return 'Invalid Date';

        const defaultOptions = {
            dateStyle: 'medium',
            timeStyle: 'short',
            ...options
        };

        return new Intl.DateTimeFormat('en-US', defaultOptions).format(date);
    } catch (error) {
        return 'Invalid Date';
    }
}

/**
 * Format relative time (e.g., "2 hours ago")
 *
 * @param {number|string|Date} timestamp - Timestamp to format
 * @returns {string} Relative time string
 *
 * @example
 * formatRelativeTime(Date.now() - 3600000) // "1 hour ago"
 * formatRelativeTime(Date.now() + 86400000) // "in 1 day"
 */
export function formatRelativeTime(timestamp) {
    if (!timestamp) return 'N/A';

    try {
        const date = new Date(timestamp);
        const now = new Date();
        const diffMs = date - now;
        const diffSec = Math.floor(diffMs / 1000);
        const diffMin = Math.floor(diffSec / 60);
        const diffHour = Math.floor(diffMin / 60);
        const diffDay = Math.floor(diffHour / 24);

        if (Math.abs(diffDay) > 0) {
            return new Intl.RelativeTimeFormat('en', { numeric: 'auto' }).format(diffDay, 'day');
        }
        if (Math.abs(diffHour) > 0) {
            return new Intl.RelativeTimeFormat('en', { numeric: 'auto' }).format(diffHour, 'hour');
        }
        if (Math.abs(diffMin) > 0) {
            return new Intl.RelativeTimeFormat('en', { numeric: 'auto' }).format(diffMin, 'minute');
        }
        return new Intl.RelativeTimeFormat('en', { numeric: 'auto' }).format(diffSec, 'second');
    } catch (error) {
        return 'N/A';
    }
}

/**
 * Format number with thousands separators
 *
 * @param {number} num - Number to format
 * @param {number} decimals - Number of decimal places (default: 0)
 * @returns {string} Formatted number
 *
 * @example
 * formatNumber(1234567)      // "1,234,567"
 * formatNumber(1234.5678, 2) // "1,234.57"
 */
export function formatNumber(num, decimals = 0) {
    if (num === null || num === undefined || isNaN(num)) {
        return 'N/A';
    }

    return new Intl.NumberFormat('en-US', {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals
    }).format(num);
}

/**
 * Format percentage
 *
 * @param {number} value - Value (0-1 or 0-100)
 * @param {boolean} isDecimal - Whether value is 0-1 (default: true)
 * @param {number} decimals - Decimal places (default: 1)
 * @returns {string} Formatted percentage
 *
 * @example
 * formatPercentage(0.856)        // "85.6%"
 * formatPercentage(85.6, false)  // "85.6%"
 * formatPercentage(0.1234, true, 2) // "12.34%"
 */
export function formatPercentage(value, isDecimal = true, decimals = 1) {
    if (value === null || value === undefined || isNaN(value)) {
        return 'N/A';
    }

    const percent = isDecimal ? value * 100 : value;
    return `${percent.toFixed(decimals)}%`;
}

/**
 * Format file size in bytes to human-readable string
 *
 * @param {number} bytes - File size in bytes
 * @param {number} decimals - Decimal places (default: 2)
 * @returns {string} Formatted size
 *
 * @example
 * formatFileSize(1024)        // "1.00 KB"
 * formatFileSize(1536000)     // "1.46 MB"
 * formatFileSize(1073741824)  // "1.00 GB"
 */
export function formatFileSize(bytes, decimals = 2) {
    if (bytes === 0) return '0 Bytes';
    if (!bytes) return 'N/A';

    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));

    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(decimals))} ${sizes[i]}`;
}

/* ==========================================================================
   Data Loading Utilities
   ========================================================================== */

/**
 * Load JSON data from a URL or file path
 *
 * @param {string} path - URL or file path
 * @returns {Promise<Object>} Parsed JSON data
 *
 * @example
 * const data = await loadJSON('episodes.json');
 * console.log(data.episodes);
 */
export async function loadJSON(path) {
    try {
        const response = await fetch(path);
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return await response.json();
    } catch (error) {
        console.error('Error loading JSON:', error);
        throw error;
    }
}

/**
 * Load JSON with error handling and fallback
 *
 * @param {string} path - URL or file path
 * @param {Object} fallback - Fallback data if load fails
 * @returns {Promise<Object>} Parsed JSON or fallback
 */
export async function loadJSONSafe(path, fallback = null) {
    try {
        return await loadJSON(path);
    } catch (error) {
        console.warn(`Failed to load ${path}, using fallback:`, error);
        return fallback;
    }
}

/**
 * Load multiple JSON files in parallel
 *
 * @param {string[]} paths - Array of URLs or file paths
 * @returns {Promise<Object[]>} Array of parsed JSON data
 *
 * @example
 * const [episodes, recordings] = await loadMultipleJSON([
 *   'episodes.json',
 *   'recordings.json'
 * ]);
 */
export async function loadMultipleJSON(paths) {
    return Promise.all(paths.map(path => loadJSON(path)));
}

/**
 * Load image with preloading
 *
 * @param {string} src - Image URL
 * @returns {Promise<HTMLImageElement>} Loaded image element
 *
 * @example
 * const img = await loadImage('screenshot.png');
 * document.body.appendChild(img);
 */
export function loadImage(src) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => resolve(img);
        img.onerror = reject;
        img.src = src;
    });
}

/* ==========================================================================
   Event Handling Utilities
   ========================================================================== */

/**
 * Debounce function calls
 *
 * @param {Function} func - Function to debounce
 * @param {number} wait - Wait time in milliseconds
 * @returns {Function} Debounced function
 *
 * @example
 * const handleSearch = debounce((query) => {
 *   console.log('Searching:', query);
 * }, 300);
 *
 * input.addEventListener('input', (e) => handleSearch(e.target.value));
 */
export function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

/**
 * Throttle function calls
 *
 * @param {Function} func - Function to throttle
 * @param {number} limit - Time limit in milliseconds
 * @returns {Function} Throttled function
 *
 * @example
 * const handleScroll = throttle(() => {
 *   console.log('Scrolled');
 * }, 100);
 *
 * window.addEventListener('scroll', handleScroll);
 */
export function throttle(func, limit) {
    let inThrottle;
    return function executedFunction(...args) {
        if (!inThrottle) {
            func(...args);
            inThrottle = true;
            setTimeout(() => inThrottle = false, limit);
        }
    };
}

/**
 * Wait for DOM element to exist
 *
 * @param {string} selector - CSS selector
 * @param {number} timeout - Timeout in milliseconds (default: 5000)
 * @returns {Promise<Element>} Element when found
 *
 * @example
 * const element = await waitForElement('#episode-list');
 * element.classList.add('visible');
 */
export function waitForElement(selector, timeout = 5000) {
    return new Promise((resolve, reject) => {
        const element = document.querySelector(selector);
        if (element) {
            return resolve(element);
        }

        const observer = new MutationObserver(() => {
            const element = document.querySelector(selector);
            if (element) {
                observer.disconnect();
                resolve(element);
            }
        });

        observer.observe(document.body, {
            childList: true,
            subtree: true
        });

        setTimeout(() => {
            observer.disconnect();
            reject(new Error(`Element ${selector} not found within ${timeout}ms`));
        }, timeout);
    });
}

/* ==========================================================================
   DOM Utilities
   ========================================================================== */

/**
 * Escape HTML to prevent XSS
 *
 * @param {string} text - Text to escape
 * @returns {string} Escaped HTML
 *
 * @example
 * escapeHTML('<script>alert("XSS")</script>')
 * // "&lt;script&gt;alert(&quot;XSS&quot;)&lt;/script&gt;"
 */
export function escapeHTML(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Create element with attributes and children
 *
 * @param {string} tag - Element tag name
 * @param {Object} attrs - Element attributes
 * @param {Array|string} children - Child elements or text
 * @returns {HTMLElement} Created element
 *
 * @example
 * const div = createElement('div',
 *   { class: 'container', id: 'main' },
 *   [
 *     createElement('h1', {}, 'Title'),
 *     createElement('p', {}, 'Content')
 *   ]
 * );
 */
export function createElement(tag, attrs = {}, children = []) {
    const element = document.createElement(tag);

    // Set attributes
    Object.entries(attrs).forEach(([key, value]) => {
        if (key === 'class') {
            element.className = value;
        } else if (key === 'style' && typeof value === 'object') {
            Object.assign(element.style, value);
        } else {
            element.setAttribute(key, value);
        }
    });

    // Add children
    if (typeof children === 'string') {
        element.textContent = children;
    } else if (Array.isArray(children)) {
        children.forEach(child => {
            if (typeof child === 'string') {
                element.appendChild(document.createTextNode(child));
            } else if (child instanceof HTMLElement) {
                element.appendChild(child);
            }
        });
    }

    return element;
}

/* ==========================================================================
   Utility Helpers
   ========================================================================== */

/**
 * Deep clone an object
 *
 * @param {Object} obj - Object to clone
 * @returns {Object} Cloned object
 */
export function deepClone(obj) {
    return JSON.parse(JSON.stringify(obj));
}

/**
 * Get nested property safely
 *
 * @param {Object} obj - Object to query
 * @param {string} path - Property path (e.g., "user.profile.name")
 * @param {*} defaultValue - Default value if not found
 * @returns {*} Property value or default
 *
 * @example
 * const user = { profile: { name: "Alice" } };
 * getNestedProperty(user, "profile.name")        // "Alice"
 * getNestedProperty(user, "profile.age", 25)     // 25
 */
export function getNestedProperty(obj, path, defaultValue = undefined) {
    const keys = path.split('.');
    let result = obj;

    for (const key of keys) {
        if (result && typeof result === 'object' && key in result) {
            result = result[key];
        } else {
            return defaultValue;
        }
    }

    return result;
}

/**
 * Group array of objects by a field
 *
 * @param {Array<Object>} items - Items to group
 * @param {string} field - Field to group by
 * @returns {Object} Grouped object
 *
 * @example
 * const episodes = [
 *   { name: "E1", domain: "notepad" },
 *   { name: "E2", domain: "paint" },
 *   { name: "E3", domain: "notepad" }
 * ];
 * groupBy(episodes, 'domain')
 * // { notepad: [E1, E3], paint: [E2] }
 */
export function groupBy(items, field) {
    return items.reduce((groups, item) => {
        const key = item[field];
        if (!groups[key]) {
            groups[key] = [];
        }
        groups[key].push(item);
        return groups;
    }, {});
}

/**
 * Sort array of objects by a field
 *
 * @param {Array<Object>} items - Items to sort
 * @param {string} field - Field to sort by
 * @param {boolean} ascending - Sort ascending (default: true)
 * @returns {Array<Object>} Sorted array (new array)
 *
 * @example
 * sortBy(episodes, 'duration', false) // Sort by duration descending
 */
export function sortBy(items, field, ascending = true) {
    return [...items].sort((a, b) => {
        const aVal = a[field];
        const bVal = b[field];

        if (aVal < bVal) return ascending ? -1 : 1;
        if (aVal > bVal) return ascending ? 1 : -1;
        return 0;
    });
}

// For non-module usage (standalone scripts)
if (typeof window !== 'undefined') {
    window.formatDuration = formatDuration;
    window.formatTimestamp = formatTimestamp;
    window.formatRelativeTime = formatRelativeTime;
    window.formatNumber = formatNumber;
    window.formatPercentage = formatPercentage;
    window.formatFileSize = formatFileSize;
    window.loadJSON = loadJSON;
    window.loadJSONSafe = loadJSONSafe;
    window.loadMultipleJSON = loadMultipleJSON;
    window.loadImage = loadImage;
    window.debounce = debounce;
    window.throttle = throttle;
    window.waitForElement = waitForElement;
    window.escapeHTML = escapeHTML;
    window.createElement = createElement;
    window.deepClone = deepClone;
    window.getNestedProperty = getNestedProperty;
    window.groupBy = groupBy;
    window.sortBy = sortBy;
}
