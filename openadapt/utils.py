"""
Utility functions for OpenAdapt.

This module provides various utility functions used throughout OpenAdapt.
"""

from datetime import datetime, timedelta
from io import BytesIO
from collections import Counter, defaultdict
import base64
import fire
import inspect
import os
import sys
import time

from loguru import logger
from PIL import Image, ImageDraw, ImageFont
import matplotlib.pyplot as plt
import mss
import mss.base
import numpy as np
from logging import StreamHandler

from openadapt import common, config


EMPTY = (None, [], {}, "")


def configure_logging(logger, log_level):
    """
    Configure the logging settings for OpenAdapt.

    Args:
        logger (loguru.logger): The logger object.
        log_level (str): The desired log level.
    """
    # TODO: redact log messages (https://github.com/Delgan/loguru/issues/17#issuecomment-717526130)
    log_level_override = os.getenv("LOG_LEVEL")
    log_level = log_level_override or log_level
    logger.remove()
    logger_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
        "- <level>{message}</level>"
    )
    logger.add(
        StreamHandler(sys.stderr),
        colorize=True,
        level=log_level,
        enqueue=True,
        format=logger_format,
        filter=config.filter_log_messages if config.IGNORE_WARNINGS else None,
    )
    logger.debug(f"{log_level=}")


def row2dict(row, follow=True):
    """
    Convert a row object to a dictionary.

    Args:
        row: The row object.
        follow (bool): Flag indicating whether to follow children. Defaults to True.

    Returns:
        dict: The row object converted to a dictionary.
    """
    if isinstance(row, dict):
        return row
    try_follow = ["children"] if follow else []
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
    """
    Round timestamps in a list of events.

    Args:
        events (list): The list of events.
        num_digits (int): The number of digits to round to.
    """
    for event in events:
        if isinstance(event, dict):
            continue
        prev_timestamp = event.timestamp
        event.timestamp = round(event.timestamp, num_digits)
        logger.debug(f"{prev_timestamp=} {event.timestamp=}")
        if hasattr(event, "children") and event.children:
            round_timestamps(event.children, num_digits)


def rows2dicts(
    rows, drop_empty=True, drop_constant=True, num_digits=None,
):
    """
    Convert a list of rows to a list of dictionaries.

    Args:
        rows (list): The list of rows.
        drop_empty (bool): Flag indicating whether to drop empty rows. Defaults to True.
        drop_constant (bool): Flag indicating whether to drop rows with constant values. Defaults to True.
        num_digits (int): The number of digits to round timestamps to. Defaults to None.

    Returns:
        list: The list of dictionaries representing the rows.
    """
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
                if len(key_to_values[key]) <= 1 or drop_empty and value in EMPTY:
                    del row_dict[key]
    for row_dict in row_dicts:
        # TODO: keep attributes in children which vary across parents
        if "children" in row_dict:
            row_dict["children"] = rows2dicts(
                row_dict["children"], drop_empty, drop_constant,
            )
    return row_dicts


def override_double_click_interval_seconds(override_value):
    """
    Override the double click interval in seconds.

    Args:
        override_value (float): The new value for the double click interval.
    """
    get_double_click_interval_seconds.override_value = override_value


def get_double_click_interval_seconds():
    """
    Get the double click interval in seconds.

    Returns:
        float: The double click interval in seconds.
    """
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
    """
    Get the double click distance in pixels.

    Returns:
        int: The double click distance in pixels.
    """
    if sys.platform == "darwin":
        # From https://developer.apple.com/documentation/appkit/nspressgesturerecognizer/1527495-allowablemovement:  # noqa: E501
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
    """
    Get the dimensions of the monitor.

    Returns:
        tuple: The width and height of the monitor.
    """
    sct = mss.mss()
    monitor = sct.monitors[0]
    monitor_width = monitor["width"]
    monitor_height = monitor["height"]
    return monitor_width, monitor_height


# TODO: move parameters to config
def draw_ellipse(
    x,
    y,
    image,
    width_pct=0.03,
    height_pct=0.03,
    fill_transparency=0.25,
    outline_transparency=0.5,
    outline_width=2,
):
    """
    Draw an ellipse on the image.

    Args:
        x (float): The x-coordinate of the center of the ellipse.
        y (float): The y-coordinate of the center of the ellipse.
        image (PIL.Image.Image): The image to draw on.
        width_pct (float): The percentage of the image width for the width of the ellipse.
        height_pct (float): The percentage of the image height for the height of the ellipse.
        fill_transparency (float): The transparency of the ellipse fill.
        outline_transparency (float): The transparency of the ellipse outline.
        outline_width (int): The width of the ellipse outline.

    Returns:
        PIL.Image.Image: The image with the ellipse drawn on it.
        float: The width of the ellipse.
        float: The height of the ellipse.
    """
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
    """
    Get a font object.

    Args:
        original_font_name (str): The original font name.
        font_size (int): The font size.

    Returns:
        PIL.ImageFont.FreeTypeFont: The font object.
    """
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
    x,
    y,
    text,
    image,
    font_size_pct=0.01,
    font_name="Arial.ttf",
    fill=(255, 0, 0),
    stroke_fill=(255, 255, 255),
    stroke_width=3,
    outline=False,
    outline_padding=10,
):
    """
    Draw text on the image.

    Args:
        x (float): The x-coordinate of the text anchor point.
        y (float): The y-coordinate of the text anchor point.
        text (str): The text to draw.
        image (PIL.Image.Image): The image to draw on.
        font_size_pct (float): The percentage of the image size for the font size. Defaults to 0.01.
        font_name (str): The name of the font. Defaults to "Arial.ttf".
        fill (tuple): The color of the text. Defaults to (255, 0, 0) (red).
        stroke_fill (tuple): The color of the text stroke. Defaults to (255, 255, 255) (white).
        stroke_width (int): The width of the text stroke. Defaults to 3.
        outline (bool): Flag indicating whether to draw an outline around the text. Defaults to False.
        outline_padding (int): The padding size for the outline. Defaults to 10.

    Returns:
        PIL.Image.Image: The image with the text drawn on it.
    """
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
    x0,
    y0,
    x1,
    y1,
    image,
    bg_color=(0, 0, 0),
    fg_color=(255, 255, 255),
    outline_color=(255, 0, 0),
    bg_transparency=0.25,
    fg_transparency=0,
    outline_transparency=0.5,
    outline_width=2,
    invert=False,
):
    """
    Draw a rectangle on the image.

    Args:
        x0 (float): The x-coordinate of the top-left corner of the rectangle.
        y0 (float): The y-coordinate of the top-left corner of the rectangle.
        x1 (float): The x-coordinate of the bottom-right corner of the rectangle.
        y1 (float): The y-coordinate of the bottom-right corner of the rectangle.
        image (PIL.Image.Image): The image to draw on.
        bg_color (tuple): The background color of the rectangle. Defaults to (0, 0, 0) (black).
        fg_color (tuple): The foreground color of the rectangle. Defaults to (255, 255, 255) (white).
        outline_color (tuple): The color of the rectangle outline. Defaults to (255, 0, 0) (red).
        bg_transparency (float): The transparency of the rectangle background. Defaults to 0.25.
        fg_transparency (float): The transparency of the rectangle foreground. Defaults to 0.
        outline_transparency (float): The transparency of the rectangle outline. Defaults to 0.5.
        outline_width (int): The width of the rectangle outline. Defaults to 2.
        invert (bool): Flag indicating whether to invert the colors. Defaults to False.

    Returns:
        PIL.Image.Image: The image with the rectangle drawn on it.
    """
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


def get_scale_ratios(action_event):
    """
    Get the scale ratios for the action event.

    Args:
        action_event (ActionEvent): The action event.

    Returns:
        float: The width ratio.
        float: The height ratio.
    """
    recording = action_event.recording
    image = action_event.screenshot.image
    width_ratio = image.width / recording.monitor_width
    height_ratio = image.height / recording.monitor_height
    return width_ratio, height_ratio


def display_event(
    action_event,
    marker_width_pct=0.03,
    marker_height_pct=0.03,
    marker_fill_transparency=0.25,
    marker_outline_transparency=0.5,
    diff=False,
):
    """
    Display an action event on the image.

    Args:
        action_event (ActionEvent): The action event to display.
        marker_width_pct (float): The percentage of the image width for the marker width. Defaults to 0.03.
        marker_height_pct (float): The percentage of the image height for the marker height. Defaults to 0.03.
        marker_fill_transparency (float): The transparency of the marker fill. Defaults to 0.25.
        marker_outline_transparency (float): The transparency of the marker outline. Defaults to 0.5.
        diff (bool): Flag indicating whether to display the diff image. Defaults to False.

    Returns:
        PIL.Image.Image: The image with the action event displayed on it.
    """
    recording = action_event.recording
    window_event = action_event.window_event
    screenshot = action_event.screenshot
    if diff and screenshot.diff:
        image = screenshot.diff.convert("RGBA")
    else:
        image = screenshot.image.convert("RGBA")
    width_ratio, height_ratio = get_scale_ratios(action_event)

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
                x0,
                y0,
                x1,
                y1,
                image,
                outline_color=(255, 0, 0),
                bg_transparency=0,
                fg_transparency=0,
                # outline_transparency=.75,
                outline_width=20,
            )

    # draw click marker
    if action_event.name in common.MOUSE_EVENTS:
        x = action_event.mouse_x * width_ratio
        y = action_event.mouse_y * height_ratio
        image, ellipse_width, ellipse_height = draw_ellipse(x, y, image)

        # draw text
        dx = action_event.mouse_dx or 0
        dy = action_event.mouse_dy or 0
        d_text = f" {dx=} {dy=}" if dx or dy else ""
        text = f"{action_event.name}{d_text}"
        image = draw_text(x, y + ellipse_height / 2, text, image)
    elif action_event.name in common.KEY_EVENTS:
        x = recording.monitor_width * width_ratio / 2
        y = recording.monitor_height * height_ratio / 2
        text = action_event.text
        if config.SCRUB_ENABLED:
            text = __import__("openadapt").scrub.scrub_text(text, is_separated=True)
        image = draw_text(x, y, text, image, outline=True)
    else:
        raise Exception("unhandled {action_event.name=}")

    return image


def image2utf8(image):
    """
    Convert an image to UTF-8 format.

    Args:
        image (PIL.Image.Image): The image to convert.

    Returns:
        str: The UTF-8 encoded image.
    """
    image = image.convert("RGB")
    buffered = BytesIO()
    image.save(buffered, format="JPEG")
    image_str = base64.b64encode(buffered.getvalue())
    base64_prefix = bytes("data:image/jpeg;base64,", encoding="utf-8")
    image_base64 = base64_prefix + image_str
    image_utf8 = image_base64.decode("utf-8")
    return image_utf8


_start_time = None
_start_perf_counter = None


def set_start_time(value=None):
    """
    Set the start time for performance measurements.

    Args:
        value (float): The start time value. Defaults to the current time.

    Returns:
        float: The start time.
    """
    global _start_time
    _start_time = value or time.time()
    logger.debug(f"{_start_time=}")
    return _start_time


def get_timestamp(is_global=False):
    """
    Get the current timestamp.

    Args:
        is_global (bool): Flag indicating whether to use the global start time. Defaults to False.

    Returns:
        float: The current timestamp.
    """
    global _start_time
    return _start_time + time.perf_counter()


# https://stackoverflow.com/a/50685454
def evenly_spaced(arr, N):
    """
    Get evenly spaced elements from the array.

    Args:
        arr (list): The input array.
        N (int): The number of elements to get.

    Returns:
        list: The evenly spaced elements.
    """
    if N >= len(arr):
        return arr
    idxs = set(np.round(np.linspace(0, len(arr) - 1, N)).astype(int))
    return [val for idx, val in enumerate(arr) if idx in idxs]


def take_screenshot() -> mss.base.ScreenShot:
    """
    Take a screenshot.

    Returns:
        mss.base.ScreenShot: The screenshot.
    """
    with mss.mss() as sct:
        # monitor 0 is all in one
        monitor = sct.monitors[0]
        sct_img = sct.grab(monitor)
    return sct_img


def get_strategy_class_by_name():
    """
    Get a dictionary of strategy classes by their names.

    Returns:
        dict: A dictionary of strategy classes.
    """
    from openadapt.strategies import BaseReplayStrategy

    strategy_classes = BaseReplayStrategy.__subclasses__()
    class_by_name = {cls.__name__: cls for cls in strategy_classes}
    logger.debug(f"{class_by_name=}")
    return class_by_name


def plot_performance(recording_timestamp: float = None) -> None:
    """
    Plot the performance of the event processing and writing.

    Args:
        recording_timestamp (float): The timestamp of the recording (defaults to latest).
    """
    type_to_prev_start_time = defaultdict(list)
    type_to_start_time_deltas = defaultdict(list)
    type_to_proc_times = defaultdict(list)
    type_to_count = Counter()
    type_to_timestamps = defaultdict(list)

    if not recording_timestamp:
        # avoid circular import
        from openadapt import crud

        recording_timestamp = crud.get_latest_recording().timestamp
    perf_stats = crud.get_perf_stats(recording_timestamp)
    perf_stat_dicts = rows2dicts(perf_stats)
    for perf_stat in perf_stat_dicts:
        prev_start_time = type_to_prev_start_time.get(
            perf_stat["event_type"], perf_stat["start_time"]
        )
        start_time_delta = perf_stat["start_time"] - prev_start_time
        type_to_start_time_deltas[perf_stat["event_type"]].append(start_time_delta)
        type_to_prev_start_time[perf_stat["event_type"]] = perf_stat["start_time"]
        type_to_proc_times[perf_stat["event_type"]].append(
            perf_stat["end_time"] - perf_stat["start_time"]
        )
        type_to_count[perf_stat["event_type"]] += 1
        type_to_timestamps[perf_stat["event_type"]].append(perf_stat["start_time"])

    y_data = {"proc_times": {}, "start_time_deltas": {}}
    for i, event_type in enumerate(type_to_count):
        type_count = type_to_count[event_type]
        start_time_deltas = type_to_start_time_deltas[event_type]
        proc_times = type_to_proc_times[event_type]
        y_data["proc_times"][event_type] = proc_times
        y_data["start_time_deltas"][event_type] = start_time_deltas

    fig, axes = plt.subplots(2, 1, sharex=True, figsize=(20, 10))
    for i, data_type in enumerate(y_data):
        for event_type in y_data[data_type]:
            x = type_to_timestamps[event_type]
            y = y_data[data_type][event_type]
            axes[i].scatter(x, y, label=event_type)
        axes[i].set_title(data_type)
        axes[i].legend()
    # TODO: add PROC_WRITE_BY_EVENT_TYPE
    fname_parts = ["performance", f"{recording_timestamp}"]
    fname = "-".join(fname_parts) + ".png"
    os.makedirs(config.DIRNAME_PERFORMANCE_PLOTS, exist_ok=True)
    fpath = os.path.join(config.DIRNAME_PERFORMANCE_PLOTS, fname)
    logger.info(f"{fpath=}")
    plt.savefig(fpath)
    os.system(f"open {fpath}")


def strip_element_state(action_event):
    """
    Strip the element state from the action event and its children.

    Args:
        action_event (ActionEvent): The action event to strip.

    Returns:
        ActionEvent: The action event without element state.
    """
    action_event.element_state = None
    for child in action_event.children:
        strip_element_state(child)
    return action_event


def get_functions(name) -> dict:
    """
    Get a dictionary of function names to functions for all non-private functions

    Usage:

        if __name__ == "__main__":
            fire.Fire(utils.get_functions(__name__))

    Args:
        name (str): The name of the module to get functions from.

    Returns:
        dict: A dictionary of function names to functions.
    """
    functions = {}
    for name, obj in inspect.getmembers(sys.modules[name]):
        if inspect.isfunction(obj) and not name.startswith("_"):
            functions[name] = obj
    return functions


if __name__ == "__main__":
    fire.Fire(get_functions(__name__))
