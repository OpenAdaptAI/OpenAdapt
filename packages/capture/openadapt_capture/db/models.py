"""SQLAlchemy models for openadapt-capture.

Copied verbatim from legacy OpenAdapt models.py.
Only import paths are changed; column definitions and relationships are identical.
"""

import io

import sqlalchemy as sa
from PIL import Image

from openadapt_capture.db import Base


# https://groups.google.com/g/sqlalchemy/c/wlr7sShU6-k
class ForceFloat(sa.TypeDecorator):
    """Custom SQLAlchemy type decorator for floating-point numbers."""

    impl = sa.Numeric(10, 2, asdecimal=False)
    cache_ok = True

    def process_result_value(
        self,
        value: int | float | str | None,
        dialect: str,
    ) -> float | None:
        """Convert the result value to float."""
        if value is not None:
            value = float(value)
        return value


class Recording(Base):
    """Class representing a recording in the database."""

    __tablename__ = "recording"

    id = sa.Column(sa.Integer, primary_key=True)
    timestamp = sa.Column(ForceFloat)
    monitor_width = sa.Column(sa.Integer)
    monitor_height = sa.Column(sa.Integer)
    double_click_interval_seconds = sa.Column(sa.Numeric(asdecimal=False))
    double_click_distance_pixels = sa.Column(sa.Numeric(asdecimal=False))
    platform = sa.Column(sa.String)
    task_description = sa.Column(sa.String)
    video_start_time = sa.Column(ForceFloat)
    config = sa.Column(sa.JSON)

    original_recording_id = sa.Column(sa.ForeignKey("recording.id"))
    original_recording = sa.orm.relationship(
        "Recording",
        back_populates="copies",
        remote_side=[id],
    )
    copies = sa.orm.relationship(
        "Recording", back_populates="original_recording", cascade="all, delete-orphan"
    )

    action_events = sa.orm.relationship(
        "ActionEvent",
        back_populates="recording",
        order_by="ActionEvent.timestamp",
        cascade="all, delete-orphan",
    )
    screenshots = sa.orm.relationship(
        "Screenshot",
        back_populates="recording",
        order_by="Screenshot.timestamp",
        cascade="all, delete-orphan",
    )
    window_events = sa.orm.relationship(
        "WindowEvent",
        back_populates="recording",
        order_by="WindowEvent.timestamp",
        cascade="all, delete-orphan",
    )
    browser_events = sa.orm.relationship(
        "BrowserEvent",
        back_populates="recording",
        order_by="BrowserEvent.timestamp",
        cascade="all, delete-orphan",
    )
    audio_info = sa.orm.relationship(
        "AudioInfo", back_populates="recording", cascade="all, delete-orphan"
    )


class ActionEvent(Base):
    """Class representing an action event in the database."""

    __tablename__ = "action_event"

    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String)
    timestamp = sa.Column(ForceFloat)
    recording_timestamp = sa.Column(ForceFloat)
    recording_id = sa.Column(sa.ForeignKey("recording.id"))
    screenshot_timestamp = sa.Column(ForceFloat)
    screenshot_id = sa.Column(sa.ForeignKey("screenshot.id"))
    window_event_timestamp = sa.Column(ForceFloat)
    window_event_id = sa.Column(sa.ForeignKey("window_event.id"))
    browser_event_timestamp = sa.Column(ForceFloat)
    browser_event_id = sa.Column(sa.ForeignKey("browser_event.id"))
    mouse_x = sa.Column(sa.Numeric(asdecimal=False))
    mouse_y = sa.Column(sa.Numeric(asdecimal=False))
    mouse_dx = sa.Column(sa.Numeric(asdecimal=False))
    mouse_dy = sa.Column(sa.Numeric(asdecimal=False))
    active_segment_description = sa.Column(sa.String)
    _available_segment_descriptions = sa.Column(
        "available_segment_descriptions",
        sa.String,
    )
    mouse_button_name = sa.Column(sa.String)
    mouse_pressed = sa.Column(sa.Boolean)
    key_name = sa.Column(sa.String)
    key_char = sa.Column(sa.String)
    key_vk = sa.Column(sa.String)
    canonical_key_name = sa.Column(sa.String)
    canonical_key_char = sa.Column(sa.String)
    canonical_key_vk = sa.Column(sa.String)
    parent_id = sa.Column(sa.Integer, sa.ForeignKey("action_event.id"))
    element_state = sa.Column(sa.JSON)
    disabled = sa.Column(sa.Boolean, default=False)

    children = sa.orm.relationship("ActionEvent")

    recording = sa.orm.relationship("Recording", back_populates="action_events")
    screenshot = sa.orm.relationship("Screenshot", back_populates="action_event")
    window_event = sa.orm.relationship("WindowEvent", back_populates="action_events")
    browser_event = sa.orm.relationship("BrowserEvent", back_populates="action_events")

    def __str__(self) -> str:
        """Return a string representation of the action event."""
        attr_names = [
            "name",
            "mouse_x",
            "mouse_y",
            "mouse_dx",
            "mouse_dy",
            "mouse_button_name",
            "mouse_pressed",
            "key_name",
            "key_char",
            "element_state",
        ]
        attrs = [getattr(self, attr_name) for attr_name in attr_names]
        attrs = [int(attr) if isinstance(attr, float) else attr for attr in attrs]
        attrs = [
            f"{attr_name}=`{attr}`"
            for attr_name, attr in zip(attr_names, attrs)
            if attr
        ]
        rval = " ".join(attrs)
        return rval


class WindowEvent(Base):
    """Class representing a window event in the database."""

    __tablename__ = "window_event"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(ForceFloat)
    recording_id = sa.Column(sa.ForeignKey("recording.id"))
    timestamp = sa.Column(ForceFloat)
    state = sa.Column(sa.JSON)
    title = sa.Column(sa.String)
    left = sa.Column(sa.Integer)
    top = sa.Column(sa.Integer)
    width = sa.Column(sa.Integer)
    height = sa.Column(sa.Integer)
    window_id = sa.Column(sa.String)

    recording = sa.orm.relationship("Recording", back_populates="window_events")
    action_events = sa.orm.relationship("ActionEvent", back_populates="window_event")


class BrowserEvent(Base):
    """Class representing a browser event in the database."""

    __tablename__ = "browser_event"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(ForceFloat)
    recording_id = sa.Column(sa.ForeignKey("recording.id"))
    message = sa.Column(sa.JSON)
    timestamp = sa.Column(ForceFloat)

    recording = sa.orm.relationship("Recording", back_populates="browser_events")
    action_events = sa.orm.relationship("ActionEvent", back_populates="browser_event")


class Screenshot(Base):
    """Class representing a screenshot in the database."""

    __tablename__ = "screenshot"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(ForceFloat)
    recording_id = sa.Column(sa.ForeignKey("recording.id"))
    timestamp = sa.Column(ForceFloat)
    png_data = sa.Column(sa.LargeBinary)
    png_diff_data = sa.Column(sa.LargeBinary, nullable=True)
    png_diff_mask_data = sa.Column(sa.LargeBinary, nullable=True)

    recording = sa.orm.relationship("Recording", back_populates="screenshots")
    action_event = sa.orm.relationship("ActionEvent", back_populates="screenshot")

    def __init__(
        self,
        *args: tuple,
        image: Image.Image | None = None,
        **kwargs: dict,
    ) -> None:
        """Initialize."""
        super().__init__(*args, **kwargs)
        self._image = image

    @sa.orm.reconstructor
    def initialize_instance_attributes(self) -> None:
        """Initialize attributes for both new and loaded objects."""
        self.prev = None
        self._image = None

    @property
    def image(self) -> Image.Image:
        """Get the image associated with the screenshot."""
        if not self._image:
            if self.png_data:
                self._image = self.convert_binary_to_png(self.png_data)
        return self._image

    @classmethod
    def take_screenshot(cls) -> "Screenshot":
        """Capture a screenshot."""
        from openadapt_capture import utils

        image = utils.take_screenshot()
        screenshot = Screenshot(image=image)
        return screenshot

    def convert_binary_to_png(self, image_binary: bytes) -> Image.Image:
        """Convert a binary image to a PNG image."""
        buffer = io.BytesIO(image_binary)
        return Image.open(buffer)

    def convert_png_to_binary(self, image: Image.Image) -> bytes:
        """Convert a PNG image to binary image data."""
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


class AudioInfo(Base):
    """Class representing the audio from a recording in the database."""

    __tablename__ = "audio_info"

    id = sa.Column(sa.Integer, primary_key=True)
    timestamp = sa.Column(ForceFloat)
    flac_data = sa.Column(sa.LargeBinary)
    transcribed_text = sa.Column(sa.String)
    recording_timestamp = sa.Column(ForceFloat)
    recording_id = sa.Column(sa.ForeignKey("recording.id"))
    sample_rate = sa.Column(sa.Integer)
    words_with_timestamps = sa.Column(sa.Text)

    recording = sa.orm.relationship("Recording", back_populates="audio_info")


class PerformanceStat(Base):
    """Class representing a performance statistic in the database."""

    __tablename__ = "performance_stat"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(ForceFloat)
    recording_id = sa.Column(sa.ForeignKey("recording.id"))
    event_type = sa.Column(sa.String)
    start_time = sa.Column(sa.Integer)
    end_time = sa.Column(sa.Integer)
    window_id = sa.Column(sa.String)


class MemoryStat(Base):
    """Class representing a memory usage statistic in the database."""

    __tablename__ = "memory_stat"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(sa.Integer)
    recording_id = sa.Column(sa.ForeignKey("recording.id"))
    memory_usage_bytes = sa.Column(ForceFloat)
    timestamp = sa.Column(ForceFloat)
