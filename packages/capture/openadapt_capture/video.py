"""Video capture and frame extraction using PyAV.

This module provides video recording capabilities using libx264 encoding,
following OpenAdapt's proven implementation. Includes both a VideoWriter class
and legacy functional API (initialize/write/finalize) copied from legacy OpenAdapt.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

import av
from loguru import logger

from openadapt_capture import utils
from openadapt_capture.config import config

if TYPE_CHECKING:
    from PIL import Image


# =============================================================================
# Video Writer
# =============================================================================


class VideoWriter:
    """H.264 video writer using PyAV.

    Writes frames to an MP4 file with H.264 encoding for maximum compatibility
    and efficient compression.

    Usage:
        writer = VideoWriter("output.mp4", width=1920, height=1080)
        writer.write_frame(image, timestamp)
        writer.close()

    Or as context manager:
        with VideoWriter("output.mp4", width=1920, height=1080) as writer:
            writer.write_frame(image, timestamp)
    """

    def __init__(
        self,
        output_path: str | Path,
        width: int,
        height: int,
        fps: int = 24,
        codec: str = "libx264",
        pix_fmt: str = "yuv444p",
        crf: int = 0,
        preset: str = "veryslow",
    ) -> None:
        """Initialize video writer.

        Args:
            output_path: Path to output MP4 file.
            width: Video width in pixels.
            height: Video height in pixels.
            fps: Frames per second (default 24).
            codec: Video codec (default libx264).
            pix_fmt: Pixel format (default yuv444p for full color).
            crf: Constant Rate Factor, 0 for lossless (default 0).
            preset: Encoding preset (default veryslow for max compression).
        """

        self.output_path = Path(output_path)
        self.width = width
        self.height = height
        self.fps = fps
        self.codec = codec
        self.pix_fmt = pix_fmt
        self.crf = crf
        self.preset = preset

        self._container = None
        self._stream = None
        self._start_time: float | None = None
        self._last_pts: int = -1
        self._last_frame: "Image.Image" | None = None
        self._last_frame_timestamp: float | None = None
        self._lock = threading.Lock()

    def _init_stream(self) -> None:
        """Initialize the video stream."""
        self._container = av.open(str(self.output_path), mode="w")
        self._stream = self._container.add_stream(self.codec, rate=self.fps)
        self._stream.width = self.width
        self._stream.height = self.height
        self._stream.pix_fmt = self.pix_fmt
        self._stream.options = {"crf": str(self.crf), "preset": self.preset}

    @property
    def start_time(self) -> float | None:
        """Get the start time of the video."""
        return self._start_time

    @property
    def is_open(self) -> bool:
        """Check if writer is open."""
        return self._container is not None

    def write_frame(
        self,
        image: "Image.Image",
        timestamp: float,
        force_key_frame: bool = False,
    ) -> None:
        """Write a frame to the video.

        Args:
            image: PIL Image to write.
            timestamp: Unix timestamp of the frame.
            force_key_frame: Force this frame to be a key frame.
        """
        with self._lock:
            if self._container is None:
                self._init_stream()
                self._start_time = timestamp

            # Convert PIL Image to AVFrame
            av_frame = av.VideoFrame.from_image(image)

            # Force key frame if requested
            if force_key_frame:
                av_frame.pict_type = av.video.frame.PictureType.I

            # Calculate PTS based on elapsed time
            time_diff = timestamp - self._start_time
            pts = int(time_diff * float(Fraction(self._stream.average_rate)))

            # Ensure monotonically increasing PTS
            if pts <= self._last_pts:
                pts = self._last_pts + 1

            av_frame.pts = pts
            self._last_pts = pts

            # Encode and write
            for packet in self._stream.encode(av_frame):
                packet.pts = pts
                self._container.mux(packet)

            # Track last frame for finalization
            self._last_frame = image
            self._last_frame_timestamp = timestamp

    def close(self) -> None:
        """Close the video writer and finalize the file.

        This method handles the GIL deadlock issue by closing in a separate thread.
        """
        with self._lock:
            if self._container is None:
                return

            # Write a final key frame to ensure clean ending
            if self._last_frame is not None and self._last_frame_timestamp is not None:
                av_frame = av.VideoFrame.from_image(self._last_frame)
                # pict_type 1 = I-frame (key frame)
                av_frame.pict_type = av.video.frame.PictureType.I

                time_diff = self._last_frame_timestamp - self._start_time
                pts = int(time_diff * float(Fraction(self._stream.average_rate)))
                if pts <= self._last_pts:
                    pts = self._last_pts + 1
                av_frame.pts = pts

                for packet in self._stream.encode(av_frame):
                    packet.pts = pts
                    self._container.mux(packet)

            # Flush the stream
            for packet in self._stream.encode():
                self._container.mux(packet)

            # Close in separate thread to avoid GIL deadlock
            # https://github.com/PyAV-Org/PyAV/issues/1053
            container = self._container

            def close_container() -> None:
                container.close()

            close_thread = threading.Thread(target=close_container)
            close_thread.start()
            close_thread.join()

            self._container = None
            self._stream = None

    def __enter__(self) -> "VideoWriter":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()


# =============================================================================
# Legacy Functional API (copied from legacy OpenAdapt video.py)
# =============================================================================


def get_video_file_path(recording_timestamp: float, video_dir: str = None) -> str:
    """Generates a file path for a video recording based on a timestamp.

    Args:
        recording_timestamp (float): The timestamp of the recording.
        video_dir (str): Directory for video files. If None, uses capture dir.

    Returns:
        str: The generated file name for the video recording.
    """
    if video_dir is None:
        video_dir = os.path.join(os.getcwd(), "video")
    os.makedirs(video_dir, exist_ok=True)
    return os.path.join(
        video_dir, f"oa_recording-{recording_timestamp}.mp4"
    )


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
    screenshot: "Image.Image",
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
        av_frame.pict_type = av.video.frame.PictureType.I

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
    last_frame: "Image.Image",
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
    temp_file = None
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


# =============================================================================
# Frame Extraction
# =============================================================================


def extract_frames(
    video_path: str | Path,
    timestamps: list[float],
    tolerance: float = 0.1,
) -> list["Image.Image"]:
    """Extract frames from a video at specified timestamps.

    Args:
        video_path: Path to the video file.
        timestamps: List of timestamps (in seconds) to extract.
        tolerance: Maximum difference between requested and actual frame time.

    Returns:
        List of PIL Images at the requested timestamps.

    Raises:
        ValueError: If no frame found within tolerance for a timestamp.
    """

    video_container = av.open(str(video_path))
    video_stream = video_container.streams.video[0]

    # Storage for matched frames
    frame_by_timestamp: dict[float, "Image.Image" | None] = {t: None for t in timestamps}
    frame_differences: dict[float, float] = {t: float("inf") for t in timestamps}

    # Convert PTS to seconds
    time_base = float(video_stream.time_base)

    for frame in video_container.decode(video_stream):
        frame_timestamp = frame.pts * time_base

        for target_timestamp in timestamps:
            difference = abs(frame_timestamp - target_timestamp)
            if difference <= tolerance and difference < frame_differences[target_timestamp]:
                frame_by_timestamp[target_timestamp] = frame.to_image()
                frame_differences[target_timestamp] = difference

    video_container.close()

    # Check for missing frames
    missing = [t for t, frame in frame_by_timestamp.items() if frame is None]
    if missing:
        raise ValueError(f"No frame within tolerance for timestamps: {missing}")

    # Return in same order as input
    return [frame_by_timestamp[t] for t in timestamps]


def extract_frame(
    video_path: str | Path,
    timestamp: float,
    tolerance: float = 0.1,
) -> "Image.Image":
    """Extract a single frame from a video.

    Args:
        video_path: Path to the video file.
        timestamp: Timestamp (in seconds) to extract.
        tolerance: Maximum difference between requested and actual frame time.

    Returns:
        PIL Image at the requested timestamp.
    """
    return extract_frames(video_path, [timestamp], tolerance)[0]


def get_video_info(video_path: str | Path) -> dict:
    """Get information about a video file.

    Args:
        video_path: Path to the video file.

    Returns:
        Dictionary with video information (duration, width, height, fps, etc).
    """

    video_container = av.open(str(video_path))
    video_stream = video_container.streams.video[0]

    info = {
        "duration": float(video_stream.duration * video_stream.time_base)
        if video_stream.duration
        else None,
        "width": video_stream.width,
        "height": video_stream.height,
        "fps": float(video_stream.average_rate) if video_stream.average_rate else None,
        "codec": video_stream.codec_context.codec.name,
        "frames": video_stream.frames,
    }

    video_container.close()
    return info


# =============================================================================
# Chunked Video Writer (for long captures)
# =============================================================================


class ChunkedVideoWriter:
    """Video writer that automatically chunks output into segments.

    For long captures (hours/days), this splits the video into manageable
    segments to avoid huge files and enable recovery from crashes.

    Usage:
        writer = ChunkedVideoWriter(
            output_dir="capture_abc123/video",
            width=1920, height=1080,
            chunk_duration=600,  # 10 minutes per chunk
        )
        writer.write_frame(image, timestamp)
        writer.close()
    """

    def __init__(
        self,
        output_dir: str | Path,
        width: int,
        height: int,
        chunk_duration: float = 600.0,  # 10 minutes
        fps: int = 24,
        codec: str = "libx264",
        pix_fmt: str = "yuv444p",
        crf: int = 0,
        preset: str = "veryslow",
    ) -> None:
        """Initialize chunked video writer.

        Args:
            output_dir: Directory for video chunks.
            width: Video width in pixels.
            height: Video height in pixels.
            chunk_duration: Duration of each chunk in seconds.
            fps: Frames per second.
            codec: Video codec.
            pix_fmt: Pixel format.
            crf: Constant Rate Factor.
            preset: Encoding preset.
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.width = width
        self.height = height
        self.chunk_duration = chunk_duration
        self.fps = fps
        self.codec = codec
        self.pix_fmt = pix_fmt
        self.crf = crf
        self.preset = preset

        self._current_writer: VideoWriter | None = None
        self._chunk_index = 0
        self._chunk_start_time: float | None = None
        self._start_time: float | None = None
        self._lock = threading.Lock()

    @property
    def start_time(self) -> float | None:
        """Get the start time of the recording."""
        return self._start_time

    @property
    def chunk_paths(self) -> list[Path]:
        """Get list of all chunk file paths."""
        return sorted(self.output_dir.glob("chunk_*.mp4"))

    def _get_chunk_path(self, index: int) -> Path:
        """Get path for a chunk by index."""
        return self.output_dir / f"chunk_{index:04d}.mp4"

    def _start_new_chunk(self, timestamp: float) -> None:
        """Start a new video chunk."""
        if self._current_writer is not None:
            self._current_writer.close()

        chunk_path = self._get_chunk_path(self._chunk_index)
        self._current_writer = VideoWriter(
            chunk_path,
            width=self.width,
            height=self.height,
            fps=self.fps,
            codec=self.codec,
            pix_fmt=self.pix_fmt,
            crf=self.crf,
            preset=self.preset,
        )
        self._chunk_start_time = timestamp
        self._chunk_index += 1

    def write_frame(
        self,
        image: "Image.Image",
        timestamp: float,
        force_key_frame: bool = False,
    ) -> None:
        """Write a frame, automatically starting new chunks as needed.

        Args:
            image: PIL Image to write.
            timestamp: Unix timestamp of the frame.
            force_key_frame: Force this frame to be a key frame.
        """
        with self._lock:
            if self._start_time is None:
                self._start_time = timestamp

            # Check if we need a new chunk
            needs_new_chunk = (
                self._current_writer is None
                or (
                    self._chunk_start_time is not None
                    and timestamp - self._chunk_start_time >= self.chunk_duration
                )
            )

            if needs_new_chunk:
                self._start_new_chunk(timestamp)
                force_key_frame = True  # First frame of chunk should be key frame

            self._current_writer.write_frame(image, timestamp, force_key_frame)

    def close(self) -> None:
        """Close the current chunk and finalize."""
        with self._lock:
            if self._current_writer is not None:
                self._current_writer.close()
                self._current_writer = None

    def __enter__(self) -> "ChunkedVideoWriter":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()
