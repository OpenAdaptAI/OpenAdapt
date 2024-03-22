"""This module defines the models used in the OpenAdapt system."""

from typing import Union
import io

from loguru import logger
from oa_pynput import keyboard
from PIL import Image, ImageChops
import numpy as np
import sqlalchemy as sa

from openadapt import config, window
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
    ) -> Union[keyboard.Key, keyboard.KeyCode, str, None]:
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
    def key(self) -> Union[keyboard.Key, keyboard.KeyCode, str, None]:
        """Get the key associated with the action event."""
        logger.trace(f"{self.name=} {self.key_name=} {self.key_char=} {self.key_vk=}")
        return self._key(
            self.key_name,
            self.key_char,
            self.key_vk,
        )

    @property
    def canonical_key(self) -> Union[keyboard.Key, keyboard.KeyCode, str, None]:
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

    @property
    def canonical_text(self) -> str:
        """Get the canonical text representation of the action event."""
        return self._text(canonical=True)

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
    def from_children(cls: list, children_dicts: list) -> "ActionEvent":
        """Create an ActionEvent instance from a list of child event dictionaries.

        Args:
            children_dicts (list): List of dictionaries representing child events.

        Returns:
            ActionEvent: An instance of ActionEvent with the specified child events.

        """
        for child_dict in children_dicts:
            for key in child_dict:
                #if isinstance(getattr(type(ActionEvent), key), property):
                if key == "text":
                    # TODO: decompose into individual children
                    import ipdb; ipdb.set_trace()
                    foo = 1

        children = [ActionEvent(**child_dict) for child_dict in children_dicts]
        return ActionEvent(children=children)

    @classmethod
    def from_dict(cls, action_dict: dict) -> list['ActionEvent']:
        # TODO: handle both parent and children
        import ipdb; ipdb.set_trace()
        text_actions = parent['text'][1:-1].split('>-<')
        canonical_actions = parent['canonical_text'][1:-1].split('>-<')
        parent_id = None  # Set this according to your needs, possibly passed in the parent dict
        children = []

        for action, canonical_action in zip(text_actions, canonical_actions):
            # Assuming the action names 'press' and 'release' are adequate for the child events
            press_event = cls(
                name='press',
                key_name=action,
                canonical_key_name=action if not action.isdigit() else None,
                canonical_key_vk=canonical_action if action.isdigit() else None,
                parent_id=parent_id
            )
            release_event = cls(
                name='release',
                key_name=action,
                canonical_key_name=action if not action.isdigit() else None,
                canonical_key_vk=canonical_action if action.isdigit() else None,
                parent_id=parent_id
            )

            children.extend([press_event, release_event])

        return children

    def scale_to_screenshot_image(self):
        """
            mouse_x = sa.Column(sa.Numeric(asdecimal=False))
            mouse_y = sa.Column(sa.Numeric(asdecimal=False))
            mouse_dx = sa.Column(sa.Numeric(asdecimal=False))
            mouse_dy = sa.Column(sa.Numeric(asdecimal=False))

            x = action_event.mouse_x * width_ratio
            y = action_event.mouse_y * height_ratio
        """
        width_ratio, height_ratio = utils.get_scale_ratios(self)

        # TODO: return new ActionEvent
        return {
            "mouse_x": self.mouse_x * width_ratio,
            "mouse_y": self.mouse_y * height_ratio,
            "mouse_dx": self.mouse_dx * width_ratio,
            "mouse_dy": self.mouse_dy * height_ratio,
        }




class Screenshot(db.Base):
    """Class representing a screenshot in the database."""

    __tablename__ = "screenshot"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(sa.ForeignKey("recording.timestamp"))
    timestamp = sa.Column(ForceFloat)
    png_data = sa.Column(sa.LargeBinary)
    png_diff_data = sa.Column(sa.LargeBinary, nullable=True)
    png_diff_mask_data = sa.Column(sa.LargeBinary, nullable=True)

    recording = sa.orm.relationship("Recording", back_populates="screenshots")
    action_event = sa.orm.relationship("ActionEvent", back_populates="screenshot")

    # TODO: convert to png_data on save
    sct_img = None

    # TODO: replace prev with prev_timestamp?
    prev = None
    _image = None
    _image_history = []
    _diff = None
    _diff_mask = None
    _base64 = None

    @property
    def image(self) -> Image:
        """Get the image associated with the screenshot."""
        if not self._image:
            if self.sct_img:
                self._image = Image.frombytes(
                    "RGB",
                    self.sct_img.size,
                    self.sct_img.bgra,
                    "raw",
                    "BGRX",
                )
            else:
                self._image = self.convert_binary_to_png(self.png_data)
        return self._image

    @property
    def base64(self) -> str:
        """Return data URI of JPEG encoded base64"""
        if not self._base64:
            from openadapt import utils
            self._base64 = utils.image2utf8(self.image)
        return self._base64

    @property
    def diff(self) -> Image:
        """Get the difference between the current and previous screenshot."""
        if self.png_diff_data:
            return self.convert_binary_to_png(self.png_diff_data)

        assert self.prev, "Attempted to compute diff before setting prev"
        self._diff = ImageChops.difference(self.image, self.prev.image)
        return self._diff

    @property
    def diff_mask(self) -> Image:
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
        # avoid circular import
        from openadapt import utils

        sct_img = utils.take_screenshot()
        screenshot = Screenshot(sct_img=sct_img)
        return screenshot

    def crop_active_window(self, action_event: ActionEvent) -> None:
        """Crop the screenshot to the active window defined by the action event."""
        # avoid circular import
        from openadapt import utils

        window_event = action_event.window_event
        width_ratio, height_ratio = utils.get_scale_ratios(action_event)

        x0 = window_event.left * width_ratio
        y0 = window_event.top * height_ratio
        x1 = x0 + window_event.width * width_ratio
        y1 = y0 + window_event.height * height_ratio

        box = (x0, y0, x1, y1)
        self._image_history.append(self._image)
        self._image = self._image.crop(box)

    def convert_binary_to_png(self, image_binary: bytes) -> Image:
        """Convert a binary image to a PNG image.

        Args:
            image_binary (bytes): The binary image data.

        Returns:
            Image: The PNG image.
        """
        buffer = io.BytesIO(image_binary)
        return Image.open(buffer)

    def convert_png_to_binary(self, image: Image) -> bytes:
        """Convert a PNG image to binary image data.

        Args:
            image (Image): The PNG image.

        Returns:
            bytes: The binary image data.
        """
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


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
