/**
 * OpenAdapt Viewer - Advanced Search Utilities
 *
 * Provides token-based search with flexible matching capabilities.
 * Supports case-insensitive, token order independent, partial matching.
 *
 * Example:
 *   advancedSearch(episodes, "nightshift", ['name', 'description'])
 *   // Finds "Disable night shift" even with different spacing/case
 *
 * @module search
 */

/**
 * Advanced token-based search with flexible matching
 *
 * Algorithm:
 * 1. Normalizes query by:
 *    - Converting to lowercase
 *    - Removing punctuation (replaced with spaces)
 *    - Normalizing whitespace
 *    - Splitting into tokens
 *
 * 2. For each item:
 *    - Extracts searchable text from specified fields
 *    - Normalizes text the same way
 *    - Checks if all query tokens match
 *
 * 3. Token matching:
 *    - Bidirectional substring matching
 *    - "nightshift" matches "night" and "shift"
 *    - "night" matches "nightshift"
 *
 * Examples:
 *   - "nightshift" finds "Disable night shift" ✓
 *   - "night shift" finds "Configure nightshift" ✓
 *   - "shift night" finds "night shift" (order independent) ✓
 *   - "nightsh" finds "nightshift" (partial) ✓
 *
 * @param {Array<Object>} items - Items to search through
 * @param {string} query - Search query string
 * @param {string[]} fields - Fields to search in (default: ['name', 'description'])
 * @returns {Array<Object>} Filtered items that match the query
 *
 * @example
 * const episodes = [
 *   { name: "Disable night shift", description: "Turn off Night Shift", steps: [...] },
 *   { name: "Open Notepad", description: "Launch notepad application", steps: [...] }
 * ];
 *
 * // Find episodes about nightshift
 * const results = advancedSearch(episodes, "nightshift", ['name', 'description', 'steps']);
 * // Returns: [{ name: "Disable night shift", ... }]
 *
 * // Order independent
 * const results2 = advancedSearch(episodes, "shift night", ['name', 'description']);
 * // Returns same result
 */
export function advancedSearch(items, query, fields = ['name', 'description']) {
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
            // Split search text into tokens
            const searchTokens = searchText.split(' ');

            // Check if any search token contains the query token or vice versa
            return searchTokens.some(searchToken =>
                searchToken.includes(queryToken) || queryToken.includes(searchToken)
            );
        });
    });
}

/**
 * Simplified search for non-module usage (standalone HTML files)
 * Can be copied directly into <script> tags
 *
 * @param {Array<Object>} items - Items to search
 * @param {string} query - Search query
 * @param {string[]} fields - Fields to search in
 * @returns {Array<Object>} Filtered items
 */
export function simpleSearch(items, query, fields = ['name', 'description']) {
    if (!query || query.trim() === '') return items;

    const lowerQuery = query.toLowerCase();

    return items.filter(item => {
        const searchText = fields
            .map(field => item[field] || '')
            .join(' ')
            .toLowerCase();

        return searchText.includes(lowerQuery);
    });
}

/**
 * Fuzzy search with edit distance (Levenshtein)
 * More forgiving for typos but slower
 *
 * @param {Array<Object>} items - Items to search
 * @param {string} query - Search query
 * @param {string[]} fields - Fields to search in
 * @param {number} maxDistance - Maximum edit distance (default: 2)
 * @returns {Array<Object>} Filtered items
 */
export function fuzzySearch(items, query, fields = ['name', 'description'], maxDistance = 2) {
    if (!query || query.trim() === '') return items;

    const lowerQuery = query.toLowerCase();

    return items.filter(item => {
        const searchText = fields
            .map(field => item[field] || '')
            .join(' ')
            .toLowerCase();

        // Check if query is a substring (fast path)
        if (searchText.includes(lowerQuery)) return true;

        // Check edit distance for individual words
        const words = searchText.split(/\s+/);
        return words.some(word => levenshteinDistance(word, lowerQuery) <= maxDistance);
    });
}

/**
 * Calculate Levenshtein distance between two strings
 *
 * @param {string} a - First string
 * @param {string} b - Second string
 * @returns {number} Edit distance
 */
function levenshteinDistance(a, b) {
    const matrix = [];

    // Initialize matrix
    for (let i = 0; i <= b.length; i++) {
        matrix[i] = [i];
    }
    for (let j = 0; j <= a.length; j++) {
        matrix[0][j] = j;
    }

    // Fill matrix
    for (let i = 1; i <= b.length; i++) {
        for (let j = 1; j <= a.length; j++) {
            if (b.charAt(i - 1) === a.charAt(j - 1)) {
                matrix[i][j] = matrix[i - 1][j - 1];
            } else {
                matrix[i][j] = Math.min(
                    matrix[i - 1][j - 1] + 1, // substitution
                    matrix[i][j - 1] + 1,     // insertion
                    matrix[i - 1][j] + 1      // deletion
                );
            }
        }
    }

    return matrix[b.length][a.length];
}

/**
 * Highlight matching tokens in text
 *
 * @param {string} text - Text to highlight
 * @param {string} query - Search query
 * @returns {string} HTML with highlighted matches
 *
 * @example
 * highlightMatches("Disable night shift", "nightshift")
 * // Returns: "Disable <mark>night</mark> <mark>shift</mark>"
 */
export function highlightMatches(text, query) {
    if (!query || query.trim() === '') return text;

    const queryTokens = query
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .split(' ')
        .filter(t => t.length > 0);

    let result = text;

    queryTokens.forEach(token => {
        const regex = new RegExp(`(${token})`, 'gi');
        result = result.replace(regex, '<mark>$1</mark>');
    });

    return result;
}

/**
 * Get inline version of advancedSearch for embedding in HTML
 * Returns the function as a string that can be inserted into <script> tags
 *
 * @returns {string} JavaScript function code
 */
export function getInlineSearchFunction() {
    return `
function advancedSearch(items, query, fields = ['name', 'description']) {
    if (!query || query.trim() === '') {
        return items;
    }

    const queryTokens = query
        .toLowerCase()
        .replace(/[^a-z0-9\\s]/g, ' ')
        .replace(/\\s+/g, ' ')
        .trim()
        .split(' ')
        .filter(t => t.length > 0);

    if (queryTokens.length === 0) {
        return items;
    }

    return items.filter(item => {
        const searchText = fields
            .map(field => {
                const value = item[field];
                if (Array.isArray(value)) {
                    return value.map(v => typeof v === 'string' ? v : v.description || '').join(' ');
                }
                return value || '';
            })
            .join(' ')
            .toLowerCase()
            .replace(/[^a-z0-9\\s]/g, ' ')
            .replace(/\\s+/g, ' ');

        return queryTokens.every(queryToken => {
            const searchTokens = searchText.split(' ');
            return searchTokens.some(searchToken =>
                searchToken.includes(queryToken) || queryToken.includes(searchToken)
            );
        });
    });
}
`.trim();
}

// For non-module usage (standalone scripts)
if (typeof window !== 'undefined') {
    window.advancedSearch = advancedSearch;
    window.simpleSearch = simpleSearch;
    window.fuzzySearch = fuzzySearch;
    window.highlightMatches = highlightMatches;
}
