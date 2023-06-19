"""Module for scrubbing a media file.

Usage: python -m openadapt.scripts.scrub <media_file_path>

"""

import os
import sys

from loguru import logger
from PIL import Image
import cv2
import numpy as np

from openadapt import scrub


def scrub_mp4(sourceVideopath: str) -> str:
    """
    Scrub a mp4 file.

    Args:
        sourceVideopath: Path to the mp4 file.

    Returns:
        Path to the scrubbed (redacted) mp4 file.
    """

    destVideoPath = sourceVideopath[:-4] + "_scrubbed.mp4"

    cap = cv2.VideoCapture(sourceVideopath)
    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = int(cap.get(cv2.CAP_PROP_FPS))
    fourcc = cv2.VideoWriter_fourcc("m", "p", "4", "v")
    out = cv2.VideoWriter(
        destVideoPath, fourcc, fps, (frame_width, frame_height)
    )
    frameRate = fps

    while cap.isOpened():
        frameId = cap.get(1)  # current frame number
        ret, frame = cap.read()
        if ret != True:
            break
        # Convert frame to PIL.Image
        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

        # Apply redaction to the frame
        redacted_image = scrub.scrub_image(image)

        # Convert redacted image back to OpenCV format
        redacted_frame = cv2.cvtColor(
            np.array(redacted_image), cv2.COLOR_RGB2BGR
        )
        out.write(redacted_frame)

    cap.release()
    out.release()

    return destVideoPath


def scrub_media_file(media_file_path: str) -> str:
    """
    Scrub a media file based on its extension.

    Args:
        media_file_path: Path to the media file.

    Returns:
        Path to the scrubbed media file.
    """

    file_extension = os.path.splitext(media_file_path)[1].lower()
    scrub_functions = {
        ".mp4": scrub_mp4,
        # Add more extensions and corresponding functions as needed
    }

    if file_extension in scrub_functions:
        scrub_function = scrub_functions[file_extension]
        return scrub_function(media_file_path)
    logger.info(f"Unsupported media file format: {file_extension}")
    return ""


if __name__ == "__main__":
    if (
        len(sys.argv) < 2
        or len(sys.argv) > 2
        or sys.argv[1] == "-h"
        or sys.argv[1] == "--help"
    ):
        logger.info(
            "Usage: python -m openadapt.scripts.scrub <video_file_path>"
        )
        sys.exit(1)

    scrubbed_file_path = scrub_media_file(sys.argv[1])

    if scrubbed_file_path:
        logger.info(f"Scrubbed media file saved at: {scrubbed_file_path}")
    else:
        logger.info("Failed to scrub the media file.")
