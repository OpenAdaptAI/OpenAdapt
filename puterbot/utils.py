from io import BytesIO
import base64
import os
import sys
import time
import datetime

from loguru import logger
from PIL import Image, ImageDraw, ImageFont
import mss
import mss.base
import numpy as np

from puterbot.config import DT_FMT
from puterbot.common import MOUSE_EVENTS, KEY_EVENTS


EMPTY = (None, [], {}, "")


def get_now_dt_str(dt_format=DT_FMT):
    """
    Get the current date and time as a formatted string.

    Args:
        dt_format (str): The format to use for the date and time string.

    Returns:
        str: The current date and time formatted as a string.
    """
    # Get the current date and time
    now = datetime.datetime.now()

    # Format the date and time according to the specified format
    dt_str = now.strftime(dt_format)

    return dt_str


def configure_logging(logger, log_level):
    log_level_override = os.getenv("LOG_LEVEL")
    log_level = log_level_override or log_level
    logger.remove()
    logger.add(sys.stderr, level=log_level)
    logger.debug(f"{log_level=}")


def row2dict(row, follow=True):
    if isinstance(row, dict):
        return row
    try_follow = [
        "children",
    ] if follow else []
    to_follow = [key for key in try_follow if hasattr(row, key)]

    # follow children recursively
    if "children" in to_follow:
        to_follow = {key: {} for key in to_follow}
        to_follow["children"]["follow"] = to_follow

    try_include = [
        "key",
        "text",
        "canonical_key",
        "canonical_text",
    ]
    to_include = [key for key in try_include if hasattr(row, key)]
    row_dict = row.asdict(follow=to_follow, include=to_include)
    return row_dict


def round_timestamps(events, num_digits):
    for event in events:
        if isinstance(event, dict):
            continue
        prev_timestamp = event.timestamp
        event.timestamp = round(event.timestamp, num_digits)
        logger.debug(f"{prev_timestamp=} {event.timestamp=}")
        if hasattr(event, "children") and event.children:
            round_timestamps(event.children, num_digits)


def rows2dicts(
    rows,
    drop_empty=True,
    drop_constant=True,
    num_digits=None,
):
    if num_digits:
        round_timestamps(rows, num_digits)
    row_dicts = [row2dict(row) for row in rows]
    if drop_empty:
        keep_keys = set()
        for row_dict in row_dicts:
            for key in row_dict:
                if row_dict[key] not in EMPTY:
                    keep_keys.add(key)
        for row_dict in row_dicts:
            for key in list(row_dict.keys()):
                if key not in keep_keys:
                    del row_dict[key]
    if drop_constant:
        key_to_values = {}
        for row_dict in row_dicts:
            for key in row_dict:
                key_to_values.setdefault(key, set())
                value = repr(row_dict[key])
                key_to_values[key].add(value)
        for row_dict in row_dicts:
            for key in list(row_dict.keys()):
                value = row_dict[key]
                if (
                    len(key_to_values[key]) <= 1
                    or drop_empty and value in EMPTY
                ):
                    del row_dict[key]
    for row_dict in row_dicts:
        # TODO: keep attributes in children which vary across parents
        if "children" in row_dict:
            row_dict["children"] = rows2dicts(
                row_dict["children"], drop_empty, drop_constant,
            )
    return row_dicts


def override_double_click_interval_seconds(override_value):
    get_double_click_interval_seconds.override_value = override_value


def get_double_click_interval_seconds():
    if hasattr(get_double_click_interval_seconds, "override_value"):
        return get_double_click_interval_seconds.override_value
    if sys.platform == "darwin":
        from AppKit import NSEvent
        return NSEvent.doubleClickInterval()
    elif sys.platform == "win32":
        # https://stackoverflow.com/a/31686041/95989
        from ctypes import windll
        return windll.user32.GetDoubleClickTime() / 1000
    else:
        raise Exception(f"Unsupported {sys.platform=}")


def get_double_click_distance_pixels():
    if sys.platform == "darwin":
        # From https://developer.apple.com/documentation/appkit/nspressgesturerecognizer/1527495-allowablemovement:
        #     The default value of this property is the same as the
        #     double-click distance.
        # TODO: do this more robustly; see:
        # https://forum.xojo.com/t/get-allowed-unit-distance-between-doubleclicks-on-macos/35014/7
        from AppKit import NSPressGestureRecognizer
        return NSPressGestureRecognizer.new().allowableMovement()
    elif sys.platform == "win32":
        import win32api
        import win32con
        x = win32api.GetSystemMetrics(win32con.SM_CXDOUBLECLK)
        y = win32api.GetSystemMetrics(win32con.SM_CYDOUBLECLK)
        if x != y:
            logger.warning(f"{x=} != {y=}")
        return max(x, y)
    else:
        raise Exception(f"Unsupported {sys.platform=}")


def get_monitor_dims():
    sct = mss.mss()
    monitor = sct.monitors[0]
    monitor_width = monitor["width"]
    monitor_height = monitor["height"]
    return monitor_width, monitor_height


def draw_ellipse(
    x, y, image,
    width_pct=.03,
    height_pct=.03,
    fill_transparency=.25,
    outline_transparency=.5,
    outline_width=2,
):
    overlay = Image.new("RGBA", image.size)
    draw = ImageDraw.Draw(overlay)
    max_dim = max(image.size)
    width = width_pct * max_dim
    height = height_pct * max_dim
    x0 = x - width / 2
    x1 = x + width / 2
    y0 = y - height / 2
    y1 = y + height / 2
    xy = (x0, y0, x1, y1)
    fill_opacity = int(255 * fill_transparency)
    outline_opacity = int(255 * outline_transparency)
    fill = (255, 0, 0, fill_opacity)
    outline = (0, 0, 0, outline_opacity)
    draw.ellipse(xy, fill=fill, outline=outline, width=outline_width)
    image = Image.alpha_composite(image, overlay)
    return image, width, height


def get_font(original_font_name, font_size):
    font_names = [
        original_font_name,
        original_font_name.lower(),
    ]
    for font_name in font_names:
        logger.debug(f"Attempting to load {font_name=}...")
        try:
            return ImageFont.truetype(font_name, font_size)
        except OSError as exc:
            logger.debug(f"Unable to load {font_name=}, {exc=}")
    raise


def draw_text(
    x, y, text, image,
    font_size_pct=0.01,
    font_name="Arial.ttf",
    fill=(255, 0, 0),
    stroke_fill=(255, 255, 255),
    stroke_width=3,
    outline=False,
    outline_padding=10,
):
    overlay = Image.new("RGBA", image.size)
    draw = ImageDraw.Draw(overlay)
    max_dim = max(image.size)
    font_size = int(font_size_pct * max_dim)
    font = get_font(font_name, font_size)
    fill = (255, 0, 0)
    stroke_fill = (255, 255, 255)
    stroke_width = 3
    text_bbox = font.getbbox(text)
    bbox_left, bbox_top, bbox_right, bbox_bottom = text_bbox
    bbox_width = bbox_right - bbox_left
    bbox_height = bbox_bottom - bbox_top
    if outline:
        x0 = x - bbox_width / 2 - outline_padding
        x1 = x + bbox_width / 2 + outline_padding
        y0 = y - bbox_height / 2 - outline_padding
        y1 = y + bbox_height / 2 + outline_padding
        image = draw_rectangle(x0, y0, x1, y1, image, invert=True)
    xy = (x, y)
    draw.text(
        xy,
        text=text,
        font=font,
        fill=fill,
        stroke_fill=stroke_fill,
        stroke_width=stroke_width,
        # https://pillow.readthedocs.io/en/stable/handbook/text-anchors.html#text-anchors
        anchor="mm",
    )
    image = Image.alpha_composite(image, overlay)
    return image


def draw_rectangle(
    x0, y0, x1, y1, image,
    bg_color=(0, 0, 0),
    fg_color=(255, 255, 255),
    outline_color=(255, 0, 0),
    bg_transparency=0.25,
    fg_transparency=0,
    outline_transparency=0.5,
    outline_width=2,
    invert=False,
):
    if invert:
        bg_color, fg_color = fg_color, bg_color
        bg_transparency, fg_transparency = fg_transparency, bg_transparency
    bg_opacity = int(255 * bg_transparency)
    overlay = Image.new("RGBA", image.size, bg_color + (bg_opacity,))
    draw = ImageDraw.Draw(overlay)
    xy = (x0, y0, x1, y1)
    fg_opacity = int(255 * fg_transparency)
    outline_opacity = int(255 * outline_transparency)
    fill = fg_color + (fg_opacity,)
    outline = outline_color + (outline_opacity,)
    draw.rectangle(xy, fill=fill, outline=outline, width=outline_width)
    image = Image.alpha_composite(image, overlay)
    return image


def get_scale_ratios(input_event):
    recording = input_event.recording
    image = input_event.screenshot.image
    width_ratio = image.width / recording.monitor_width
    height_ratio = image.height / recording.monitor_height
    return width_ratio, height_ratio


def display_event(
    input_event,
    marker_width_pct=.03,
    marker_height_pct=.03,
    marker_fill_transparency=.25,
    marker_outline_transparency=.5,
    diff=False,
):
    recording = input_event.recording
    window_event = input_event.window_event
    screenshot = input_event.screenshot
    if diff and screenshot.diff:
        image = screenshot.diff.convert("RGBA")
    else:
        image = screenshot.image.convert("RGBA")
    width_ratio, height_ratio = get_scale_ratios(input_event)

    # dim area outside window event
    x0 = window_event.left * width_ratio
    y0 = window_event.top * height_ratio
    x1 = x0 + window_event.width * width_ratio
    y1 = y0 + window_event.height * height_ratio
    image = draw_rectangle(x0, y0, x1, y1, image, outline_width=5)

    # display diff bbox
    if diff:
        diff_bbox = screenshot.diff.getbbox()
        if diff_bbox:
            x0, y0, x1, y1 = diff_bbox
            image = draw_rectangle(
                x0, y0, x1, y1, image,
                outline_color=(255, 0, 0),
                bg_transparency=0,
                fg_transparency=0,
                #outline_transparency=.75,
                outline_width=20,
            )

    # draw click marker
    if input_event.name in MOUSE_EVENTS:
        x = input_event.mouse_x * width_ratio
        y = input_event.mouse_y * height_ratio
        image, ellipse_width, ellipse_height = draw_ellipse(x, y, image)

        # draw text
        dx = input_event.mouse_dx or 0
        dy = input_event.mouse_dy or 0
        d_text = f" {dx=} {dy=}" if dx or dy else ""
        text = f"{input_event.name}{d_text}"
        image = draw_text(x, y + ellipse_height / 2, text, image)
    elif input_event.name in KEY_EVENTS:
        x = recording.monitor_width * width_ratio / 2
        y = recording.monitor_height * height_ratio / 2
        text = input_event.text
        image = draw_text(x, y, text, image, outline=True)
    else:
        raise Exception("unhandled {input_event.name=}")

    return image


def image2utf8(image):
    image = image.convert("RGB")
    buffered = BytesIO()
    image.save(buffered, format="JPEG")
    image_str = base64.b64encode(buffered.getvalue())
    base64_prefix = bytes("data:image/jpeg;base64,", encoding="utf-8")
    image_base64 = base64_prefix + image_str
    image_utf8 = image_base64.decode("utf-8")
    return image_utf8


_start_time = None


def set_start_time(value=None):
    global _start_time
    _start_time = value or time.time()
    logger.debug(f"{_start_time=}")
    return _start_time


def get_timestamp(is_global=False):
    global _start_time
    return _start_time + time.perf_counter()


# https://stackoverflow.com/a/50685454
def evenly_spaced(arr, N):
    if N >= len(arr):
        return arr
    idxs = set(np.round(np.linspace(0, len(arr) - 1, N)).astype(int))
    return [val for idx, val in enumerate(arr) if idx in idxs]


def take_screenshot() -> mss.base.ScreenShot:
    with mss.mss() as sct:
        # monitor 0 is all in one
        monitor = sct.monitors[0]
        sct_img = sct.grab(monitor)
    return sct_img


def get_strategy_class_by_name():
    from puterbot.strategies import BaseReplayStrategy
    strategy_classes = BaseReplayStrategy.__subclasses__()
    class_by_name = {
        cls.__name__: cls
        for cls in strategy_classes
    }
    logger.debug(f"{class_by_name=}")
    return class_by_name
