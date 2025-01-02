from pprint import pformat

from loguru import logger
import cv2
import numpy as np

from openadapt.db import crud


def embed_description(image, description, x=None, y=None):
    """Embed a description into an image at the specified location.

    Args:
        image (np.ndarray): The image to annotate.
        description (str): The text to embed.
        x (int, optional): The x-coordinate. Defaults to None (centered).
        y (int, optional): The y-coordinate. Defaults to None (centered).

    Returns:
        np.ndarray: The annotated image.
    """
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 1
    font_color = (255, 255, 255)  # White
    line_type = 1

    # Split description into multiple lines
    max_width = 60  # Maximum characters per line
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

    # Default to center if coordinates are not provided
    if x is None or y is None:
        x = image.shape[1] // 2
        y = image.shape[0] // 2

    # Draw semi-transparent background and text
    for i, line in enumerate(lines):
        text_size, _ = cv2.getTextSize(line, font, font_scale, line_type)
        text_x = max(0, min(x - text_size[0] // 2, image.shape[1] - text_size[0]))
        text_y = y + i * 20

        # Draw background
        cv2.rectangle(
            image,
            (text_x - 15, text_y - 25),
            (text_x + text_size[0] + 15, text_y + 15),
            (0, 0, 0),
            -1,
        )

        # Draw text
        cv2.putText(
            image,
            line,
            (text_x, text_y),
            font,
            font_scale,
            font_color,
            line_type,
        )

    return image


def main() -> None:
    with crud.get_new_session(read_only=True) as session:
        recording = crud.get_latest_recording(session)
        action_events = recording.processed_action_events
        descriptions = []
        for action in action_events:
            description, image = action.prompt_for_description(return_image=True)

            # Convert image to numpy array for OpenCV compatibility
            image = np.array(image)

            if action.mouse_x is not None and action.mouse_y is not None:
                # Use the mouse coordinates for mouse events
                annotated_image = embed_description(
                    image,
                    description,
                    x=int(action.mouse_x) * 2,
                    y=int(action.mouse_y) * 2,
                )
            else:
                # Center the text for other events
                annotated_image = embed_description(image, description)

            logger.info(f"{action=}")
            logger.info(f"{description=}")
            cv2.imshow("Annotated Image", annotated_image)
            cv2.waitKey(0)
            descriptions.append(description)

        logger.info(f"descriptions=\n{pformat(descriptions)}")


if __name__ == "__main__":
    main()
