"""Tests for video module."""

import tempfile
import time

import av
import pytest
from PIL import Image

from openadapt_capture import utils
from openadapt_capture.video import (
    initialize_video_writer,
    write_video_frame,
)


@pytest.fixture(autouse=True)
def _init_timestamp():
    """Ensure utils timestamp system is initialized."""
    utils.set_start_time(time.time())


class TestWriteVideoFrame:
    """Tests for write_video_frame."""

    def test_write_frame_basic(self):
        """Test writing a basic video frame."""
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            container, stream, start_ts = initialize_video_writer(
                f.name, 100, 100
            )
            img = Image.new("RGB", (100, 100), color="red")
            last_pts = write_video_frame(
                container, stream, img, start_ts + 0.1, start_ts, 0
            )
            assert last_pts > 0
            container.close()

    def test_write_frame_force_key_frame(self):
        """Test writing a video frame with force_key_frame=True."""
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            container, stream, start_ts = initialize_video_writer(
                f.name, 100, 100
            )
            img = Image.new("RGB", (100, 100), color="blue")
            last_pts = write_video_frame(
                container, stream, img, start_ts + 0.1, start_ts, 0,
                force_key_frame=True,
            )
            assert last_pts > 0
            container.close()

    def test_pict_type_enum(self):
        """Test that PictureType.I is valid for pict_type assignment."""
        frame = av.VideoFrame(100, 100, "rgb24")
        frame.pict_type = av.video.frame.PictureType.I
        assert frame.pict_type == av.video.frame.PictureType.I
