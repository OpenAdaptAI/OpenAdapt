"""Example demonstrating the use of JavaScript utilities in a viewer.

This example shows how to use the shared JavaScript libraries
(search.js, filters.js, utils.js) in a viewer built with PageBuilder.
"""

from pathlib import Path

from openadapt_viewer.builders import PageBuilder


def generate_example_viewer():
    """Generate an example viewer using JavaScript utilities."""

    # Create builder with JS utilities enabled
    builder = PageBuilder(
        title="JavaScript Utilities Demo",
        include_alpine=True,
        include_search_js=True,
        include_filter_js=True,
        include_utils_js=True,
    )

    # Add header
    builder.add_header(
        title="JavaScript Utilities Demo",
        subtitle="Demonstrating search, filter, and utility functions",
    )

    # Add demo data section
    builder.add_section(
        content="""
        <div id="demo-data" style="display: none;">
            <script>
                // Sample episode data
                window.episodes = [
                    {
                        name: "Disable night shift",
                        description: "Turn off Night Shift in System Settings",
                        domain: "system",
                        duration: 45.5,
                        steps: ["Open Settings", "Click Displays", "Toggle Night Shift"],
                        created_at: "2024-01-15T10:30:00Z"
                    },
                    {
                        name: "Open Notepad",
                        description: "Launch notepad application",
                        domain: "notepad",
                        duration: 12.3,
                        steps: ["Click Start", "Search notepad", "Click Notepad"],
                        created_at: "2024-01-16T14:20:00Z"
                    },
                    {
                        name: "Draw rectangle in Paint",
                        description: "Create a rectangle shape in Paint",
                        domain: "paint",
                        duration: 67.8,
                        steps: ["Open Paint", "Select rectangle tool", "Draw shape"],
                        created_at: "2024-01-17T09:15:00Z"
                    },
                    {
                        name: "Configure nightshift schedule",
                        description: "Set up automatic Night Shift schedule",
                        domain: "system",
                        duration: 120.0,
                        steps: ["Open Settings", "Navigate to Displays", "Configure schedule"],
                        created_at: "2024-01-18T16:45:00Z"
                    }
                ];
            </script>
        </div>
        """,
        title="Demo Data"
    )

    # Add search demo
    builder.add_section(
        content="""
        <div style="background: var(--oa-bg-secondary); padding: 24px; border-radius: 12px;">
            <h3 style="margin-bottom: 16px; color: var(--oa-accent);">Search Demo (search.js)</h3>

            <div style="margin-bottom: 16px;">
                <label style="display: block; margin-bottom: 8px; font-size: 0.9rem; color: var(--oa-text-secondary);">
                    Search Query:
                </label>
                <input
                    id="search-input"
                    type="text"
                    placeholder="Try: nightshift, notepad, paint..."
                    style="width: 100%; padding: 12px; background: var(--oa-bg-tertiary); border: 1px solid var(--oa-border-color); border-radius: 8px; color: var(--oa-text-primary); font-size: 1rem;"
                />
            </div>

            <div style="margin-bottom: 16px;">
                <div style="font-size: 0.85rem; color: var(--oa-text-muted); margin-bottom: 8px;">
                    Results: <span id="search-count" style="color: var(--oa-accent); font-weight: 600;">4</span>
                </div>
                <div id="search-results" style="display: flex; flex-direction: column; gap: 8px;"></div>
            </div>

            <div style="padding: 12px; background: var(--oa-bg-tertiary); border-radius: 8px; font-family: var(--oa-font-mono); font-size: 0.85rem;">
                <strong>Try these searches:</strong><br>
                • "nightshift" → finds "Disable night shift" and "Configure nightshift"<br>
                • "shift night" → same results (order independent)<br>
                • "paint rect" → finds "Draw rectangle in Paint"<br>
                • "note" → finds "Open Notepad"
            </div>
        </div>
        """,
        title="1. Advanced Search"
    )

    # Add filter demo
    builder.add_section(
        content="""
        <div style="background: var(--oa-bg-secondary); padding: 24px; border-radius: 12px;">
            <h3 style="margin-bottom: 16px; color: var(--oa-accent);">Filter Demo (filters.js)</h3>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 16px;">
                <div>
                    <label style="display: block; margin-bottom: 8px; font-size: 0.9rem; color: var(--oa-text-secondary);">
                        Domain:
                    </label>
                    <select
                        id="domain-filter"
                        style="width: 100%; padding: 12px; background: var(--oa-bg-tertiary); border: 1px solid var(--oa-border-color); border-radius: 8px; color: var(--oa-text-primary);"
                    >
                        <option value="">All Domains</option>
                        <option value="system">System</option>
                        <option value="notepad">Notepad</option>
                        <option value="paint">Paint</option>
                    </select>
                </div>

                <div>
                    <label style="display: block; margin-bottom: 8px; font-size: 0.9rem; color: var(--oa-text-secondary);">
                        Duration (seconds):
                    </label>
                    <select
                        id="duration-filter"
                        style="width: 100%; padding: 12px; background: var(--oa-bg-tertiary); border: 1px solid var(--oa-border-color); border-radius: 8px; color: var(--oa-text-primary);"
                    >
                        <option value="">All Durations</option>
                        <option value="0-30">0-30s (Short)</option>
                        <option value="30-60">30-60s (Medium)</option>
                        <option value="60+">60s+ (Long)</option>
                    </select>
                </div>
            </div>

            <div style="margin-bottom: 16px;">
                <div style="font-size: 0.85rem; color: var(--oa-text-muted); margin-bottom: 8px;">
                    Filtered: <span id="filter-count" style="color: var(--oa-accent); font-weight: 600;">4</span>
                </div>
                <div id="filter-results" style="display: flex; flex-direction: column; gap: 8px;"></div>
            </div>
        </div>
        """,
        title="2. Multi-Field Filtering"
    )

    # Add utils demo
    builder.add_section(
        content="""
        <div style="background: var(--oa-bg-secondary); padding: 24px; border-radius: 12px;">
            <h3 style="margin-bottom: 16px; color: var(--oa-accent);">Utilities Demo (utils.js)</h3>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 16px;">
                <div style="padding: 16px; background: var(--oa-bg-tertiary); border-radius: 8px;">
                    <div style="font-size: 0.85rem; color: var(--oa-text-muted); margin-bottom: 4px;">Duration Formatting</div>
                    <div style="font-family: var(--oa-font-mono); font-size: 0.9rem;">
                        <code>45.5</code> → <span style="color: var(--oa-accent);" id="duration-demo"></span>
                    </div>
                </div>

                <div style="padding: 16px; background: var(--oa-bg-tertiary); border-radius: 8px;">
                    <div style="font-size: 0.85rem; color: var(--oa-text-muted); margin-bottom: 4px;">Timestamp Formatting</div>
                    <div style="font-family: var(--oa-font-mono); font-size: 0.9rem;">
                        <span style="color: var(--oa-accent);" id="timestamp-demo"></span>
                    </div>
                </div>

                <div style="padding: 16px; background: var(--oa-bg-tertiary); border-radius: 8px;">
                    <div style="font-size: 0.85rem; color: var(--oa-text-muted); margin-bottom: 4px;">Relative Time</div>
                    <div style="font-family: var(--oa-font-mono); font-size: 0.9rem;">
                        <span style="color: var(--oa-accent);" id="relative-demo"></span>
                    </div>
                </div>

                <div style="padding: 16px; background: var(--oa-bg-tertiary); border-radius: 8px;">
                    <div style="font-size: 0.85rem; color: var(--oa-text-muted); margin-bottom: 4px;">Percentage Formatting</div>
                    <div style="font-family: var(--oa-font-mono); font-size: 0.9rem;">
                        <code>0.856</code> → <span style="color: var(--oa-accent);" id="percentage-demo"></span>
                    </div>
                </div>
            </div>
        </div>
        """,
        title="3. Format Utilities"
    )

    # Add combined demo
    builder.add_section(
        content="""
        <div style="background: var(--oa-bg-secondary); padding: 24px; border-radius: 12px;">
            <h3 style="margin-bottom: 16px; color: var(--oa-accent);">Combined Demo</h3>

            <div style="margin-bottom: 16px;">
                <input
                    id="combined-search"
                    type="text"
                    placeholder="Search (combines with filters below)..."
                    style="width: 100%; padding: 12px; background: var(--oa-bg-tertiary); border: 1px solid var(--oa-border-color); border-radius: 8px; color: var(--oa-text-primary); font-size: 1rem; margin-bottom: 12px;"
                />

                <select
                    id="combined-domain"
                    style="padding: 12px; background: var(--oa-bg-tertiary); border: 1px solid var(--oa-border-color); border-radius: 8px; color: var(--oa-text-primary); margin-right: 8px;"
                >
                    <option value="">All Domains</option>
                    <option value="system">System</option>
                    <option value="notepad">Notepad</option>
                    <option value="paint">Paint</option>
                </select>

                <button
                    onclick="clearCombinedFilters()"
                    style="padding: 12px 24px; background: var(--oa-error); color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: 600;"
                >
                    Clear All
                </button>
            </div>

            <div style="margin-bottom: 16px;">
                <div style="font-size: 0.85rem; color: var(--oa-text-muted); margin-bottom: 8px;">
                    Results: <span id="combined-count" style="color: var(--oa-accent); font-weight: 600;">4</span>
                </div>
                <div id="combined-results" style="display: flex; flex-direction: column; gap: 8px;"></div>
            </div>
        </div>
        """,
        title="4. Search + Filter + Format (Combined)"
    )

    # Add JavaScript to wire up the demos
    builder.add_script("""
        // Helper to render episode card
        function renderEpisode(episode) {
            return `
                <div style="padding: 16px; background: var(--oa-bg-tertiary); border-radius: 8px; border-left: 4px solid var(--oa-accent);">
                    <div style="display: flex; justify-content: space-between; align-items: start; margin-bottom: 8px;">
                        <div style="font-weight: 600; color: var(--oa-text-primary);">${episode.name}</div>
                        <div style="font-size: 0.85rem; color: var(--oa-text-secondary);">${formatDuration(episode.duration)}</div>
                    </div>
                    <div style="font-size: 0.9rem; color: var(--oa-text-secondary); margin-bottom: 8px;">${episode.description}</div>
                    <div style="display: flex; gap: 12px; font-size: 0.85rem;">
                        <span style="color: var(--oa-text-muted);">Domain: <span style="color: var(--oa-accent);">${episode.domain}</span></span>
                        <span style="color: var(--oa-text-muted);">Steps: <span style="color: var(--oa-accent);">${episode.steps.length}</span></span>
                    </div>
                </div>
            `;
        }

        // Search demo
        function updateSearch() {
            const query = document.getElementById('search-input').value;
            const results = advancedSearch(episodes, query, ['name', 'description', 'steps']);

            document.getElementById('search-count').textContent = results.length;
            document.getElementById('search-results').innerHTML = results.map(renderEpisode).join('');
        }

        // Filter demo
        function updateFilters() {
            const domain = document.getElementById('domain-filter').value;
            const durationRange = document.getElementById('duration-filter').value;

            let filters = {};

            if (domain) {
                filters.domain = domain;
            }

            if (durationRange === '0-30') {
                filters.duration = { min: 0, max: 30 };
            } else if (durationRange === '30-60') {
                filters.duration = { min: 30, max: 60 };
            } else if (durationRange === '60+') {
                filters.duration = { min: 60 };
            }

            const results = filterItems(episodes, filters);

            document.getElementById('filter-count').textContent = results.length;
            document.getElementById('filter-results').innerHTML = results.map(renderEpisode).join('');
        }

        // Utils demo
        function updateUtils() {
            document.getElementById('duration-demo').textContent = formatDuration(45.5);
            document.getElementById('timestamp-demo').textContent = formatTimestamp('2024-01-15T10:30:00Z');
            document.getElementById('relative-demo').textContent = formatRelativeTime(new Date().getTime() - 3600000);
            document.getElementById('percentage-demo').textContent = formatPercentage(0.856);
        }

        // Combined demo
        function updateCombined() {
            const query = document.getElementById('combined-search').value;
            const domain = document.getElementById('combined-domain').value;

            // First apply search
            let results = advancedSearch(episodes, query, ['name', 'description', 'steps']);

            // Then apply filters
            if (domain) {
                results = filterItems(results, { domain });
            }

            document.getElementById('combined-count').textContent = results.length;
            document.getElementById('combined-results').innerHTML = results.map(renderEpisode).join('');
        }

        function clearCombinedFilters() {
            document.getElementById('combined-search').value = '';
            document.getElementById('combined-domain').value = '';
            updateCombined();
        }

        // Set up event listeners
        document.addEventListener('DOMContentLoaded', () => {
            // Search demo
            document.getElementById('search-input').addEventListener('input', updateSearch);
            updateSearch();

            // Filter demo
            document.getElementById('domain-filter').addEventListener('change', updateFilters);
            document.getElementById('duration-filter').addEventListener('change', updateFilters);
            updateFilters();

            // Utils demo
            updateUtils();

            // Combined demo
            document.getElementById('combined-search').addEventListener('input', updateCombined);
            document.getElementById('combined-domain').addEventListener('change', updateCombined);
            updateCombined();
        });
    """)

    return builder.render()


if __name__ == "__main__":
    html = generate_example_viewer()

    output_path = Path("js_utilities_demo.html")
    output_path.write_text(html)

    print(f"Generated viewer: {output_path.absolute()}")
    print("\nThis example demonstrates:")
    print("  1. Advanced search with token-based matching")
    print("  2. Multi-field filtering with range support")
    print("  3. Format utilities (duration, timestamp, etc.)")
    print("  4. Combined search + filter + format")
    print("\nOpen the HTML file in a browser to try it out!")
