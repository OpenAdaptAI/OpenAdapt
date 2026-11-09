"""Generate natural language descriptions from actions."""

from pprint import pformat
from loguru import logger
from PIL import Image, ImageDraw
import numpy as np

from openadapt.db import crud
from openadapt.plotting import get_font
from openadapt.utils import get_scaling_factor

scaling_factor = get_scaling_factor()


def embed_description(
    image: Image.Image,
    description: str,
    x: int = 0,
    y: int = 0,
) -> Image.Image:
    """Embed a description into an image at the specified location.

    Args:
        image (Image.Image): The image to annotate.
        description (str): The text to embed.
        x (int, optional): The x-coordinate. Defaults to 0.
        y (int, optional): The y-coordinate. Defaults to 0.

    Returns:
        Image.Image: The annotated image.
    """
    draw = ImageDraw.Draw(image)
    font_size = 30  # Set font size (2x the default size)
    font = get_font("Arial.ttf", font_size)

    # Split description into multiple lines
    max_width = image.width
    words = description.split()
    lines = []
    current_line = []
    for word in words:
        if len(" ".join(current_line + [word])) <= max_width:
            current_line.append(word)
        else:
            lines.append(" ".join(current_line))
            current_line = [word]
    if current_line:
        lines.append(" ".join(current_line))

    # Adjust coordinates for scaling factor
    x = int(x * scaling_factor)
    y = int(y * scaling_factor)

    # Calculate text dimensions and draw semi-transparent background and text
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        text_x = max(0, min(x - text_width // 2, image.width - text_width))
        text_y = y + i * text_height

        # Draw background
        background_box = (
            text_x - 15,
            text_y - 5,
            text_x + text_width + 15,
            text_y + text_height + 5,
        )
        draw.rectangle(background_box, fill=(0, 0, 0, 128))

        # Draw text
        draw.text((text_x, text_y), line, fill=(255, 255, 255), font=font)

    return image


def main() -> None:
    """Main function."""
    with crud.get_new_session(read_only=True) as session:
        recording = crud.get_latest_recording(session)
        action_events = recording.processed_action_events
        descriptions = []
        for action in action_events:
            description, image = action.prompt_for_description(return_image=True)

            # Convert image to PIL.Image for compatibility
            image = Image.fromarray(np.array(image))

            if action.mouse_x is not None and action.mouse_y is not None:
                # Use the mouse coordinates for mouse events
                annotated_image = embed_description(
                    image,
                    description,
                )
            else:
                # Center the text for other events
                annotated_image = embed_description(image, description)

            logger.info(f"{action=}")
            logger.info(f"{description=}")
            annotated_image.show()  # Opens the annotated image using the default viewer
            descriptions.append(description)

        logger.info(f"descriptions=\n{pformat(descriptions)}")


if __name__ == "__main__":
    main()
