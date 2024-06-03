"""Utility functions for OpenAdapt.

This module provides various utility functions used throughout OpenAdapt.
"""

from io import BytesIO
from logging import StreamHandler
from typing import Any
import ast
import base64
import inspect
import os
import sys
import threading
import time

from jinja2 import Environment, FileSystemLoader
from loguru import logger
from PIL import Image

from openadapt.build_utils import redirect_stdout_stderr

with redirect_stdout_stderr():
    import fire

import mss
import mss.base
import numpy as np
import orjson

if sys.platform == "win32":
    import mss.windows

    # fix cursor flicker on windows; see:
    # https://github.com/BoboTiG/python-mss/issues/179#issuecomment-673292002
    mss.windows.CAPTUREBLT = 0


from openadapt import common
from openadapt.config import config
from openadapt.db import db
from openadapt.logging import filter_log_messages
from openadapt.models import ActionEvent

# TODO: move to constants.py
EMPTY = (None, [], {}, "")
SCT = mss.mss()

_logger_lock = threading.Lock()
_start_time = None
_start_perf_counter = None


def configure_logging(logger: logger, log_level: str) -> None:
    """Configure the logging settings for OpenAdapt.

    Args:
        logger (logger): The logger object.
        log_level (str): The desired log level.
    """
    # TODO: redact log messages
    #  (https://github.com/Delgan/loguru/issues/17#issuecomment-717526130)
    log_level_override = os.getenv("LOG_LEVEL")
    log_level = log_level_override or log_level

    with _logger_lock:
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
            filter=(
                filter_log_messages if config.MAX_NUM_WARNINGS_PER_SECOND > 0 else None
            ),
        )
        logger.debug(f"{log_level=}")


def row2dict(row: dict | db.BaseModel, follow: bool = True) -> dict:
    """Convert a row object to a dictionary.

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
        "reducer_names",
    ]
    to_include = [key for key in try_include if hasattr(row, key)]
    row_dict = row.asdict(follow=to_follow, include=to_include)
    return row_dict


def round_timestamps(events: list[ActionEvent], num_digits: int) -> None:
    """Round timestamps in a list of events.

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
    rows: list[ActionEvent],
    drop_empty: bool = True,
    drop_constant: bool = True,
    drop_cols: list[str] = [],
    num_digits: int = None,
) -> list[dict]:
    """Convert a list of rows to a list of dictionaries.

    Args:
        rows (list): The list of rows.
        drop_empty (bool): Flag indicating whether to drop empty rows. Defaults to True.
        drop_constant (bool): Flag indicating whether to drop rows with constant values.
          Defaults to True.
        drop_cols (list[str]): The names of columns to drop.
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
        for key in drop_cols:
            if key in row_dict:
                del row_dict[key]
        # TODO: keep attributes in children which vary across parents
        if "children" in row_dict:
            row_dict["children"] = rows2dicts(
                row_dict["children"],
                drop_empty,
                drop_constant,
                drop_cols,
            )
    return row_dicts


def override_double_click_interval_seconds(
    override_value: float,
) -> None:
    """Override the double click interval in seconds.

    Args:
        override_value (float): The new value for the double click interval.
    """
    get_double_click_interval_seconds.override_value = override_value


def get_double_click_interval_seconds() -> float:
    """Get the double click interval in seconds.

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


def get_double_click_distance_pixels() -> int:
    """Get the double click distance in pixels.

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


def get_monitor_dims() -> tuple[int, int]:
    """Get the dimensions of the monitor.

    Returns:
        tuple[int, int]: The width and height of the monitor.
    """
    # TODO XXX: replace with get_screenshot().size and remove get_scale_ratios?
    monitor = SCT.monitors[0]
    monitor_width = monitor["width"]
    monitor_height = monitor["height"]
    return monitor_width, monitor_height


def get_scale_ratios(
    action_event: ActionEvent | None = None,
) -> tuple[float, float]:
    """Get the scale ratios for the action event.

    <position in image space> = scale_ratio * <position in window/action space>, e.g:

        width_ratio, height_ratio = get_scale_ratios(action_event)
        x0 = window_event.left * width_ratio
        y0 = window_event.top * height_ratio
        x1 = x0 + window_event.width * width_ratio
        y1 = y0 + window_event.height * height_ratio
        x = action_event.mouse_x * width_ratio
        y = action_event.mouse_y * height_ratio

    Args:
        action_event (ActionEvent): The action event.

    Returns:
        float: The width ratio.
        float: The height ratio.
    """
    if action_event:
        recording = action_event.recording
        monitor_width = recording.monitor_width
        monitor_height = recording.monitor_hefith
        image = action_event.screenshot.image
    else:
        image = take_screenshot()
        monitor_width, monitor_height = get_monitor_dims()
    width_ratio = image.width / monitor_width
    height_ratio = image.height / monitor_height
    return width_ratio, height_ratio


# TODO: png
def image2utf8(image: Image.Image, include_prefix: bool = True) -> str:
    """Convert an image to UTF-8 format.

    Args:
        image (PIL.Image.Image): The image to convert.
        include_prefix (bool): Whether to include the "data:" prefix.

    Returns:
        str: The UTF-8 encoded image.
    """
    if not image:
        return ""
    image = image.convert("RGB")
    buffered = BytesIO()
    image.save(buffered, format="JPEG")
    image_str = base64.b64encode(buffered.getvalue())
    base64_prefix = bytes("data:image/jpeg;base64,", encoding="utf-8")
    image_base64 = base64_prefix + image_str
    image_utf8 = image_base64.decode("utf-8")
    return image_utf8


def utf82image(image_utf8: str) -> Image.Image:
    """Convert a UTF-8 encoded image back into a PIL image object.

    Inverts utf82image.

    Args:
        image_utf8 (str): The UTF-8 encoded image.

    Returns:
        PIL.Image.Image: The decoded image as a PIL image object.
    """
    if not image_utf8:
        return None

    # Remove the base64 image prefix
    base64_data = image_utf8.split(",", 1)[1]

    # Decode the base64 string
    image_bytes = base64.b64decode(base64_data)

    # Convert bytes to image
    image = Image.open(BytesIO(image_bytes))

    return image


def set_start_time(value: float = None) -> float:
    """Set the start time for recordings. Required for accurate process-wide timestamps.

    Args:
        value (float): The start time value. Defaults to the current time.

    Returns:
        float: The start time.
    """
    global _start_time
    global _start_perf_counter
    _start_time = value or time.time()
    _start_perf_counter = time.perf_counter()
    logger.debug(f"{_start_time=} {_start_perf_counter=}")
    return _start_time


def get_timestamp() -> float:
    """Get the current timestamp, synchronized between processes.

    Before calling this function from any process, set_start_time must have been called.

    Returns:
        float: The current timestamp.
    """
    global _start_time
    global _start_perf_counter

    msg = "set_start_time must be called before get_timestamp"
    assert _start_time, f"{_start_time=}; {msg}"
    assert _start_perf_counter, f"{_start_perf_counter=}; {msg}"

    perf_duration = time.perf_counter() - _start_perf_counter
    return _start_time + perf_duration


# https://stackoverflow.com/a/50685454
def evenly_spaced(arr: list, N: list) -> list:
    """Get evenly spaced elements from the array.

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


def take_screenshot() -> Image.Image:
    """Take a screenshot.

    Returns:
        PIL.Image: The screenshot image.
    """
    # monitor 0 is all in one
    monitor = SCT.monitors[0]
    sct_img = SCT.grab(monitor)
    image = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
    return image


def get_strategy_class_by_name() -> dict:
    """Get a dictionary of strategy classes by their names.

    Returns:
        dict: A dictionary of strategy classes.
    """
    from openadapt.strategies import BaseReplayStrategy

    strategy_classes = BaseReplayStrategy.__subclasses__()
    class_by_name = {cls.__name__: cls for cls in strategy_classes}
    logger.debug(f"{class_by_name=}")
    return class_by_name


def strip_element_state(action_event: ActionEvent) -> ActionEvent:
    """Strip the element state from the action event and its children.

    Args:
        action_event (ActionEvent): The action event to strip.

    Returns:
        ActionEvent: The action event without element state.
    """
    action_event.element_state = None
    for child in action_event.children:
        strip_element_state(child)
    return action_event


def compute_diff(image1: Image.Image, image2: Image.Image) -> Image.Image:
    """Computes the difference between two PIL Images and returns the diff image."""
    arr1 = np.array(image1)
    arr2 = np.array(image2)
    diff = np.abs(arr1 - arr2)
    return Image.fromarray(diff.astype("uint8"))


def get_functions(name: str) -> dict:
    """Get a dictionary of function names to functions for all non-private functions.

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


def get_action_dict_from_completion(completion: str) -> dict[ActionEvent]:
    """Convert the completion to a dictionary containing action information.

    Args:
        completion (str): The completion provided by the user.

    Returns:
        dict: The action dictionary.
    """
    try:
        action = eval(completion)
    except Exception as exc:
        logger.warning(f"{exc=}")
    else:
        return action


# copied from https://github.com/OpenAdaptAI/OpenAdapt/pull/560/files
def render_template_from_file(template_relative_path: str, **kwargs: dict) -> str:
    """Load a Jinja2 template from a file and interpolate arguments.

    Args:
        template_relative_path (str): Relative path to the Jinja2 template file
            from the project root.
        **kwargs: Arguments to interpolate into the template.

    Returns:
        str: Rendered template with interpolated arguments.
    """

    def orjson_to_json(value: Any) -> str:
        # orjson.dumps returns bytes, so decode to string
        return orjson.dumps(value).decode("utf-8")

    # Construct the full path to the template file
    template_path = os.path.join(config.ROOT_DIR_PATH, template_relative_path)

    # Extract the directory and template file name
    template_dir, template_file = os.path.split(template_path)
    logger.info(f"{template_dir=} {template_file=}")

    # Create a Jinja2 environment with the directory
    env = Environment(loader=FileSystemLoader(template_dir))

    # Add custom filters
    env.filters["orjson"] = orjson_to_json
    env.globals.update(zip=zip)

    # Load the template
    template = env.get_template(template_file)

    # Render the template with provided arguments
    return template.render(**kwargs)


def parse_code_snippet(snippet: str) -> dict:
    """Parse a text code snippet into a dict.

    e.g.
        ```json
        { "foo": true }
        ```
    Returns:

        { "foo": True }

    Args:
        snippet: text snippet

    Returns:
        dict representation of what was in the text snippet
    """
    try:
        if snippet.startswith("```json"):
            # Remove Markdown code block syntax
            json_string = (
                snippet.replace("```json\n", "")
                .replace("```", "")
                .replace("True", "true")
                .replace("False", "false")
                .strip()
            )
            # Parse the JSON string
            rval = ast.literal_eval(json_string)
            return rval

        elif snippet.startswith("```python"):
            python_code = snippet.replace("```python\n", "").replace("```", "").strip()
            return ast.literal_eval(python_code)
        else:
            msg = f"Unsupported {snippet=}"
            logger.warning(msg)
            return None
    except Exception as exc:
        # TODO
        raise exc


def split_list(input_list: list, size: int) -> list[list]:
    """Splits a list into a list of lists, where each inner list has a maximum size.

    Args:
        input_list: The list to be split.
        size: The maximum size of each inner list.

    Returns:
        A new list containing inner lists of the given size.
    """
    return [input_list[i : i + size] for i in range(0, len(input_list), size)]


def filter_keys(data: dict, key_suffixes: list[str]) -> dict:
    """Return a dictionary only containing keys that match the given key suffixes.

    Retains nested structures.

    Args:
        data (dict): The input dictionary to filter.
        key_suffixes (list[str]): A list of key suffixes to match against the keys in
            the dictionary.

    Returns:
        dict: A dictionary with keys filtered by specified suffixes.
    """
    suffixes = tuple(suffix.lower() for suffix in key_suffixes)

    def recurse(obj):
        if isinstance(obj, dict):
            # Process each child to see if it or its descendants match the suffixes
            new_dict = {
                k: recurse(v)
                for k, v in obj.items()
                if k.lower().endswith(suffixes) or isinstance(v, (dict, list))
            }
            return new_dict
        elif isinstance(obj, list):
            # Filter each item in the list based on suffix criteria in their elements
            return [recurse(item) for item in obj]
        else:
            # Return the value directly if it is neither dict nor list
            return obj

    return recurse(data)


def clean_data(data: dict) -> dict:
    """
    Clean a dictionary by removing None values and redundant information.

    Args:
        data (dict): The dictionary to clean.

    Returns:
        dict: A cleaned dictionary with no None values and redundant data removed.
    """
    def remove_none_values(d: dict) -> dict:
        """Remove keys where the value is None."""
        return {k: v for k, v in d.items() if v is not None}

    def compare_dicts(d1: dict, d2: dict) -> bool:
        """Check if all non-None items in d1 are in d2."""
        for k, v in d1.items():
            if v is not None and (k not in d2 or d2[k] != v):
                return False
        return True

    def recurse(obj):
        if isinstance(obj, dict):
            temp_dict = {k: recurse(v) for k, v in obj.items()}
            # Remove redundant nested keys
            keys_to_remove = set()
            keys = list(temp_dict.keys())
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    if (
                        isinstance(temp_dict[keys[i]], dict) and
                        isinstance(temp_dict[keys[j]], dict)
                    ):
                        if compare_dicts(temp_dict[keys[i]], temp_dict[keys[j]]):
                            keys_to_remove.add(keys[i])
                        elif compare_dicts(temp_dict[keys[j]], temp_dict[keys[i]]):
                            keys_to_remove.add(keys[j])
            for key in keys_to_remove:
                del temp_dict[key]

            return remove_none_values(temp_dict)
        elif isinstance(obj, list):
            filtered_list = [recurse(item) for item in obj]
            return [item for item in filtered_list if item]
        else:
            return obj

    return recurse(data)


def normalize_positions(
    data: dict,
    width_delta: float,
    height_delta: float,
    width_keys: list[str] = None,
    height_keys: list[str] = None,
) -> dict:
    """
    Recursively normalize the position keys in a dictionary by adding deltas.

    This function traverses through all dictionary values. If a key matches
    those specified in width_keys, it adds width_delta to its value. Similarly,
    if a key matches those in height_keys, it adds height_delta to its value.

    Args:
        data (dict): The dictionary to process.
        width_delta (float): The delta to add to width/x values.
        height_delta (float): The delta to add to height/y values.
        width_keys (list[str]): List of keys corresponding to width or x coordinates.
        height_keys (list[str]): List of keys corresponding to height or y coordinates.

    Returns:
        dict: A dictionary with normalized position values.
    """
    if width_keys is None:
        width_keys = ["x"]
    if height_keys is None:
        height_keys = ["y"]

    for key, value in data.items():
        if isinstance(value, dict):
            data[key] = normalize_positions(
                value, width_delta, height_delta, width_keys, height_keys,
            )
        elif isinstance(value, list):
            data[key] = [
                normalize_positions(
                    val, width_delta, height_delta, width_keys, height_keys,
                )
                for val in value
            ]
        elif key in width_keys and isinstance(value, (int, float)):
            old_value = value
            data[key] = value + width_delta
            logger.debug(
                f"Normalized {key=} from {old_value} to {data[key]} ({width_delta=})"
            )
        elif key in height_keys and isinstance(value, (int, float)):
            old_value = value
            data[key] = value + height_delta
            logger.debug(
                f"Normalized {key=} from {old_value} to {data[key]} ({height_delta=})"
            )

    return data


if __name__ == "__main__":
    fire.Fire(get_functions(__name__))
