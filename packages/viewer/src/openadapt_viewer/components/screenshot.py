"""Screenshot display component with overlay support.

This component renders screenshots with optional overlays for:
- Click markers (human vs predicted)
- Bounding box highlights
- Labels and captions
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import TypedDict


class Overlay(TypedDict, total=False):
    """Overlay marker definition."""

    type: str  # "click", "box", "highlight"
    x: float  # Normalized x coordinate (0-1)
    y: float  # Normalized y coordinate (0-1)
    width: float  # For box overlays, normalized width
    height: float  # For box overlays, normalized height
    label: str  # Label text (e.g., "H" for human, "AI" for predicted)
    color: str  # CSS color
    variant: str  # "human", "predicted", "error"


def screenshot_display(
    image_path: str | Path | None = None,
    width: int = 800,
    height: int = 450,
    overlays: list[Overlay] | None = None,
    caption: str | None = None,
    embed_image: bool = False,
    placeholder_text: str = "No screenshot available",
    class_name: str = "",
) -> str:
    """Render a screenshot with optional overlays.

    Args:
        image_path: Path to screenshot image (can be None for placeholder)
        width: Display width in pixels
        height: Display height in pixels
        overlays: List of overlay markers (clicks, boxes, etc.)
        caption: Optional caption text below image
        embed_image: If True, embed image as base64 data URI
        placeholder_text: Text to show when no image
        class_name: Additional CSS classes

    Returns:
        HTML string for the screenshot display
    """
    overlays = overlays or []
    extra_class = f" {class_name}" if class_name else ""

    # Handle image source
    if image_path is None:
        image_html = f'<div class="oa-screenshot-placeholder">{placeholder_text}</div>'
    elif embed_image:
        # Embed as base64 data URI
        path = Path(image_path)
        if path.exists():
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode()
            suffix = path.suffix.lower()
            mime = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }.get(suffix, "image/png")
            image_html = f'<img src="data:{mime};base64,{data}" alt="Screenshot" class="oa-screenshot-image">'
        else:
            image_html = f'<div class="oa-screenshot-placeholder">Image not found: {image_path}</div>'
    else:
        image_html = f'<img src="{image_path}" alt="Screenshot" class="oa-screenshot-image">'

    # Generate overlay markers
    overlay_html_parts = []
    for overlay in overlays:
        overlay_type = overlay.get("type", "click")
        x = overlay.get("x", 0)
        y = overlay.get("y", 0)
        label = overlay.get("label", "")
        variant = overlay.get("variant", "")

        variant_class = f" oa-overlay-{variant}" if variant else ""

        if overlay_type == "click":
            label_html = f'<span class="oa-overlay-label">{label}</span>' if label else ''
            overlay_html_parts.append(
                f'<div class="oa-overlay oa-overlay-click{variant_class}" '
                f'style="left: {x * 100}%; top: {y * 100}%;">'
                f'{label_html}'
                f"</div>"
            )
        elif overlay_type == "box":
            w = overlay.get("width", 0.1)
            h = overlay.get("height", 0.1)
            label_html = f'<span class="oa-overlay-label">{label}</span>' if label else ''
            overlay_html_parts.append(
                f'<div class="oa-overlay oa-overlay-box{variant_class}" '
                f'style="left: {x * 100}%; top: {y * 100}%; '
                f'width: {w * 100}%; height: {h * 100}%;">'
                f'{label_html}'
                f"</div>"
            )

    overlays_html = "\n".join(overlay_html_parts)

    # Caption
    caption_html = f'<div class="oa-screenshot-caption">{caption}</div>' if caption else ""

    return f'''<div class="oa-screenshot-container{extra_class}" style="width: {width}px; height: {height}px;">
    {image_html}
    {overlays_html}
    {caption_html}
</div>'''
