"""Jinja2 HTML builder for openadapt-viewer.

This module sets up the Jinja2 template environment and provides
utilities for rendering templates to HTML files.
"""

import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape


class HTMLBuilder:
    """Builder for generating HTML using Jinja2 templates.

    This class manages the Jinja2 environment and provides methods
    for rendering templates with data.
    """

    def __init__(self):
        """Initialize the HTML builder with Jinja2 environment."""
        self.env = Environment(
            loader=PackageLoader("openadapt_viewer", "templates"),
            autoescape=select_autoescape(["html", "xml"]),
        )
        # Add custom filters
        self.env.filters["tojson_safe"] = self._tojson_safe
        self.env.filters["format_duration"] = self._format_duration
        self.env.filters["format_percent"] = self._format_percent

    @staticmethod
    def _tojson_safe(value: Any) -> str:
        """Convert value to JSON, safe for embedding in HTML script tags.

        Escapes </script> tags to prevent XSS.
        """
        return json.dumps(value).replace("</", "<\\/")

    @staticmethod
    def _format_duration(seconds: float | None) -> str:
        """Format duration in seconds to human-readable string."""
        if seconds is None:
            return "N/A"
        seconds = int(seconds)
        if seconds < 60:
            return f"{seconds}s"
        minutes = seconds // 60
        remaining = seconds % 60
        if minutes < 60:
            return f"{minutes}m {remaining}s"
        hours = minutes // 60
        remaining_mins = minutes % 60
        return f"{hours}h {remaining_mins}m"

    @staticmethod
    def _format_percent(value: float | None, decimals: int = 1) -> str:
        """Format a decimal as a percentage string."""
        if value is None:
            return "N/A"
        return f"{value * 100:.{decimals}f}%"

    def render_template(self, template_name: str, **context: Any) -> str:
        """Render a template with the given context.

        Args:
            template_name: Name of the template file (relative to templates/)
            **context: Variables to pass to the template

        Returns:
            Rendered HTML string
        """
        template = self.env.get_template(template_name)
        return template.render(**context)

    def render_to_file(
        self, template_name: str, output_path: Path | str, **context: Any
    ) -> None:
        """Render a template and write to a file.

        Args:
            template_name: Name of the template file
            output_path: Path where the HTML file will be written
            **context: Variables to pass to the template
        """
        html = self.render_template(template_name, **context)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            f.write(html)

    def render_inline(self, template_string: str, **context: Any) -> str:
        """Render a template from a string.

        Args:
            template_string: Jinja2 template as a string
            **context: Variables to pass to the template

        Returns:
            Rendered HTML string
        """
        template = self.env.from_string(template_string)
        return template.render(**context)
