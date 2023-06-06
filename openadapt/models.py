import io

from loguru import logger
from pynput import keyboard
from PIL import Image, ImageChops
import numpy as np
import sqlalchemy as sa

from openadapt import db, utils, window


# https://groups.google.com/g/sqlalchemy/c/wlr7sShU6-k
class ForceFloat(sa.TypeDecorator):
    impl = sa.Numeric(10, 2, asdecimal=False)
    cache_ok = True

    def process_result_value(self, value, dialect):
        if value is not None:
            value = float(value)
        return value


class Recording(db.Base):
    __tablename__ = "recording"

    id = sa.Column(sa.Integer, primary_key=True)
    timestamp = sa.Column(ForceFloat)
    monitor_width = sa.Column(sa.Integer)
    monitor_height = sa.Column(sa.Integer)
    double_click_interval_seconds = sa.Column(sa.Numeric(asdecimal=False))
    double_click_distance_pixels = sa.Column(sa.Numeric(asdecimal=False))
    platform = sa.Column(sa.String)
    task_description = sa.Column(sa.String)

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
    def processed_action_events(self):
        from openadapt import events
        if not self._processed_action_events:
            self._processed_action_events = events.get_events(self)
        return self._processed_action_events



class ActionEvent(db.Base):
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
    #children = sa.orm.relationship("ActionEvent", remote_side=[id], back_populates="parent")
    #parent = sa.orm.relationship("ActionEvent", remote_side=[parent_id], back_populates="children")

    recording = sa.orm.relationship("Recording", back_populates="action_events")
    screenshot = sa.orm.relationship("Screenshot", back_populates="action_event")
    window_event = sa.orm.relationship("WindowEvent", back_populates="action_events")

    # TODO: playback_timestamp / original_timestamp

    def _key(self, key_name, key_char, key_vk):
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
    def key(self):
        logger.trace(
            f"{self.name=} {self.key_name=} {self.key_char=} {self.key_vk=}"
        )
        return self._key(
            self.key_name,
            self.key_char,
            self.key_vk,
        )

    @property
    def canonical_key(self):
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

    def _text(self, canonical=False):
        sep = self._text_sep
        name_prefix = self._text_name_prefix
        name_suffix = self._text_name_suffix
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
                    "Key.", "",
                )
            else:
                text = key_attr
        return text

    @property
    def text(self):
        return self._text()

    @property
    def canonical_text(self):
        return self._text(canonical=True)

    def __str__(self):
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
        attrs = [
            getattr(self, attr_name)
            for attr_name in attr_names
        ]
        attrs = [
            int(attr)
            if isinstance(attr, float)
            else attr
            for attr in attrs
        ]
        attrs = [
            f"{attr_name}=`{attr}`"
            for attr_name, attr in zip(attr_names, attrs)
            if attr
        ]
        rval = " ".join(attrs)
        return rval

    _text_sep = "-"
    _text_name_prefix = "<"
    _text_name_suffix = ">"

    @classmethod
    def from_children(cls, children_dicts):
        children = [
            ActionEvent(**child_dict)
            for child_dict in children_dicts
        ]
        return ActionEvent(children=children)


class Screenshot(db.Base):
    __tablename__ = "screenshot"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(sa.ForeignKey("recording.timestamp"))
    timestamp = sa.Column(ForceFloat)
    png_data = sa.Column(sa.LargeBinary)

    recording = sa.orm.relationship("Recording", back_populates="screenshots")
    action_event = sa.orm.relationship("ActionEvent", back_populates="screenshot")

    # TODO: convert to png_data on save
    sct_img = None

    # TODO: replace prev with prev_timestamp?
    prev = None
    _image = None
    _diff = None
    _diff_mask = None

    @property
    def image(self):
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
                buffer = io.BytesIO(self.png_data)
                self._image = Image.open(buffer)
        return self._image

    @property
    def diff(self):
        if not self._diff:
            assert self.prev, "Attempted to compute diff before setting prev"
            self._diff = ImageChops.difference(self.image, self.prev.image)
        return self._diff

    @property
    def diff_mask(self):
        if not self._diff_mask:
            if self.diff:
                self._diff_mask = self.diff.convert("1")
        return self._diff_mask

    @property
    def array(self):
        return np.array(self.image)

    @classmethod
    def take_screenshot(cls):
        sct_img = utils.take_screenshot()
        screenshot = Screenshot(sct_img=sct_img)
        return screenshot


class WindowEvent(db.Base):
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
    def get_active_window_event(cls):
        return WindowEvent(**window.get_active_window_data())


class PerformanceStat(db.Base):
    __tablename__ = "performance_stat"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(sa.Integer)
    event_type = sa.Column(sa.String)
    start_time = sa.Column(sa.Integer)
    end_time = sa.Column(sa.Integer)
    window_id = sa.Column(sa.String)
