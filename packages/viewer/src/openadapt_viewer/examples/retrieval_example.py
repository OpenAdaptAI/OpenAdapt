"""Example: Retrieval results viewer using component library.

This shows how openadapt-retrieval can use the component library to
display demo search results with similarity scores.

Usage:
    python -m openadapt_viewer.examples.retrieval_example
"""

from __future__ import annotations

from pathlib import Path

from openadapt_viewer.builders import PageBuilder
from openadapt_viewer.components import (
    metrics_grid,
    screenshot_display,
)


def generate_retrieval_viewer(
    query: str = "Turn off Night Shift",
    query_screenshot: str | Path | None = None,
    results: list[dict] | None = None,
    output_path: str | Path = "retrieval_viewer.html",
) -> Path:
    """Generate a retrieval results viewer.

    Args:
        query: Search query text
        query_screenshot: Optional query screenshot path
        results: List of retrieval results [{demo_id, task, similarity, screenshot}]
        output_path: Output HTML file path

    Returns:
        Path to generated HTML file
    """
    # Sample data if not provided
    if results is None:
        results = _generate_sample_results()

    top_score = max(r.get("similarity", 0) for r in results) if results else 0
    avg_score = sum(r.get("similarity", 0) for r in results) / len(results) if results else 0

    # Build page
    builder = PageBuilder(
        title="Retrieval Results",
        include_alpine=True,
    )

    # Header
    builder.add_header(
        title="Demo Retrieval Results",
        subtitle=f'Query: "{query}"',
    )

    # Query info
    query_screenshot_html = ""
    if query_screenshot:
        query_screenshot_html = screenshot_display(
            image_path=str(query_screenshot),
            width=400,
            height=250,
            caption="Query Screenshot",
        )

    builder.add_section(f'''
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px; align-items: start;">
            <div>
                <div style="font-size: 0.85rem; color: var(--oa-text-muted); margin-bottom: 8px;">Query Text</div>
                <div style="background: var(--oa-bg-secondary); padding: 16px; border-radius: 8px; font-size: 1.1rem;">
                    {query}
                </div>
            </div>
            <div>
                {query_screenshot_html if query_screenshot else '<div style="text-align: center; color: var(--oa-text-muted); padding: 40px;">No query screenshot</div>'}
            </div>
        </div>
    ''', title="Query")

    # Search stats
    builder.add_section(
        metrics_grid([
            {"label": "Results", "value": len(results)},
            {"label": "Top Score", "value": f"{top_score:.3f}", "color": "accent"},
            {"label": "Avg Score", "value": f"{avg_score:.3f}"},
        ], columns=3),
    )

    # Results list
    results_html_parts = []
    for i, result in enumerate(results):
        demo_id = result.get("demo_id", f"demo_{i+1}")
        task = result.get("task", "Unknown task")
        similarity = result.get("similarity", 0)
        screenshot = result.get("screenshot")
        app_name = result.get("app_name", "")
        domain = result.get("domain", "")

        # Similarity bar color based on score
        if similarity >= 0.8:
            bar_color = "var(--oa-success)"
        elif similarity >= 0.6:
            bar_color = "var(--oa-accent)"
        elif similarity >= 0.4:
            bar_color = "var(--oa-warning)"
        else:
            bar_color = "var(--oa-error)"

        screenshot_html = ""
        if screenshot:
            screenshot_html = screenshot_display(
                image_path=screenshot,
                width=200,
                height=120,
            )
        else:
            screenshot_html = '<div style="width: 200px; height: 120px; background: var(--oa-bg-tertiary); border-radius: 8px; display: flex; align-items: center; justify-content: center; color: var(--oa-text-muted); font-size: 0.8rem;">No preview</div>'

        metadata_parts = []
        if app_name:
            metadata_parts.append(f"App: {app_name}")
        if domain:
            metadata_parts.append(f"Domain: {domain}")
        metadata_html = " | ".join(metadata_parts) if metadata_parts else ""

        results_html_parts.append(f'''
            <div style="background: var(--oa-bg-secondary); border-radius: 12px; padding: 16px; display: flex; gap: 16px;">
                <div style="flex-shrink: 0;">
                    {screenshot_html}
                </div>
                <div style="flex: 1; min-width: 0;">
                    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
                        <span style="font-weight: 600; color: var(--oa-accent);">#{i+1}</span>
                        <span style="font-size: 0.85rem; color: var(--oa-text-muted);">{demo_id}</span>
                    </div>
                    <div style="font-size: 1rem; margin-bottom: 12px;">{task}</div>
                    <div style="margin-bottom: 8px;">
                        <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 4px;">
                            <span style="font-size: 0.75rem; color: var(--oa-text-muted);">Similarity</span>
                            <span style="font-weight: 600; color: {bar_color};">{similarity:.3f}</span>
                        </div>
                        <div style="height: 6px; background: var(--oa-bg-tertiary); border-radius: 3px; overflow: hidden;">
                            <div style="height: 100%; width: {similarity * 100}%; background: {bar_color}; border-radius: 3px;"></div>
                        </div>
                    </div>
                    {f'<div style="font-size: 0.75rem; color: var(--oa-text-muted);">{metadata_html}</div>' if metadata_html else ''}
                </div>
            </div>
        ''')

    builder.add_section(f'''
        <div style="display: flex; flex-direction: column; gap: 16px;">
            {"".join(results_html_parts)}
        </div>
    ''', title=f"Top {len(results)} Results")

    return builder.render_to_file(output_path)


def _generate_sample_results() -> list[dict]:
    """Generate sample retrieval results."""
    results = [
        {
            "demo_id": "turn-off-nightshift-macos",
            "task": "Turn off Night Shift in macOS System Settings",
            "similarity": 0.95,
            "app_name": "System Settings",
            "domain": "system",
        },
        {
            "demo_id": "adjust-display-brightness",
            "task": "Adjust display brightness in System Settings",
            "similarity": 0.78,
            "app_name": "System Settings",
            "domain": "system",
        },
        {
            "demo_id": "enable-dark-mode",
            "task": "Enable dark mode in macOS appearance settings",
            "similarity": 0.71,
            "app_name": "System Settings",
            "domain": "system",
        },
        {
            "demo_id": "change-wallpaper",
            "task": "Change desktop wallpaper in System Settings",
            "similarity": 0.52,
            "app_name": "System Settings",
            "domain": "system",
        },
        {
            "demo_id": "set-alarm-clock",
            "task": "Set an alarm in the Clock app",
            "similarity": 0.23,
            "app_name": "Clock",
            "domain": "utility",
        },
    ]
    return results


if __name__ == "__main__":
    output = generate_retrieval_viewer()
    print(f"Generated: {output}")
