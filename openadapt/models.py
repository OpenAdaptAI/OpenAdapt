"""This module defines the models used in the OpenAdapt system."""

from collections import OrderedDict
from typing import Type
import io

from loguru import logger
from oa_pynput import keyboard
from PIL import Image, ImageChops
import numpy as np
import sqlalchemy as sa

from openadapt import window
from openadapt.config import config
from openadapt.db import db


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


class Recording(db.Base):
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

    action_events = sa.orm.relationship(
        "ActionEvent",
        back_populates="recording",
        order_by="ActionEvent.timestamp",
    )
    screenshots = sa.orm.relationship(
        "Screenshot",
        back_populates="recording",
        order_by="Screenshot.timestamp",
    )
    window_events = sa.orm.relationship(
        "WindowEvent",
        back_populates="recording",
        order_by="WindowEvent.timestamp",
    )

    _processed_action_events = None

    @property
    def processed_action_events(self) -> list:
        """Get the processed action events for the recording."""
        from openadapt import events

        if not self._processed_action_events:
            self._processed_action_events = events.get_events(self)
        return self._processed_action_events


class ActionEvent(db.Base):
    """Class representing an action event in the database."""

    __tablename__ = "action_event"

    _segment_description_separator = ";"

    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String)
    timestamp = sa.Column(ForceFloat)
    recording_timestamp = sa.Column(sa.ForeignKey("recording.timestamp"))
    screenshot_timestamp = sa.Column(sa.ForeignKey("screenshot.timestamp"))
    window_event_timestamp = sa.Column(sa.ForeignKey("window_event.timestamp"))
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

    def __new__(cls, *args: tuple, **kwargs: dict) -> "ActionEvent":
        """Create a new instance; also called when loading from db."""
        instance = super(ActionEvent, cls).__new__(cls)
        instance.reducer_names = set()
        return instance

    def __init__(self, **kwargs: dict) -> None:
        """Initialize attributes first, then properties."""
        super().__init__()

        # Temporary dictionary to hold property values
        properties = {}

        # Introspect to determine properties
        prop_keys = [
            name
            for name, obj in type(self).__dict__.items()
            if isinstance(obj, property)
        ]

        # Set non-property attributes first
        for key, value in kwargs.items():
            if key in prop_keys:
                properties[key] = value
            else:
                setattr(self, key, value)

        # Now handle properties
        for key, value in properties.items():
            setattr(self, key, value)

    @property
    def available_segment_descriptions(self) -> list[str]:
        """Gets the available segment descriptions."""
        if self._available_segment_descriptions:
            return self._available_segment_descriptions.split(
                self._segment_description_separator
            )
        else:
            return []

    @available_segment_descriptions.setter
    def available_segment_descriptions(self, value: list[str]) -> None:
        """Sets the available segment descriptions."""
        self._available_segment_descriptions = self._segment_description_separator.join(
            value
        )

    children = sa.orm.relationship("ActionEvent")
    # TODO: replacing the above line with the following two results in an error:
    #     AttributeError: 'list' object has no attribute '_sa_instance_state'
    # children = sa.orm.relationship(
    #     "ActionEvent", remote_side=[id], back_populates="parent"
    # )
    # parent = sa.orm.relationship(
    #     "ActionEvent", remote_side=[parent_id], back_populates="children"
    # )  # noqa: E501

    recording = sa.orm.relationship("Recording", back_populates="action_events")
    screenshot = sa.orm.relationship("Screenshot", back_populates="action_event")
    window_event = sa.orm.relationship("WindowEvent", back_populates="action_events")

    # TODO: playback_timestamp / original_timestamp

    def _key(
        self, key_name: str, key_char: str, key_vk: str
    ) -> keyboard.Key | keyboard.KeyCode | str | None:
        """Helper method to determine the key attribute based on available data."""
        if key_name:
            key = keyboard.Key[key_name]
        elif key_char:
            key = key_char
        elif key_vk:
            # TODO: verify this is correct
            key = keyboard.KeyCode.from_vk(int(key_vk))
        else:
            key = None
        return key

    @property
    def key(self) -> keyboard.Key | keyboard.KeyCode | str | None:
        """Get the key associated with the action event."""
        logger.trace(f"{self.name=} {self.key_name=} {self.key_char=} {self.key_vk=}")
        return self._key(
            self.key_name,
            self.key_char,
            self.key_vk,
        )

    @property
    def canonical_key(self) -> keyboard.Key | keyboard.KeyCode | str | None:
        """Get the canonical key associated with the action event."""
        logger.trace(
            f"{self.name=} "
            f"{self.canonical_key_name=} "
            f"{self.canonical_key_char=} "
            f"{self.canonical_key_vk=}"
        )
        return self._key(
            self.canonical_key_name,
            self.canonical_key_char,
            self.canonical_key_vk,
        )

    def _text(self, canonical: bool = False) -> str | None:
        """Helper method to generate the text representation of the action event."""
        sep = config.ACTION_TEXT_SEP
        name_prefix = config.ACTION_TEXT_NAME_PREFIX
        name_suffix = config.ACTION_TEXT_NAME_SUFFIX
        if canonical:
            key_attr = self.canonical_key
            key_name_attr = self.canonical_key_name
        else:
            key_attr = self.key
            key_name_attr = self.key_name
        if self.children:
            parts = [
                child._text(canonical=canonical)
                for child in self.children
                if child.name == "press"
            ]
            if any(parts):
                # str is necessary for canonical=True named keys
                # e.g. canonical(<esc>) == <53> (darwin)
                text = sep.join([str(part) for part in parts])
            else:
                text = None
        else:
            if key_name_attr:
                text = f"{name_prefix}{key_attr}{name_suffix}".replace(
                    "Key.",
                    "",
                )
            else:
                text = key_attr
        return text

    @property
    def text(self) -> str:
        """Get the text representation of the action event."""
        return self._text()

    @text.setter
    def text(self, value: str) -> None:
        """Validate the text property. Useful for ActionModel(**action_dict)."""
        if not value == self.text:
            logger.warning(f"{value=} did not match {self.text=}")

    @property
    def canonical_text(self) -> str:
        """Get the canonical text representation of the action event."""
        return self._text(canonical=True)

    @canonical_text.setter
    def canonical_text(self, value: str) -> None:
        """Validate canonical_text property. Useful for ActionModel(**action_dict)."""
        if not value == self.canonical_text:
            logger.warning(f"{value=} did not match {self.canonical_text=}")

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
            "text",
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

    @classmethod
    def from_children(cls: Type["ActionEvent"], children_dicts: list) -> "ActionEvent":
        """Create an ActionEvent instance from a list of child event dictionaries.

        Args:
            children_dicts (list): List of dictionaries representing child events.

        Returns:
            ActionEvent: An instance of ActionEvent with the specified child events.
        """
        children = [ActionEvent(**child_dict) for child_dict in children_dicts]
        return ActionEvent(children=children)

    @classmethod
    def from_dict(
        cls: Type["ActionEvent"],
        action_dict: dict,
    ) -> "ActionEvent":
        """Get an ActionEvent from a dict."""
        sep = config.ACTION_TEXT_SEP
        name_prefix = config.ACTION_TEXT_NAME_PREFIX
        name_suffix = config.ACTION_TEXT_NAME_SUFFIX
        children = []
        release_events = []
        if "text" in action_dict:
            # Splitting actions based on whether they are special keys or characters
            if action_dict["text"].startswith(name_prefix) and action_dict[
                "text"
            ].endswith(name_suffix):
                # Handling special keys
                sep = "".join([name_suffix, sep, name_prefix])
                prefix_len = len(name_prefix)
                suffix_len = len(name_suffix)
                key_names = action_dict["text"][prefix_len:-suffix_len].split(sep)
                canonical_key_names = action_dict["canonical_text"][
                    prefix_len:-suffix_len
                ].split(sep)
                for key_name, canonical_key_name in zip(key_names, canonical_key_names):
                    press, release = cls._create_key_events(
                        key_name, canonical_key_name
                    )
                    children.append(press)
                    release_events.append(
                        release
                    )  # Collect release events to append in reverse order later
            else:
                # Handling regular character sequences
                sep_len = len(sep)
                for key_char in action_dict["text"][:: sep_len + 1]:
                    # Press and release each character one after another
                    press, release = cls._create_key_events(key_char=key_char)
                    children.append(press)
                    children.append(release)
            children += release_events[::-1]
        rval = ActionEvent(**action_dict, children=children)
        return rval

    @classmethod
    def _create_key_events(
        cls: Type["ActionEvent"],
        key_name: str | None = None,
        canonical_key_name: str | None = None,
        key_char: str | None = None,
        canonical_key_char: str | None = None,
    ) -> list["ActionEvent"]:
        # This helper function creates press and release events for a given key_name
        # TODO: remove canonical?
        press_event = cls(
            name="press",
            key_name=key_name,
            # canonical_key_name=canonical_key_name,
            key_char=key_char,
            # canonical_key_char=canonical_key_char,
        )
        release_event = cls(
            name="release",
            key_name=key_name,
            # canonical_key_name=canonical_key_name,
            key_char=key_char,
            # canonical_key_char=canonical_key_char,
        )
        return press_event, release_event


class WindowEvent(db.Base):
    """Class representing a window event in the database."""

    __tablename__ = "window_event"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(sa.ForeignKey("recording.timestamp"))
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

    @classmethod
    def get_active_window_event(cls: "WindowEvent") -> "WindowEvent":
        """Get the active window event."""
        return WindowEvent(**window.get_active_window_data())


class FrameCache:
    """Provide a caching mechanism for video frames to minimize IO operations.

    This class maintains a nested dictionary to store video frames by video
    filename and their respective timestamps. It offers functionalities to get
    frames from the cache or load them if they are not available in the cache.

    Attributes:
        capacity (int): The maximum number of frames that can be stored per video.
            If set to 0, the capacity is considered infinite.
        frames (dict): A nested dictionary to store frames by video filename and
            timestamp.
    """

    ENABLED = True

    def __init__(self, capacity: int = 0) -> None:
        """Initialize a new FrameCache instance with specified capacity.

        Args:
            capacity (int): The maximum number of frames to cache per video file.
        """
        self.capacity = capacity
        # Nested dictionary to store frames by video filename and timestamp
        self.frames = {}

    def get_frame(self, video_file_path: str, timestamp: float) -> Image.Image:
        """Retrieve a frame by video file path and timestamp from the cache.

        If the frame is not available in the cache, it logs a warning and loads
        the frame into the cache before returning it.

        Args:
            video_file_path (str): The path to the video file.
            timestamp (float): The timestamp of the frame in the video.

        Returns:
            Image.Image: The requested video frame.
        """
        # Check if the frame is cached
        if not (
            video_file_path in self.frames and timestamp in self.frames[video_file_path]
        ):
            # Issue a warning if the frame needs to be loaded without prior caching
            logger.warning(
                f"Frame at timestamp {timestamp} from {video_file_path} was not "
                "pre-cached. Loading it now, but consider using cache_frames to load "
                "batches."
            )
            # Load the frame since it wasn't cached
            self.cache_frames(video_file_path, [timestamp])
        return self.frames[video_file_path][timestamp]

    def cache_frames(self, video_file_path: str, timestamps: list[float]) -> None:
        """Cache multiple frames from a video file at specified timestamps.

        This method checks which frames are not already cached and loads them.
        It respects the capacity limit of the cache, potentially evicting the oldest
        cached frame to make room for new ones if the capacity is exceeded.

        Args:
            video_file_path (str): The path to the video file.
            timestamps (list[float]): A list of timestamps of frames to cache.

        Returns:
            None
        """
        # avoid circular import
        from openadapt import video

        # Ensure the dictionary for this video file is initialized
        if video_file_path not in self.frames:
            self.frames[video_file_path] = OrderedDict()

        # Load only the frames that have not been loaded yet
        uncached_timestamps = [
            t for t in timestamps if t not in self.frames[video_file_path]
        ]
        frames = video.extract_frames(video_file_path, uncached_timestamps)
        # Add loaded frames to cache, respecting capacity if not infinite
        for timestamp, frame in zip(uncached_timestamps, frames):
            if self.capacity > 0 and len(self.frames[video_file_path]) >= self.capacity:
                # Remove oldest frame if capacity is exceeded
                self.frames[video_file_path].popitem(last=False)
            self.frames[video_file_path][timestamp] = frame


# for use in Screenshot.image
frame_cache = FrameCache()


class Screenshot(db.Base):
    """Class representing a screenshot in the database."""

    __tablename__ = "screenshot"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(sa.ForeignKey("recording.timestamp"))
    timestamp = sa.Column(ForceFloat)
    png_data = sa.Column(sa.LargeBinary)
    png_diff_data = sa.Column(sa.LargeBinary, nullable=True)
    png_diff_mask_data = sa.Column(sa.LargeBinary, nullable=True)
    # cropped_png_data = sa.Column(sa.LargeBinary, nullable=True)

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
        self.initialize_instance_attributes()
        self._image = image

    @sa.orm.reconstructor
    def initialize_instance_attributes(self) -> None:
        """Initialize attributes for both new and loaded objects."""
        # TODO: convert to png_data on save
        # TODO: replace prev with prev_timestamp?
        self.prev = None
        self._image = None
        self._cropped_image = None
        self._diff = None
        self._diff_mask = None
        self._base64 = None

    @property
    def image(self) -> Image.Image:
        """Get the image associated with the screenshot."""
        if not self._image:
            if self.png_data:
                self._image = self.convert_binary_to_png(self.png_data)
            else:
                # avoid circular import
                from openadapt import video

                video_file_path = video.get_video_file_path(self.recording_timestamp)
                if FrameCache.ENABLED:
                    if video_file_path not in frame_cache.frames:
                        screenshot_timestamps = [
                            screenshot.timestamp - self.recording.video_start_time
                            for screenshot in self.recording.screenshots
                        ]
                        frame_cache.cache_frames(video_file_path, screenshot_timestamps)
                    self._image = frame_cache.get_frame(
                        video_file_path,
                        self.timestamp - self.recording.video_start_time,
                    )
                else:
                    self._image = video.extract_frames(
                        video_file_path,
                        [self.timestamp - self.recording.video_start_time],
                    )[0]
        return self._image

    @property
    def cropped_image(self) -> Image.Image:
        """Return screenshot image cropped to corresponding action's active window."""
        if not self._cropped_image:
            # if events have been merged, the last event will be the parent, e.g.
            #   ipdb> [(action.name, action.timestamp) for action in self.action_event]
            #   [('move', 1714142176.1630979), ('click', 1714142174.4848516),
            #   ('singleclick', 1714142174.4537418)]
            # TODO: verify (e.g. assert)
            # TODO: rename action_event -> action_events?
            action_event = self.action_event[-1]
            self._cropped_image = self.crop_active_window(action_event)
            # TODO: save?
            # self.cropped_png_data = self.convert_png_to_binary(self._cropped_image)
        return self._cropped_image

    @property
    def base64(self) -> str:
        """Return data URI of JPEG encoded base64."""
        if not self._base64:
            self._base64 = utils.image2utf8(self.image)
        return self._base64

    @property
    def diff(self) -> Image.Image:
        """Get the difference between the current and previous screenshot."""
        if self.png_diff_data:
            return self.convert_binary_to_png(self.png_diff_data)

        assert self.prev, "Attempted to compute diff before setting prev"
        self._diff = ImageChops.difference(self.image, self.prev.image)
        return self._diff

    @property
    def diff_mask(self) -> Image.Image:
        """Get the difference mask between the current and previous screenshot."""
        if self.png_diff_mask_data:
            return self.convert_binary_to_png(self.png_diff_mask_data)

        if self.diff:
            self._diff_mask = self.diff.convert("1")
        return self._diff_mask

    @property
    def array(self) -> np.ndarray:
        """Get the NumPy array representation of the image."""
        return np.array(self.image)

    @classmethod
    def take_screenshot(cls: "Screenshot") -> "Screenshot":
        """Capture a screenshot."""
        image = utils.take_screenshot()
        screenshot = Screenshot(image=image)
        return screenshot

    def crop_active_window(self, action_event: ActionEvent) -> None:
        """Crop the screenshot to the active window defined by the action event."""
        window_event = action_event.window_event
        width_ratio, height_ratio = utils.get_scale_ratios(action_event)

        x0 = window_event.left * width_ratio
        y0 = window_event.top * height_ratio
        x1 = x0 + window_event.width * width_ratio
        y1 = y0 + window_event.height * height_ratio

        box = (x0, y0, x1, y1)
        cropped_image = self._image.crop(box)
        return cropped_image

    def convert_binary_to_png(self, image_binary: bytes) -> Image.Image:
        """Convert a binary image to a PNG image.

        Args:
            image_binary (bytes): The binary image data.

        Returns:
            Image: The PNG image.
        """
        buffer = io.BytesIO(image_binary)
        return Image.open(buffer)

    def convert_png_to_binary(self, image: Image.Image) -> bytes:
        """Convert a PNG image to binary image data.

        Args:
            image (Image): The PNG image.

        Returns:
            bytes: The binary image data.
        """
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


class PerformanceStat(db.Base):
    """Class representing a performance statistic in the database."""

    __tablename__ = "performance_stat"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(sa.Integer)
    event_type = sa.Column(sa.String)
    start_time = sa.Column(sa.Integer)
    end_time = sa.Column(sa.Integer)
    window_id = sa.Column(sa.String)


class MemoryStat(db.Base):
    """Class representing a memory usage statistic in the database."""

    __tablename__ = "memory_stat"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(sa.Integer)
    memory_usage_bytes = sa.Column(ForceFloat)
    timestamp = sa.Column(ForceFloat)


# avoid circular import
from openadapt import utils  # noqa
