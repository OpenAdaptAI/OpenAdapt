"""Module for scrubbing a media file.

Usage:
    $ python -m openadapt.scripts.scrub scrub_mp4 <mp4_file_path>

"""

import math
import time

from tqdm import tqdm
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

    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    progress_bar_format = (
        "{desc}: {percentage:.0f}% "
        "| {bar} | "
        "{n_fmt}/{total_fmt} | {rate_fmt} | [{elapsed}<{remaining}] |"
    )
    progress_bar = tqdm(
        total=frame_count,
        desc="Processing",
        unit="frame",
        bar_format=progress_bar_format,
        colour="green",
    )

    progress_interval = 0.1  # Print progress every 10% of frames
    progress_threshold = math.floor(
        frame_count * progress_interval
    )
    
    a = b = c = d = 0

    while cap.isOpened():
        frame_id = cap.get(1)  # current frame number
        ret, frame = cap.read()
        if not ret:
            break
        
        a_start = time.time()
        # Convert frame to PIL.Image
        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        a += time.time() - a_start

        b_start = time.time()
        # Apply redaction to the frame
        redacted_image = scrub.scrub_image(image)
        b += time.time() - b_start

        c_start = time.time()
        # Convert redacted image back to OpenCV format
        redacted_frame = cv2.cvtColor(
            np.array(redacted_image), cv2.COLOR_RGB2BGR
        )
        c += time.time() - c_start

        d_start = time.time()
        out.write(redacted_frame)
        d += time.time() - d_start

        progress_bar.update(1)
        if frame_id >= progress_threshold:
            progress_threshold += math.floor(
                frame_count * progress_interval
            )

    cap.release()
    out.release()
    progress_bar.close()
    
    print(f"Time spent on Line 71: {a}")
    print(f"Time spent on Line 76: {b}")
    print(f"Time spent on Line 81: {c}")
    print(f"Time spent on Line 87: {d}")

    message = (
        f"Scrubbed .mp4 file saved to the relative path: {scrubbed_file}"
    )
    return message


if __name__ == "__main__":
    fire.Fire(utils.get_functions(__name__))
