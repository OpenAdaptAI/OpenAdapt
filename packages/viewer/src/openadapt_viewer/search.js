/**
 * Advanced Search Module for OpenAdapt Viewers
 *
 * Provides token-based, case-insensitive search with fuzzy matching support.
 * Inspired by full-text search principles but implemented for client-side JavaScript.
 *
 * Key Features:
 * - Case-insensitive matching
 * - Token-based (search terms can be in any order)
 * - Partial word matching ("nightsh" matches "nightshift")
 * - Whitespace normalization ("nightshift" matches "night shift")
 * - Optional fuzzy matching with Levenshtein distance
 * - Multi-field search support
 * - Relevance scoring and ranking
 *
 * @example
 * // Basic usage
 * const results = searchItems(items, "night shift", {
 *     fields: ['name', 'description', 'steps']
 * });
 *
 * // With fuzzy matching and ranking
 * const results = searchItems(items, "nitshift", {
 *     fields: ['name', 'description'],
 *     fuzzyThreshold: 2,
 *     rankResults: true
 * });
 */

/**
 * Search items using advanced token-based matching
 *
 * @param {Array} items - Array of items to search
 * @param {string} query - Search query
 * @param {Object} options - Search options
 * @param {Array<string>} options.fields - Fields to search (default: ['name', 'description'])
 * @param {number} options.fuzzyThreshold - Levenshtein distance threshold (default: 0, disabled)
 * @param {boolean} options.rankResults - Sort by relevance score (default: false)
 * @param {number} options.minScore - Minimum score threshold (default: 1)
 * @returns {Array} Filtered (and optionally ranked) items
 */
export function searchItems(items, query, options = {}) {
    const {
        fields = ['name', 'description'],
        fuzzyThreshold = 0,
        rankResults = false,
        minScore = 1
    } = options;

    if (!query || query.trim() === '') {
        return items;
    }

    const queryTokens = tokenize(query);
    if (queryTokens.length === 0) {
        return items;
    }

    const results = [];

    for (const item of items) {
        const score = scoreItem(item, queryTokens, fields, fuzzyThreshold);
        if (score >= minScore) {
            results.push({ item, score });
        }
    }

    if (rankResults) {
        results.sort((a, b) => b.score - a.score);
    }

    return results.map(r => r.item);
}

/**
 * Tokenize and normalize text for searching
 * Removes punctuation, converts to lowercase, splits on whitespace
 *
 * @param {string} text - Text to tokenize
 * @returns {Array<string>} Normalized tokens
 */
function tokenize(text) {
    if (!text) return [];

    return text
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, ' ')  // Replace punctuation with space
        .replace(/\s+/g, ' ')           // Normalize whitespace
        .trim()
        .split(' ')
        .filter(t => t.length > 0);
}

/**
 * Calculate relevance score for an item against query tokens
 *
 * Scoring:
 * - Exact token match: 2 points
 * - Fuzzy match (within threshold): 1 point
 * - All query tokens must have at least one match
 *
 * @param {Object} item - Item to score
 * @param {Array<string>} queryTokens - Normalized query tokens
 * @param {Array<string>} fields - Fields to search in
 * @param {number} fuzzyThreshold - Max Levenshtein distance for fuzzy match
 * @returns {number} Relevance score (0 if no match)
 */
function scoreItem(item, queryTokens, fields, fuzzyThreshold) {
    // Build searchable text from all fields
    const searchText = fields
        .map(field => {
            const value = getNestedValue(item, field);
            if (Array.isArray(value)) {
                // Handle array fields (e.g., steps)
                return value.join(' ');
            }
            return value || '';
        })
        .join(' ');

    const searchTokens = tokenize(searchText);

    let totalScore = 0;
    let matchedTokens = 0;

    // Score each query token
    for (const queryToken of queryTokens) {
        let bestTokenScore = 0;

        // Check for exact or partial matches
        for (const searchToken of searchTokens) {
            if (searchToken === queryToken) {
                // Exact token match
                bestTokenScore = Math.max(bestTokenScore, 3);
            } else if (searchToken.includes(queryToken)) {
                // Query token is substring of search token (e.g., "night" in "nightshift")
                bestTokenScore = Math.max(bestTokenScore, 2);
            } else if (queryToken.includes(searchToken)) {
                // Search token is substring of query token
                bestTokenScore = Math.max(bestTokenScore, 2);
            } else if (fuzzyThreshold > 0) {
                // Check fuzzy match
                const distance = levenshteinDistance(searchToken, queryToken);
                if (distance <= fuzzyThreshold) {
                    bestTokenScore = Math.max(bestTokenScore, 1);
                }
            }
        }

        if (bestTokenScore > 0) {
            matchedTokens++;
            totalScore += bestTokenScore;
        }
    }

    // All query tokens must have at least one match
    return matchedTokens === queryTokens.length ? totalScore : 0;
}

/**
 * Get nested object value by dot notation path
 *
 * @param {Object} obj - Object to access
 * @param {string} path - Dot notation path (e.g., 'user.name')
 * @returns {*} Value at path or undefined
 */
function getNestedValue(obj, path) {
    return path.split('.').reduce((current, key) => current?.[key], obj);
}

/**
 * Calculate Levenshtein distance between two strings
 * (minimum number of single-character edits to transform one string into another)
 *
 * @param {string} a - First string
 * @param {string} b - Second string
 * @returns {number} Edit distance
 */
function levenshteinDistance(a, b) {
    if (a.length === 0) return b.length;
    if (b.length === 0) return a.length;

    const matrix = [];

    // Initialize first column
    for (let i = 0; i <= b.length; i++) {
        matrix[i] = [i];
    }

    // Initialize first row
    for (let j = 0; j <= a.length; j++) {
        matrix[0][j] = j;
    }

    // Fill in the rest of the matrix
    for (let i = 1; i <= b.length; i++) {
        for (let j = 1; j <= a.length; j++) {
            if (b.charAt(i - 1) === a.charAt(j - 1)) {
                matrix[i][j] = matrix[i - 1][j - 1];
            } else {
                matrix[i][j] = Math.min(
                    matrix[i - 1][j - 1] + 1,  // substitution
                    matrix[i][j - 1] + 1,      // insertion
                    matrix[i - 1][j] + 1       // deletion
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
 * @returns {string} HTML with <mark> tags around matches
 */
export function highlightMatches(text, query) {
    if (!query || !text) return text;

    const queryTokens = tokenize(query);
    let result = text;

    // Sort tokens by length (longest first) to avoid partial replacements
    queryTokens.sort((a, b) => b.length - a.length);

    for (const token of queryTokens) {
        // Create case-insensitive regex
        const regex = new RegExp(`(${escapeRegex(token)})`, 'gi');
        result = result.replace(regex, '<mark>$1</mark>');
    }

    return result;
}

/**
 * Escape special regex characters
 *
 * @param {string} str - String to escape
 * @returns {string} Escaped string
 */
function escapeRegex(str) {
    return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Simple search for standalone HTML files (inline version)
 * Paste this into <script> tags for self-contained viewers
 *
 * @param {Array} items - Items to search
 * @param {string} query - Search query
 * @param {Array<string>} fields - Fields to search
 * @returns {Array} Filtered items
 */
export function simpleSearch(items, query, fields = ['name', 'description']) {
    if (!query || query.trim() === '') {
        return items;
    }

    // Tokenize query
    const queryTokens = query
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .split(' ')
        .filter(t => t.length > 0);

    if (queryTokens.length === 0) {
        return items;
    }

    return items.filter(item => {
        // Build searchable text
        const searchText = fields
            .map(field => {
                const value = item[field];
                if (Array.isArray(value)) {
                    return value.join(' ');
                }
                return value || '';
            })
            .join(' ')
            .toLowerCase()
            .replace(/[^a-z0-9\s]/g, ' ')
            .replace(/\s+/g, ' ');

        // All query tokens must match
        return queryTokens.every(token => searchText.includes(token));
    });
}
