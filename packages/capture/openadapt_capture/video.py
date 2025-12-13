"""Video capture and frame extraction using PyAV.

This module provides video recording capabilities using libx264 encoding,
following OpenAdapt's proven implementation.
"""

from __future__ import annotations

import threading
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

import av

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
