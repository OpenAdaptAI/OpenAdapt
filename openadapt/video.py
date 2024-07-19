"""Module for recording and manipulating video recordings."""

from fractions import Fraction
from pprint import pformat
import os
import subprocess
import tempfile
import threading

from PIL import Image
import av

from openadapt import utils
from openadapt.config import config
from openadapt.custom_logger import logger


def get_video_file_path(recording_timestamp: float) -> str:
    """Generates a file path for a video recording based on a timestamp.

    Args:
        recording_timestamp (float): The timestamp of the recording.

    Returns:
        str: The generated file name for the video recording.
    """
    os.makedirs(config.VIDEO_DIR_PATH, exist_ok=True)
    return os.path.join(
        config.VIDEO_DIR_PATH, f"oa_recording-{recording_timestamp}.mp4"
    )


def delete_video_file(recording_timestamp: float) -> None:
    """Deletes the video file corresponding to the given recording timestamp.

    Args:
        recording_timestamp (float): The timestamp of the recording to delete.
    """
    video_file_path = get_video_file_path(recording_timestamp)
    if os.path.exists(video_file_path):
        os.remove(video_file_path)
        logger.info(f"Deleted video file: {video_file_path}")
    else:
        logger.error(f"Video file not found: {video_file_path}")


def initialize_video_writer(
    output_path: str,
    width: int,
    height: int,
    fps: int = 24,
    codec: str = config.VIDEO_ENCODING,
    pix_fmt: str = config.VIDEO_PIXEL_FORMAT,
    crf: int = 0,
    preset: str = "veryslow",
) -> tuple[av.container.OutputContainer, av.stream.Stream, float]:
    """Initializes video writer and returns the container, stream, and base timestamp.

    Args:
        output_path (str): Path to the output video file.
        width (int): Width of the video.
        height (int): Height of the video.
        fps (int, optional): Frames per second of the video. Defaults to 24.
        codec (str, optional): Codec used for encoding the video.
            Defaults to 'libx264'.
        pix_fmt (str, optional): Pixel format of the video. Defaults to 'yuv420p'.
        crf (int, optional): Constant Rate Factor for encoding quality.
            Defaults to 0 for lossless.
        preset (str, optional): Encoding speed/quality trade-off.
            Defaults to 'veryslow' for maximum compression.

    Returns:
        tuple[av.container.OutputContainer, av.stream.Stream, float]: The initialized
            container, stream, and base timestamp.
    """
    logger.info("initializing video stream...")
    video_container = av.open(output_path, mode="w")
    video_stream = video_container.add_stream(codec, rate=fps)
    video_stream.width = width
    video_stream.height = height
    video_stream.pix_fmt = pix_fmt
    video_stream.options = {"crf": str(crf), "preset": preset}

    base_timestamp = utils.get_timestamp()

    return video_container, video_stream, base_timestamp


def write_video_frame(
    video_container: av.container.OutputContainer,
    video_stream: av.stream.Stream,
    screenshot: Image.Image,
    timestamp: float,
    video_start_timestamp: float,
    last_pts: int,
    force_key_frame: bool = False,
) -> int:
    """Encodes and writes a video frame to the output container from a given screenshot.

    This function converts a PIL.Image to an AVFrame,
    and encodes it for writing to the video stream. It calculates the
    presentation timestamp (PTS) for each frame based on the elapsed time since
    the base timestamp, ensuring monotonically increasing PTS values.

    Args:
        video_container (av.container.OutputContainer): The output container to which
            the frame is written.
        video_stream (av.stream.Stream): The video stream within the container.
        screenshot (Image.Image): The screenshot to be written as a video frame.
        timestamp (float): The timestamp of the current frame.
        video_start_timestamp (float): The base timestamp from which the video
            recording started.
        last_pts (int): The PTS of the last written frame.
        force_key_frame (bool): Whether to force this frame to be a key frame.

    Returns:
        int: The updated last_pts value, to be used for writing the next frame.

    Note:
        - It is crucial to maintain monotonically increasing PTS values for the
              video stream's consistency and playback.
        - The function logs the current timestamp, base timestamp, and
              calculated PTS values for debugging purposes.
    """
    # Convert the PIL Image to an AVFrame
    av_frame = av.VideoFrame.from_image(screenshot)

    # Optionally force a key frame
    # TODO: force key frames on active window change?
    if force_key_frame:
        av_frame.pict_type = "I"

    # Calculate the time difference in seconds
    time_diff = timestamp - video_start_timestamp

    # Calculate PTS, taking into account the fractional average rate
    pts = int(time_diff * float(Fraction(video_stream.average_rate)))

    logger.debug(
        f"{timestamp=} {video_start_timestamp=} {time_diff=} {pts=} {force_key_frame=}"
    )

    # Ensure monotonically increasing PTS
    if pts <= last_pts:
        pts = last_pts + 1
        logger.debug(f"incremented {pts=}")
    av_frame.pts = pts
    last_pts = pts  # Update the last_pts

    # Encode and write the frame
    for packet in video_stream.encode(av_frame):
        packet.pts = pts
        video_container.mux(packet)

    return last_pts  # Return the updated last_pts for the next call


def finalize_video_writer(
    video_container: av.container.OutputContainer,
    video_stream: av.stream.Stream,
    video_start_timestamp: float,
    last_frame: Image.Image,
    last_frame_timestamp: float,
    last_pts: int,
    video_file_path: str,
    fix_moov: bool = False,
) -> None:
    """Finalizes the video writer, ensuring all buffered frames are encoded and written.

    Args:
        video_container (av.container.OutputContainer): The AV container to finalize.
        video_stream (av.stream.Stream): The AV stream to finalize.
        video_start_timestamp (float): The base timestamp from which the video
            recording started.
        last_frame (Image.Image): The last frame that was written (to be written again).
        last_frame_timestamp (float): The timestamp of the last frame that was written.
        last_pts (int): The last presentation timestamp.
        video_file_path (str): The path to the video file.
        fix_moov (bool): Whether to move the moov atom to the beginning of the file.
            Setting this to True will fix a bug when displaying the video in Github
            comments causing the video to appear to start a few seconds after 0:00.
            However, this causes extract_frames to fail.
    """
    # Closing the container in the main thread leads to a GIL deadlock.
    # https://github.com/PyAV-Org/PyAV/issues/1053

    # Write a final key frame
    last_pts = write_video_frame(
        video_container,
        video_stream,
        last_frame,
        last_frame_timestamp,
        video_start_timestamp,
        last_pts,
        force_key_frame=True,
    )

    # Closing in the same thread sometimes hangs, so do it in a different thread:

    # Define a function to close the container
    def close_container() -> None:
        logger.info("closing video container...")
        video_container.close()

    # Create a new thread to close the container
    close_thread = threading.Thread(target=close_container)

    # Flush stream
    logger.info("flushing video stream...")
    for packet in video_stream.encode():
        video_container.mux(packet)

    # Start the thread to close the container
    close_thread.start()

    # Wait for the thread to finish execution
    close_thread.join()

    # Move moov atom to beginning of file
    if fix_moov:
        # TODO: fix this
        logger.warning(f"{fix_moov=} will cause extract_frames() to fail!!!")
        move_moov_atom(video_file_path)

    logger.info("done")


def move_moov_atom(input_file: str, output_file: str = None) -> None:
    """Moves the moov atom to the beginning of the video file using ffmpeg.

    If no output file is specified, modifies the input file in place.

    Args:
        input_file (str): The path to the input MP4 file.
        output_file (str, optional): The path to the output MP4 file where the moov
            atom is at the beginning. If None, modifies the input file in place.
    """
    if output_file is None:
        # Create a temporary file
        temp_file = tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".mp4",
            dir=os.path.dirname(input_file),
        ).name
        output_file = temp_file

    command = [
        "ffmpeg",
        "-y",  # Automatically overwrite files without asking
        "-i",
        input_file,
        "-codec",
        "copy",  # Avoid re-encoding; just copy streams
        "-movflags",
        "faststart",  # Move the moov atom to the start
        output_file,
    ]
    logger.info(f"{command=}")
    subprocess.run(command, check=True)

    if temp_file:
        # Replace the original file with the modified one
        os.replace(temp_file, input_file)


def extract_frames(
    video_filename: str,
    timestamps: list[str],
    tolerance: float = 0.1,
) -> list[Image.Image]:
    """Extracts frames from a video file at specified timestamps within a tolerance.

    Args:
        video_filename (str): The path to the video file.
        timestamps (list): A list of timestamps (in seconds) at which to extract frames.
        tolerance (float, optional): The maximum difference in seconds between
            the timestamp and the actual frame timestamp. Defaults to 0.1.

    Returns:
        list: A list of extracted frames as PIL Image objects.

    Raises:
        Exception: If a frame is found to be the closest for more than one timestamp.
        Exception: If no frame is found within the tolerance for any of the timestamps.
    """
    # Open the video file
    video_container = av.open(video_filename)
    video_stream = video_container.streams.video[0]  # Assuming the first video stream

    # To store matched frames
    timestamp_frames = {t: None for t in timestamps}
    # To store closest frame differences
    frame_differences = {t: float("inf") for t in timestamps}

    # Prepare to convert PTS to seconds
    time_base = float(video_stream.time_base)

    for frame in video_container.decode(video_stream):
        frame_timestamp = frame.pts * time_base  # Convert to float
        # Find the closest timestamp within tolerance
        for timestamp in timestamps:
            difference = abs(frame_timestamp - timestamp)
            # if difference <= tolerance and difference < frame_differences[timestamp]:
            if difference <= tolerance:
                # Check if this frame is already the closest for another timestamp
                if frame_timestamp in frame_differences.values():
                    raise Exception(
                        f"Frame at {frame_timestamp}s is closest for more than one"
                        " timestamp."
                    )
                timestamp_frames[timestamp] = frame
                frame_differences[timestamp] = difference

    video_container.close()

    logger.debug(f"frame_differences=\n{pformat(frame_differences)}")
    invalid_frame_differences = {
        timestamp: difference
        for timestamp, difference in frame_differences.items()
        if difference > tolerance
    }
    logger.info(f"invalid_frame_differences=\n{pformat(invalid_frame_differences)}")

    # Check if all timestamps have been matched
    for timestamp, frame in timestamp_frames.items():
        if frame is None:
            raise Exception(
                f"No frame found within tolerance for timestamp {timestamp}s."
            )

    # Convert frames to PIL Image and return
    extracted_frames = [timestamp_frames[t].to_image() for t in timestamps]

    return extracted_frames
