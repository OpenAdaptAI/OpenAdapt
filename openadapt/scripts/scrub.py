"""Module for scrubbing a media file.

Usage:
    $ python -m openadapt.scripts.scrub scrub_mp4 <mp4_file_path> \
        <scrub_all_entities> <playback_speed_multiplier> <crop_start_time> \
        <crop_end_time>

Parameters:
        mp4_file_path: Path to the mp4 file (str)
        scrub_all_entities: True/False
        playback_speed_multiplier: (float/int)
        crop_start_time: (int) [in seconds]
        end_start_time: (int) [in seconds]

    All arguments are required at command line.

Example: To redact all entities in sample2.mp4
         from the 2nd second to the 16th second and play it at 2x speed:
    $ python -m openadapt.scripts.scrub scrub_mp4 sample2.mp4 True 2 2 16
"""

from typing import Optional
import math

from loguru import logger
from moviepy.editor import VideoClip, VideoFileClip
from PIL import Image

from openadapt.build_utils import redirect_stdout_stderr

with redirect_stdout_stderr():
    from tqdm import tqdm
    import fire

import numpy as np

from openadapt import scrub, utils
from openadapt.config import config


def _make_frame(
    time: int,
    final: VideoFileClip,
    progress_bar: tqdm.std.tqdm,
    progress_threshold: int,
) -> np.ndarray:
    """Private function to scrub a frame.

    Args:
        time: Time (in seconds)
        final: Final video clip
        progress_bar: Progress bar
        frame_count: Total number of frames
        progress_interval: Progress interval
        progress_threshold: Progress threshold

    Returns:
        A Redacted frame
    """
    frame = final.get_frame(time)

    image = Image.fromarray(frame)

    redacted_image = scrub.scrub_image(image)  # Redaction

    # Convert redacted image back to OpenCV format
    redacted_frame = np.array(redacted_image)

    progress_bar.update(1)  # Update the progress bar

    if progress_bar.n >= progress_threshold:
        progress_threshold += progress_threshold

    return redacted_frame


def scrub_mp4(
    mp4_file: str,
    scrub_all_entities: bool = False,
    playback_speed_multiplier: float = 1.0,
    crop_start_time: int = 0,
    crop_end_time: Optional[int] = None,
) -> str:
    """Scrub a mp4 file.

    Args:
        mp4_file_path: Path to the mp4 file.
        scrub_all_entities: True/False. If true, scrubs all entities
        playback_speed_multiplier: Multiplier for playback speed. (float/int)
        crop_start_time: Start Time (in seconds)
        end_start_time: End Time (in seconds)

    Returns:
        Path to the scrubbed (redacted) mp4 file.
    """
    logger.info(f"{mp4_file=}")
    logger.info(f"{scrub_all_entities=}")
    logger.info(f"{playback_speed_multiplier=}")
    logger.info(f"{crop_start_time=}")
    logger.info(f"{crop_end_time=}")

    if scrub_all_entities:
        config.SCRUB_IGNORE_ENTITIES = []

    mp4_clip = VideoFileClip(mp4_file)
    cropped_clip = mp4_clip.subclip(crop_start_time, crop_end_time)
    final = cropped_clip.fx(VideoClip.speedx, playback_speed_multiplier)

    # Prepare progress bar
    frame_count = round(final.duration * final.fps)
    progress_bar_format = (
        "{desc}: {percentage:.0f}% "
        "| {bar} | "
        "{n_fmt}/{total_fmt} | {rate_fmt} | [{elapsed}<{remaining}] |"
    )
    with redirect_stdout_stderr():
        progress_bar = tqdm(
            total=frame_count,
            desc="Processing",
            unit="frame",
            bar_format=progress_bar_format,
            colour="green",
            dynamic_ncols=True,
        )
        progress_interval = 0.1  # Print progress every 10% of frames
        progress_threshold = math.floor(frame_count * progress_interval)

        redacted_clip = VideoClip(
            make_frame=lambda t: _make_frame(
                t, final, progress_bar, progress_threshold
            ),
            duration=final.duration,
        )  # Redact the clip

        scrubbed_file = mp4_file[:-4] + "_scrubbed.mp4"
        redacted_clip.write_videofile(
            scrubbed_file, fps=final.fps, logger=None
        )  # Write the redacted clip to a file

        progress_bar.close()
    return "Scrubbed File Saved at: " + scrubbed_file


if __name__ == "__main__":
    fire.Fire(utils.get_functions(__name__))
