"""Drawing utilities for visual event overlays.

Provides functions to draw click markers, drag arrows, text labels,
and other visual indicators on screenshot frames.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image, ImageDraw


# Default colors (RGBA)
CLICK_COLOR = (255, 59, 48, 200)  # Red
DOUBLE_CLICK_COLOR = (255, 149, 0, 200)  # Orange
DRAG_COLOR = (0, 122, 255, 200)  # Blue
SCROLL_COLOR = (88, 86, 214, 200)  # Purple
TEXT_COLOR = (52, 199, 89, 230)  # Green
LABEL_BG_COLOR = (0, 0, 0, 180)  # Semi-transparent black
LABEL_TEXT_COLOR = (255, 255, 255, 255)  # White


def draw_click(
    draw: "ImageDraw.ImageDraw",
    x: int,
    y: int,
    radius: int = 20,
    color: tuple = CLICK_COLOR,
    ring_width: int = 3,
) -> None:
    """Draw a click indicator (circle with ring).

    Args:
        draw: PIL ImageDraw object.
        x: Click x coordinate.
        y: Click y coordinate.
        radius: Radius of the click circle.
        color: RGBA color tuple.
        ring_width: Width of the ring.
    """
    # Outer ring
    draw.ellipse(
        [x - radius, y - radius, x + radius, y + radius],
        outline=color,
        width=ring_width,
    )
    # Inner dot
    inner_radius = 4
    draw.ellipse(
        [x - inner_radius, y - inner_radius, x + inner_radius, y + inner_radius],
        fill=color,
    )


def draw_double_click(
    draw: "ImageDraw.ImageDraw",
    x: int,
    y: int,
    radius: int = 20,
    color: tuple = DOUBLE_CLICK_COLOR,
) -> None:
    """Draw a double-click indicator (concentric circles).

    Args:
        draw: PIL ImageDraw object.
        x: Click x coordinate.
        y: Click y coordinate.
        radius: Radius of the outer circle.
        color: RGBA color tuple.
    """
    # Outer ring
    draw.ellipse(
        [x - radius, y - radius, x + radius, y + radius],
        outline=color,
        width=3,
    )
    # Inner ring
    inner_radius = radius * 0.6
    draw.ellipse(
        [x - inner_radius, y - inner_radius, x + inner_radius, y + inner_radius],
        outline=color,
        width=2,
    )
    # Center dot
    dot_radius = 4
    draw.ellipse(
        [x - dot_radius, y - dot_radius, x + dot_radius, y + dot_radius],
        fill=color,
    )


def draw_drag(
    draw: "ImageDraw.ImageDraw",
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    color: tuple = DRAG_COLOR,
    width: int = 3,
) -> None:
    """Draw a drag indicator (line with arrow).

    Args:
        draw: PIL ImageDraw object.
        start_x: Start x coordinate.
        start_y: Start y coordinate.
        end_x: End x coordinate.
        end_y: End y coordinate.
        color: RGBA color tuple.
        width: Line width.
    """
    import math

    # Draw the main line
    draw.line([(start_x, start_y), (end_x, end_y)], fill=color, width=width)

    # Draw start circle
    start_radius = 6
    draw.ellipse(
        [
            start_x - start_radius,
            start_y - start_radius,
            start_x + start_radius,
            start_y + start_radius,
        ],
        fill=color,
    )

    # Draw arrowhead at end
    arrow_length = 15
    arrow_angle = math.pi / 6  # 30 degrees

    # Calculate angle of the line
    dx = end_x - start_x
    dy = end_y - start_y
    angle = math.atan2(dy, dx)

    # Calculate arrowhead points
    arrow_x1 = end_x - arrow_length * math.cos(angle - arrow_angle)
    arrow_y1 = end_y - arrow_length * math.sin(angle - arrow_angle)
    arrow_x2 = end_x - arrow_length * math.cos(angle + arrow_angle)
    arrow_y2 = end_y - arrow_length * math.sin(angle + arrow_angle)

    # Draw arrowhead
    draw.polygon(
        [(end_x, end_y), (arrow_x1, arrow_y1), (arrow_x2, arrow_y2)],
        fill=color,
    )


def draw_scroll(
    draw: "ImageDraw.ImageDraw",
    x: int,
    y: int,
    dx: int,
    dy: int,
    color: tuple = SCROLL_COLOR,
) -> None:
    """Draw a scroll indicator (arrows showing direction).

    Args:
        draw: PIL ImageDraw object.
        x: Scroll x coordinate.
        y: Scroll y coordinate.
        dx: Horizontal scroll amount.
        dy: Vertical scroll amount.
        color: RGBA color tuple.
    """

    # Determine primary scroll direction
    arrow_length = 25
    arrow_width = 12

    if abs(dy) > abs(dx):
        # Vertical scroll
        direction = -1 if dy > 0 else 1  # Up if dy > 0, down if dy < 0
        # Draw vertical arrow
        tip_y = y + direction * arrow_length
        draw.polygon(
            [
                (x, tip_y),  # Tip
                (x - arrow_width // 2, y),  # Left base
                (x + arrow_width // 2, y),  # Right base
            ],
            fill=color,
        )
    else:
        # Horizontal scroll
        direction = 1 if dx > 0 else -1
        tip_x = x + direction * arrow_length
        draw.polygon(
            [
                (tip_x, y),  # Tip
                (x, y - arrow_width // 2),  # Top base
                (x, y + arrow_width // 2),  # Bottom base
            ],
            fill=color,
        )


def draw_text_bubble(
    draw: "ImageDraw.ImageDraw",
    x: int,
    y: int,
    text: str,
    bg_color: tuple = TEXT_COLOR,
    text_color: tuple = LABEL_TEXT_COLOR,
    max_chars: int = 20,
    font_size: int = 14,
) -> None:
    """Draw a text bubble showing typed text.

    Args:
        draw: PIL ImageDraw object.
        x: Bubble x coordinate.
        y: Bubble y coordinate.
        text: Text to display.
        bg_color: Background color.
        text_color: Text color.
        max_chars: Maximum characters to show.
        font_size: Font size.
    """
    from PIL import ImageFont

    # Truncate text if too long
    if len(text) > max_chars:
        display_text = text[: max_chars - 1] + "…"
    else:
        display_text = text

    # Try to get a font
    try:
        font = ImageFont.truetype("/System/Library/Fonts/SFNSMono.ttf", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()

    # Calculate text size
    bbox = draw.textbbox((0, 0), display_text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    # Draw bubble background
    padding = 6
    bubble_x1 = x - padding
    bubble_y1 = y - text_height - padding * 2
    bubble_x2 = x + text_width + padding
    bubble_y2 = y

    # Rounded rectangle for bubble
    draw.rounded_rectangle(
        [bubble_x1, bubble_y1, bubble_x2, bubble_y2],
        radius=5,
        fill=bg_color,
    )

    # Draw text
    draw.text((x, bubble_y1 + padding), display_text, fill=text_color, font=font)


def draw_label(
    draw: "ImageDraw.ImageDraw",
    x: int,
    y: int,
    text: str,
    bg_color: tuple = LABEL_BG_COLOR,
    text_color: tuple = LABEL_TEXT_COLOR,
    font_size: int = 12,
    position: str = "below",  # "above", "below", "left", "right"
) -> None:
    """Draw a label near a position.

    Args:
        draw: PIL ImageDraw object.
        x: Label anchor x coordinate.
        y: Label anchor y coordinate.
        text: Label text.
        bg_color: Background color.
        text_color: Text color.
        font_size: Font size.
        position: Where to place label relative to anchor.
    """
    from PIL import ImageFont

    # Try to get a font
    try:
        font = ImageFont.truetype("/System/Library/Fonts/SFNSMono.ttf", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()

    # Calculate text size
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    padding = 4
    offset = 25  # Distance from anchor

    # Calculate position
    if position == "below":
        label_x = x - text_width // 2
        label_y = y + offset
    elif position == "above":
        label_x = x - text_width // 2
        label_y = y - offset - text_height - padding * 2
    elif position == "left":
        label_x = x - offset - text_width - padding * 2
        label_y = y - text_height // 2
    else:  # right
        label_x = x + offset
        label_y = y - text_height // 2

    # Draw background
    draw.rounded_rectangle(
        [
            label_x - padding,
            label_y - padding,
            label_x + text_width + padding,
            label_y + text_height + padding,
        ],
        radius=3,
        fill=bg_color,
    )

    # Draw text
    draw.text((label_x, label_y), text, fill=text_color, font=font)


def draw_timestamp(
    draw: "ImageDraw.ImageDraw",
    image_width: int,
    image_height: int,
    timestamp: float,
    font_size: int = 16,
) -> None:
    """Draw timestamp overlay in corner of image.

    Args:
        draw: PIL ImageDraw object.
        image_width: Image width.
        image_height: Image height.
        timestamp: Timestamp in seconds.
        font_size: Font size.
    """
    from PIL import ImageFont

    # Format timestamp
    minutes = int(timestamp // 60)
    seconds = timestamp % 60
    text = f"{minutes}:{seconds:05.2f}"

    # Try to get a font
    try:
        font = ImageFont.truetype("/System/Library/Fonts/SFNSMono.ttf", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()

    # Calculate text size
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    # Position in bottom-right corner
    padding = 8
    margin = 15
    x = image_width - text_width - padding * 2 - margin
    y = image_height - text_height - padding * 2 - margin

    # Draw background
    draw.rounded_rectangle(
        [x, y, x + text_width + padding * 2, y + text_height + padding * 2],
        radius=5,
        fill=LABEL_BG_COLOR,
    )

    # Draw text
    draw.text((x + padding, y + padding), text, fill=LABEL_TEXT_COLOR, font=font)


def annotate_frame(
    image: "Image.Image",
    events: list,
    show_labels: bool = True,
    show_timestamp: bool = True,
    current_time: float | None = None,
) -> "Image.Image":
    """Annotate a frame with event overlays.

    Args:
        image: PIL Image to annotate.
        events: List of events to draw (with type, x, y, etc.).
        show_labels: Whether to show event type labels.
        show_timestamp: Whether to show timestamp.
        current_time: Current timestamp for display.

    Returns:
        Annotated copy of the image.
    """
    from PIL import Image, ImageDraw

    # Create a copy with alpha channel for overlay
    if image.mode != "RGBA":
        result = image.convert("RGBA")
    else:
        result = image.copy()

    # Create overlay layer
    overlay = Image.new("RGBA", result.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for event in events:
        event_type = event.get("type", "")
        x = event.get("x")
        y = event.get("y")

        # Skip events without valid coordinates
        if x is None or y is None:
            continue

        x = int(x)
        y = int(y)

        if "click" in event_type.lower() and "double" not in event_type.lower():
            draw_click(draw, x, y)
            if show_labels:
                draw_label(draw, x, y, "click", position="below")

        elif "double" in event_type.lower():
            draw_double_click(draw, x, y)
            if show_labels:
                draw_label(draw, x, y, "double-click", position="below")

        elif "drag" in event_type.lower():
            end_x = int(event.get("end_x", x))
            end_y = int(event.get("end_y", y))
            draw_drag(draw, x, y, end_x, end_y)
            if show_labels:
                draw_label(draw, x, y, "drag", position="above")

        elif "scroll" in event_type.lower():
            dx = event.get("dx", 0) or 0
            dy = event.get("dy", 0) or 0
            draw_scroll(draw, x, y, int(dx), int(dy))
            if show_labels:
                draw_label(draw, x, y, "scroll", position="below")

        elif "type" in event_type.lower() or "key" in event_type.lower():
            text = event.get("text", "")
            if text:
                draw_text_bubble(draw, x, y, text)

    # Draw timestamp
    if show_timestamp and current_time is not None:
        draw_timestamp(draw, result.width, result.height, current_time)

    # Composite overlay onto result
    result = Image.alpha_composite(result, overlay)

    return result
