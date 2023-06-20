"""Module for scrubbing a media file.

Usage: 
    $ python -m openadapt.scripts.scrub scrub_mp4 <mp4_file_path>

"""

import os
import sys

from loguru import logger
from PIL import Image
import cv2
import fire
import numpy as np

from openadapt import scrub, utils


def scrub_mp4(mp4_file: str) -> str:
    """
    Scrub a mp4 file.

    Args:
        mp4_file: Path to the mp4 file.

    Returns:
        Path to the scrubbed (redacted) mp4 file.
    """

    scrubbed_file = mp4_file[:-4] + "_scrubbed.mp4"

    cap = cv2.VideoCapture(mp4_file)
    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = int(cap.get(cv2.CAP_PROP_FPS))
    fourcc = cv2.VideoWriter_fourcc("m", "p", "4", "v")
    out = cv2.VideoWriter(
        scrubbed_file, fourcc, fps, (frame_width, frame_height)
    )

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
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

    return scrubbed_file


if __name__ == "__main__":
    fire.Fire(utils.get_functions(__name__))
