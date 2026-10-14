"""Page builder for creating complete HTML pages from components.

This module provides a fluent API for building viewer HTML pages.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any


class PageBuilder:
    """Builder for creating complete HTML viewer pages.

    Example:
        builder = PageBuilder(title="My Viewer")
        builder.add_header(title="Results", subtitle="Model: gpt-5.1")
        builder.add_section(metrics_grid([...]))
        builder.add_section(screenshot_display(...))
        html = builder.render()
    """

    def __init__(
        self,
        title: str = "OpenAdapt Viewer",
        dark_mode: bool = True,
        include_alpine: bool = True,
        include_chartjs: bool = False,
        include_plotly: bool = False,
        include_search_js: bool = False,
        include_filter_js: bool = False,
        include_utils_js: bool = False,
    ):
        """Initialize the page builder.

        Args:
            title: Page title
            dark_mode: Whether to use dark mode by default
            include_alpine: Include Alpine.js for interactivity
            include_chartjs: Include Chart.js for charts
            include_plotly: Include Plotly.js for advanced charts
            include_search_js: Include search utilities (search.js)
            include_filter_js: Include filter utilities (filters.js)
            include_utils_js: Include common utilities (utils.js)
        """
        self.title = title
        self.dark_mode = dark_mode
        self.include_alpine = include_alpine
        self.include_chartjs = include_chartjs
        self.include_plotly = include_plotly
        self.include_search_js = include_search_js
        self.include_filter_js = include_filter_js
        self.include_utils_js = include_utils_js

        self._header_html = ""
        self._nav_html = ""
        self._sections: list[str] = []
        self._scripts: list[str] = []
        self._alpine_data: dict[str, Any] = {}
        self._custom_css = ""

    def add_header(
        self,
        title: str,
        subtitle: str | None = None,
        nav_tabs: list[dict[str, str]] | None = None,
        actions_html: str = "",
    ) -> PageBuilder:
        """Add a header with optional navigation tabs.

        Args:
            title: Header title
            subtitle: Optional subtitle
            nav_tabs: List of nav tabs [{href, label, active}]
            actions_html: Additional HTML for header actions

        Returns:
            Self for chaining
        """
        # Escape title and subtitle for XSS prevention
        safe_title = html.escape(title)
        subtitle_html = f'<p style="font-size: 0.85rem; color: var(--oa-text-secondary);">{html.escape(subtitle)}</p>' if subtitle else ""

        self._header_html = f'''
        <header style="padding: 16px 24px; background: var(--oa-bg-secondary); border-bottom: 1px solid var(--oa-border-color); margin-bottom: 24px;">
            <div style="display: flex; align-items: center; justify-content: space-between;">
                <div>
                    <h1 style="font-size: 1.25rem; font-weight: 600; margin: 0;">{safe_title}</h1>
                    {subtitle_html}
                </div>
                <div style="display: flex; align-items: center; gap: 16px;">
                    {actions_html}
                    {self._generate_dark_mode_toggle()}
                </div>
            </div>
        </header>
        '''

        if nav_tabs:
            self._nav_html = self._generate_nav_tabs(nav_tabs)

        return self

    def add_nav_tabs(self, tabs: list[dict[str, str]]) -> PageBuilder:
        """Add navigation tabs.

        Args:
            tabs: List of tabs [{href, label, active}]

        Returns:
            Self for chaining
        """
        self._nav_html = self._generate_nav_tabs(tabs)
        return self

    def add_section(
        self,
        content: str,
        title: str | None = None,
        class_name: str = "",
    ) -> PageBuilder:
        """Add a content section.

        Args:
            content: HTML content for the section
            title: Optional section title
            class_name: Additional CSS classes

        Returns:
            Self for chaining
        """
        extra_class = f" {class_name}" if class_name else ""
        # Escape title for XSS prevention
        title_html = f'<h2 style="font-size: 1.1rem; font-weight: 600; margin-bottom: 16px;">{html.escape(title)}</h2>' if title else ""

        self._sections.append(f'''
        <section class="oa-section{extra_class}" style="margin-bottom: 24px;">
            {title_html}
            {content}
        </section>
        ''')

        return self

    def add_script(self, script: str) -> PageBuilder:
        """Add a JavaScript script.

        Args:
            script: JavaScript code

        Returns:
            Self for chaining
        """
        self._scripts.append(script)
        return self

    def add_alpine_data(self, name: str, data: dict[str, Any]) -> PageBuilder:
        """Add Alpine.js data.

        Args:
            name: Variable name for x-data
            data: Data object

        Returns:
            Self for chaining
        """
        self._alpine_data[name] = data
        return self

    def add_css(self, css: str) -> PageBuilder:
        """Add custom CSS.

        Args:
            css: CSS code

        Returns:
            Self for chaining
        """
        self._custom_css += css
        return self

    def render(self) -> str:
        """Render the complete HTML page.

        Returns:
            Complete HTML string
        """
        # CDN scripts
        scripts_html = ""
        if self.include_alpine:
            scripts_html += '<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>\n'
        if self.include_chartjs:
            scripts_html += '<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>\n'
        if self.include_plotly:
            scripts_html += '<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>\n'

        # OpenAdapt JS utilities
        openadapt_js = self._get_openadapt_js()

        # Core CSS
        core_css = self._get_core_css()

        # Sections
        sections_html = "\n".join(self._sections)

        # Custom scripts
        custom_scripts = "\n".join(f"<script>{s}</script>" for s in self._scripts)

        # Alpine data
        alpine_data_script = ""
        if self._alpine_data:
            import json
            for name, data in self._alpine_data.items():
                alpine_data_script += f'''
                <script>
                    document.addEventListener('alpine:init', () => {{
                        Alpine.data('{name}', () => ({json.dumps(data)}))
                    }})
                </script>
                '''

        # Escape title for XSS prevention
        safe_title = html.escape(self.title)

        return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{safe_title}</title>
    {scripts_html}
    <style>
        {core_css}
        {self._custom_css}
    </style>
</head>
<body style="background: var(--oa-bg-primary); color: var(--oa-text-primary); font-family: var(--oa-font-sans); min-height: 100vh; margin: 0;">
    {self._header_html}
    {self._nav_html}
    <main style="max-width: 1400px; margin: 0 auto; padding: 0 24px 24px;">
        {sections_html}
    </main>
    <footer style="text-align: center; padding: 24px; font-size: 0.8rem; color: var(--oa-text-muted);">
        Generated by <a href="https://github.com/OpenAdaptAI/openadapt-viewer" style="color: var(--oa-accent);">openadapt-viewer</a>
    </footer>
    {openadapt_js}
    {alpine_data_script}
    {custom_scripts}
</body>
</html>'''

    def render_to_file(self, path: str | Path) -> Path:
        """Render and write to a file.

        Args:
            path: Output file path

        Returns:
            Path to the written file
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render())
        return path

    def _generate_nav_tabs(self, tabs: list[dict[str, str]]) -> str:
        """Generate navigation tabs HTML."""
        tabs_html_parts = []
        for tab in tabs:
            href = tab.get("href", "#")
            label = tab.get("label", "")
            active = tab.get("active", False)
            active_style = "background: var(--oa-accent); color: var(--oa-bg-primary);" if active else ""

            tabs_html_parts.append(f'''
            <a href="{href}" style="
                padding: 8px 16px;
                border-radius: 6px;
                font-size: 0.85rem;
                font-weight: 500;
                text-decoration: none;
                color: var(--oa-text-secondary);
                transition: all 0.2s;
                {active_style}
            ">{label}</a>
            ''')

        return f'''
        <nav style="display: flex; gap: 4px; padding: 8px 24px; background: var(--oa-bg-secondary); border-bottom: 1px solid var(--oa-border-color); margin-bottom: 24px;">
            {"".join(tabs_html_parts)}
        </nav>
        '''

    def _generate_dark_mode_toggle(self) -> str:
        """Generate dark mode toggle button."""
        return '''
        <button onclick="document.body.classList.toggle('oa-light')"
                style="padding: 8px; border-radius: 8px; background: var(--oa-bg-tertiary); border: none; cursor: pointer; color: var(--oa-text-primary);"
                title="Toggle dark/light mode">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                <path d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z"/>
            </svg>
        </button>
        '''

    def _get_core_css(self) -> str:
        """Return core CSS with variables and base styles."""
        # Read from styles/core.css if available, otherwise inline minimal version
        css_path = Path(__file__).parent.parent / "styles" / "core.css"
        try:
            if css_path.exists():
                return css_path.read_text()
        except (OSError, UnicodeDecodeError) as e:
            # A packaged core.css that exists but cannot be read used to fall
            # through to the truncated inline fallback in total silence, so a
            # page rendered with visibly different styling and nothing said why.
            print(f"Warning: Could not read {css_path}, using inline CSS: {e}")

        # Fallback minimal CSS
        return '''
        :root {
            --oa-bg-primary: #0a0a0f;
            --oa-bg-secondary: #12121a;
            --oa-bg-tertiary: #1a1a24;
            --oa-border-color: rgba(255, 255, 255, 0.06);
            --oa-text-primary: #f0f0f0;
            --oa-text-secondary: #888;
            --oa-text-muted: #555;
            --oa-accent: #00d4aa;
            --oa-accent-dim: rgba(0, 212, 170, 0.15);
            --oa-success: #34d399;
            --oa-error: #ff5f5f;
            --oa-warning: #f59e0b;
            --oa-info: #3b82f6;
            --oa-font-sans: -apple-system, BlinkMacSystemFont, "Inter", sans-serif;
            --oa-font-mono: "SF Mono", Monaco, Consolas, monospace;
        }
        .oa-light {
            --oa-bg-primary: #ffffff;
            --oa-bg-secondary: #f3f4f6;
            --oa-bg-tertiary: #e5e7eb;
            --oa-border-color: rgba(0, 0, 0, 0.1);
            --oa-text-primary: #111827;
            --oa-text-secondary: #6b7280;
            --oa-text-muted: #9ca3af;
        }
        '''

    def _get_openadapt_js(self) -> str:
        """Return OpenAdapt JavaScript utilities inline.

        Returns:
            JavaScript code wrapped in <script> tags
        """
        scripts = []

        # Read and inline JavaScript files
        js_dir = Path(__file__).parent.parent / "js"

        if self.include_search_js:
            search_path = js_dir / "search.js"
            if search_path.exists():
                content = search_path.read_text()
                # Remove export statements for inline usage
                content = content.replace("export function", "function")
                content = content.replace("export {", "// export {")
                scripts.append(f"<script>\n{content}\n</script>")

        if self.include_filter_js:
            filter_path = js_dir / "filters.js"
            if filter_path.exists():
                content = filter_path.read_text()
                # Remove export statements for inline usage
                content = content.replace("export function", "function")
                content = content.replace("export {", "// export {")
                scripts.append(f"<script>\n{content}\n</script>")

        if self.include_utils_js:
            utils_path = js_dir / "utils.js"
            if utils_path.exists():
                content = utils_path.read_text()
                # Remove export statements for inline usage
                content = content.replace("export function", "function")
                content = content.replace("export {", "// export {")
                scripts.append(f"<script>\n{content}\n</script>")

        return "\n".join(scripts)
