"""This module provides functionality for aggregating events."""

from pprint import pformat
from typing import Any, Callable, Optional
import time

from loguru import logger
from scipy.spatial import distance
import numpy as np

from openadapt import common, models, utils
from openadapt.db import crud

MAX_PROCESS_ITERS = 1
MOUSE_MOVE_EVENT_MERGE_CLICK_DISTANCE_THRESHOLD = 5
MOUSE_MOVE_EVENT_MERGE_DIFF_DISTANCE_THRESHOLD = 1
MOUSE_MOVE_EVENT_MERGE_MIN_IDX_DELTA = 5
KEYBOARD_EVENTS_MERGE_GROUP_NAMED_KEYS = True
USE_SCREENSHOT_DIFFS = False


def get_events(
    recording: models.Recording,
    process: bool = True,
    meta: dict = None,
    session: Any = None,
) -> list[models.ActionEvent]:
    """Retrieve events for a recording.

    Args:
        recording (models.Recording): The recording object.
        process (bool): Whether to process the events by merging and discarding certain
          types of events. Default is True.
        meta (dict): Metadata dictionary to populate with information
          about the processing. Default is None.
        session (Any): The database session. Default is None.

    Returns:
        list: A list of action events.
    """
    start_time = time.time()
    action_events = crud.get_action_events(recording, session=session)
    window_events = crud.get_window_events(recording, session=session)
    screenshots = crud.get_screenshots(recording, session=session)

    raw_action_event_dicts = utils.rows2dicts(action_events)
    logger.debug(f"raw_action_event_dicts=\n{pformat(raw_action_event_dicts)}")

    num_action_events = len(action_events)
    assert num_action_events > 0, "No action events found."
    num_window_events = len(window_events)
    num_screenshots = len(screenshots)

    num_action_events_raw = num_action_events
    num_window_events_raw = num_window_events
    num_screenshots_raw = num_screenshots
    duration_raw = action_events[-1].timestamp - action_events[0].timestamp

    num_process_iters = 0
    if process:
        while True:
            logger.info(
                f"{num_process_iters=} "
                f"{num_action_events=} "
                f"{num_window_events=} "
                f"{num_screenshots=}"
            )
            (
                action_events,
                window_events,
                screenshots,
            ) = process_events(
                action_events,
                window_events,
                screenshots,
            )
            if (
                len(action_events) == num_action_events
                and len(window_events) == num_window_events
                and len(screenshots) == num_screenshots
            ):
                break
            num_process_iters += 1
            num_action_events = len(action_events)
            num_window_events = len(window_events)
            num_screenshots = len(screenshots)
            if num_process_iters == MAX_PROCESS_ITERS:
                break

    if meta is not None:
        format_num = (
            lambda num, raw_num: f"{num} of {raw_num} ({(num / raw_num):.2%})"
        )  # noqa: E731
        meta["num_process_iters"] = num_process_iters
        meta["num_action_events"] = format_num(
            num_action_events,
            num_action_events_raw,
        )
        meta["num_window_events"] = format_num(
            num_window_events,
            num_window_events_raw,
        )
        meta["num_screenshots"] = format_num(
            num_screenshots,
            num_screenshots_raw,
        )

        duration = action_events[-1].timestamp - action_events[0].timestamp
        if len(action_events) > 1:
            assert duration > 0, duration
        meta["duration"] = format_num(duration, duration_raw)

    end_time = time.time()
    duration = end_time - start_time
    logger.info(f"{duration=}")

    return action_events  # , window_events, screenshots


def make_parent_event(
    child: models.ActionEvent, extra: dict[str, Any] = None
) -> models.ActionEvent:
    """Create a parent event from a child event.

    Args:
        child (models.ActionEvent): The child event.
        extra (dict): Extra attributes to include in the parent event. Default is None.

    Returns:
        models.ActionEvent: The parent event.

    """
    # TODO: record which process_fn created the parent event
    event_dict = {
        # TODO: set parent event to child timestamp?
        # "timestamp": child.timestamp,
        "recording_timestamp": child.recording_timestamp,
        "window_event_timestamp": child.window_event_timestamp,
        "screenshot_timestamp": child.screenshot_timestamp,
        "recording": child.recording,
        "window_event": child.window_event,
        "screenshot": child.screenshot,
    }
    extra = extra or {}
    for key, val in extra.items():
        event_dict[key] = val
    action_event = models.ActionEvent(**event_dict)
    return action_event


def merge_consecutive_mouse_move_events(
    events: list[models.ActionEvent],
    by_diff_distance: bool = USE_SCREENSHOT_DIFFS,
) -> list[models.ActionEvent]:
    """Merge consecutive mouse move events into a single move event.

    Args:
        events (list): The list of events to process.
        by_diff_distance (bool): Whether to compute the distance from the mouse to
          the screenshot diff. This is computationally expensive but keeps more useful
          events. Default is False.

    Returns:
        list: The merged list of events.

    """
    _all_slowdowns = []

    def is_target_event(event: models.ActionEvent, state: dict[str, Any]) -> bool:
        return event.name == "move"

    def get_merged_events(
        to_merge: list[models.ActionEvent],
        state: dict[str, Any],
        distance_threshold: int = MOUSE_MOVE_EVENT_MERGE_DIFF_DISTANCE_THRESHOLD,
        # Minimum number of consecutive events (in which the distance between
        # the cursor and the nearest non-zero diff pixel is greater than
        # distance_threshold) in order to result in a separate parent event.
        # Larger values merge more events under a single parent.
        # TODO: verify logic is correct (test)
        # TODO: compute, e.g. as a function of diff and/or cursor velocity?
        min_idx_delta: int = MOUSE_MOVE_EVENT_MERGE_MIN_IDX_DELTA,  # 100
    ) -> list[models.ActionEvent]:
        N = len(to_merge)
        # (inclusive, exclusive)
        group_idx_tups = [(0, N)]
        if by_diff_distance:
            width_ratio, height_ratio = utils.get_scale_ratios(to_merge[0])
            close_idxs = []
            # TODO: improve performance, e.g. vectorization, resizing
            _all_dts = []
            for idx, event in enumerate(to_merge):
                cursor_position = (
                    event.mouse_y * height_ratio,
                    event.mouse_x * width_ratio,
                )
                diff_mask = event.screenshot.diff_mask

                _ts = [time.perf_counter()]
                # ~99x slowdown
                if np.any(diff_mask):
                    _ts.append(time.perf_counter())

                    # TODO: compare with
                    # https://logicatcore.github.io/2020-08-13-sparse-image-coordinates/

                    # ~247x slowdown
                    diff_positions = np.argwhere(diff_mask)
                    _ts.append(time.perf_counter())

                    # ~6x slowdown
                    distances = distance.cdist(
                        [cursor_position],
                        diff_positions,
                    )
                    _ts.append(time.perf_counter())

                    # ~1x slowdown
                    min_distance = distances.min()
                    _ts.append(time.perf_counter())
                    _dts = np.diff(_ts)
                    _all_dts.append(_dts)

                    logger.info(f"{min_distance=}")
                    if min_distance <= distance_threshold:
                        close_idxs.append(idx)

            if _all_dts:
                _all_dts = np.array(_all_dts)
                _slowdowns = _all_dts.mean(axis=0) / _all_dts.mean(axis=0).min()
                _all_slowdowns.append(_slowdowns)
                _mean_slowdowns = np.mean(_all_slowdowns, axis=0)
                logger.info(f"{_mean_slowdowns=}")

            if close_idxs:
                idx_deltas = np.diff(close_idxs)
                min_idx_delta_idxs = (
                    np.argwhere(idx_deltas >= min_idx_delta).flatten().tolist()
                )
                group_idxs = np.array(close_idxs)[min_idx_delta_idxs].tolist()
                prefix = [0] if not group_idxs or group_idxs[0] != 0 else []
                suffix = [N] if not group_idxs or group_idxs[-1] != N else []
                group_boundary_idxs = prefix + group_idxs + suffix
                logger.debug(f"{close_idxs=}")
                logger.debug(f"{idx_deltas=}")
                logger.debug(f"{min_idx_delta_idxs=}")
                logger.debug(f"{group_idxs=}")
                logger.debug(f"{group_boundary_idxs=}")
                group_idx_tups = [
                    (start_idx, end_idx)
                    for start_idx, end_idx in zip(
                        group_boundary_idxs, group_boundary_idxs[1:]
                    )
                ]
        logger.debug(f"{group_idx_tups=}")
        merged_events = []
        for start_idx, end_idx in group_idx_tups:
            children = to_merge[start_idx:end_idx]
            # TODO: consolidate pattern with merge_consecutive_keyboard_events
            if len(children) == 1:
                # TODO: test
                event = children[0]
                event.timestamp -= state["dt"]
            else:
                first_child = children[0]
                last_child = children[-1]
                event = make_parent_event(
                    first_child,
                    {
                        "name": "move",
                        "mouse_x": last_child.mouse_x,
                        "mouse_y": last_child.mouse_y,
                        "timestamp": first_child.timestamp - state["dt"],
                        "children": children,
                    },
                )
                state["dt"] += last_child.timestamp - first_child.timestamp
            merged_events.append(event)
        logger.debug(f"{len(group_idx_tups)=}")
        logger.debug(f"{len(merged_events)=}")
        return merged_events

    return merge_consecutive_action_events(
        "mouse_move",
        events,
        is_target_event,
        get_merged_events,
    )


def merge_consecutive_mouse_scroll_events(
    events: list[models.ActionEvent],
) -> list[models.ActionEvent]:
    """Merge consecutive mouse scroll events into a single scroll event.

    Args:
        events (list): The list of events to process.

    Returns:
        list: The merged list of events.

    """

    def is_target_event(event: models.ActionEvent, state: dict[str, Any]) -> bool:
        return event.name == "scroll"

    def get_merged_events(
        to_merge: list[models.ActionEvent], state: dict[str, Any]
    ) -> list[models.ActionEvent]:
        state["dt"] += to_merge[-1].timestamp - to_merge[0].timestamp
        mouse_dx = sum(event.mouse_dx for event in to_merge)
        mouse_dy = sum(event.mouse_dy for event in to_merge)
        merged_event = to_merge[-1]
        merged_event.timestamp -= state["dt"]
        merged_event.mouse_dx = mouse_dx
        merged_event.mouse_dy = mouse_dy
        return [merged_event]

    return merge_consecutive_action_events(
        "mouse_scroll",
        events,
        is_target_event,
        get_merged_events,
    )


def merge_consecutive_mouse_click_events(
    events: list[models.ActionEvent],
) -> list[models.ActionEvent]:
    """Merge consecutive mouse click events into a single doubleclick event.

    Args:
        events (list): The list of events to process.

    Returns:
        list: The merged list of events.

    """

    def get_recording_attr(
        event: models.ActionEvent, attr_name: str, fallback: Callable[[], Any]
    ) -> Optional[Any]:
        attr = getattr(event.recording, attr_name) if event.recording else None
        if attr is None:
            fallback_value = fallback()
            logger.warning(f"{attr=} for {attr_name=}; using {fallback_value=}")
            attr = fallback_value
        return attr

    def is_target_event(event: models.ActionEvent, state: dict[str, Any]) -> bool:
        # TODO: parametrize button name
        return event.name == "click" and event.mouse_button_name == "left"

    def get_timestamp_mappings(
        to_merge: list[models.ActionEvent],
    ) -> tuple[dict[float, float], dict[float, float]]:
        double_click_distance = get_recording_attr(
            to_merge[0],
            "double_click_distance_pixels",
            utils.get_double_click_distance_pixels,
        )
        logger.debug(f"{double_click_distance=}")
        double_click_interval = get_recording_attr(
            to_merge[0],
            "double_click_interval_seconds",
            utils.get_double_click_interval_seconds,
        )
        logger.debug(f"{double_click_interval=}")
        press_to_press_t = {}
        press_to_release_t = {}
        prev_pressed_event = None
        for event in to_merge:
            if event.mouse_pressed:
                if prev_pressed_event:
                    dt = event.timestamp - prev_pressed_event.timestamp
                    dx = abs(event.mouse_x - prev_pressed_event.mouse_x)
                    dy = abs(event.mouse_y - prev_pressed_event.mouse_y)
                    if (
                        dt <= double_click_interval
                        and dx <= double_click_distance
                        and dy <= double_click_distance
                    ):
                        press_to_press_t[prev_pressed_event.timestamp] = event.timestamp
                prev_pressed_event = event
            elif prev_pressed_event:
                if prev_pressed_event.timestamp in press_to_release_t:
                    # should never happen
                    logger.warning("consecutive mouse release events")
                press_to_release_t[prev_pressed_event.timestamp] = event.timestamp
        return press_to_press_t, press_to_release_t

    def get_merged_events(
        to_merge: list[models.ActionEvent], state: dict[str, Any]
    ) -> list[models.ActionEvent]:
        press_to_press_t, press_to_release_t = get_timestamp_mappings(to_merge)
        t_to_event = {event.timestamp: event for event in to_merge}
        merged = []
        skip_timestamps = set()
        for event in to_merge:
            if event.timestamp in skip_timestamps:
                logger.debug(f"skipping {event.timestamp=}")
                continue
            if event.timestamp in press_to_press_t:
                # convert to doubleclick
                release_t = press_to_release_t[event.timestamp]
                next_press_t = press_to_press_t[event.timestamp]
                next_release_t = press_to_release_t[next_press_t]
                skip_timestamps.add(release_t)
                skip_timestamps.add(next_press_t)
                skip_timestamps.add(next_release_t)
                state["dt"] += next_release_t - event.timestamp
                event = make_parent_event(
                    event,
                    {
                        "name": "doubleclick",
                        "timestamp": next_release_t,
                        "mouse_x": event.mouse_x,
                        "mouse_y": event.mouse_y,
                        "mouse_button_name": event.mouse_button_name,
                        "children": [
                            event,
                            t_to_event[release_t],
                            t_to_event[next_press_t],
                            t_to_event[next_release_t],
                        ],
                    },
                )
            elif event.timestamp in press_to_release_t:
                # convert to singleclick
                release_t = press_to_release_t[event.timestamp]
                skip_timestamps.add(release_t)
                state["dt"] += release_t - event.timestamp
                event = make_parent_event(
                    event,
                    {
                        "name": "singleclick",
                        "timestamp": release_t,
                        "mouse_x": event.mouse_x,
                        "mouse_y": event.mouse_y,
                        "mouse_button_name": event.mouse_button_name,
                        "children": [event, t_to_event[release_t]],
                    },
                )
            event.timestamp -= state["dt"]
            merged.append(event)
        return merged

    return merge_consecutive_action_events(
        "mouse_click",
        events,
        is_target_event,
        get_merged_events,
    )


def remove_invalid_keyboard_events(
    events: list[models.ActionEvent],
) -> list[models.ActionEvent]:
    """Remove invalid keyboard events."""
    return [
        event
        for event in events
        # https://github.com/moses-palmer/pynput/issues/481
        if not str(event.key) == "<0>"
    ]


def merge_consecutive_keyboard_events(
    events: list[models.ActionEvent],
    group_named_keys: bool = KEYBOARD_EVENTS_MERGE_GROUP_NAMED_KEYS,
) -> list[models.ActionEvent]:
    """Merge consecutive keyboard char press events into a single press event."""

    def is_target_event(event: models.ActionEvent, state: dict[str, Any]) -> bool:
        is_target_event = bool(event.key)
        logger.debug(f"{is_target_event=} {event=}")
        return is_target_event

    def get_group_idx_tups(to_merge: list[models.ActionEvent]) -> list[tuple[int, int]]:
        pressed_keys = set()
        was_pressed = False
        start_idx = 0
        group_idx_tups = []
        for event_idx, event in enumerate(to_merge):
            assert event.name in ("press", "release"), event
            if event.key_name:
                if event.name == "press":
                    if event.key in pressed_keys:
                        logger.warning(f"{event.key=} already in {pressed_keys=}")
                    else:
                        pressed_keys.add(event.key)
                elif event.name == "release":
                    if event.key not in pressed_keys:
                        logger.warning(f"{event.key} not in {pressed_keys=}")
                    else:
                        pressed_keys.remove(event.key)
            is_pressed = bool(pressed_keys)
            group_end = was_pressed and not is_pressed
            group_start = is_pressed and not was_pressed
            logger.debug(
                f"{event_idx=} {pressed_keys=} {is_pressed=} {was_pressed=} "
                f"{group_start=} {group_end=}"
            )
            if group_start or group_end:
                end_idx = event_idx + int(group_end)
                if end_idx > start_idx:
                    group_idx_tups.append((start_idx, end_idx))
                    logger.debug(f"{group_idx_tups=}")
                start_idx = end_idx
            was_pressed = is_pressed
        if start_idx < len(to_merge) - 1:
            # TODO: test
            group_idx_tups.append((start_idx, len(to_merge)))
        logger.info(f"{len(to_merge)=} {group_idx_tups=}")
        return group_idx_tups

    def get_merged_events(
        to_merge: list[models.ActionEvent], state: dict[str, Any]
    ) -> list[models.ActionEvent]:
        if group_named_keys:
            group_idx_tups = get_group_idx_tups(to_merge)
        else:
            group_idx_tups = [(0, len(to_merge))]
        merged_events = []
        for start_idx, end_idx in group_idx_tups:
            children = to_merge[start_idx:end_idx]
            # TODO: consolidate pattern with merge_consecutive_mouse_move_events
            if len(children) == 1:
                # TODO: test
                merged_event = children[0]
                merged_event.timestamp -= state["dt"]
            else:
                first_child = children[0]
                last_child = children[-1]
                merged_event = make_parent_event(
                    first_child,
                    {
                        "name": "type",
                        "timestamp": first_child.timestamp - state["dt"],
                        "children": children,
                    },
                )
                state["dt"] += last_child.timestamp - first_child.timestamp
            merged_events.append(merged_event)
        return merged_events

    return merge_consecutive_action_events(
        "keyboard",
        events,
        is_target_event,
        get_merged_events,
    )


def remove_redundant_mouse_move_events(
    events: list[models.ActionEvent],
) -> list[models.ActionEvent]:
    """Remove mouse move events that don't change the mouse position."""

    def is_target_event(event: models.ActionEvent, state: dict[str, Any]) -> bool:
        return event.name in ("move", "click")

    def is_same_pos(e0: models.ActionEvent, e1: models.ActionEvent) -> bool:
        if not all([e0, e1]):
            return False
        for attr in ("mouse_x", "mouse_y"):
            val0 = getattr(e0, attr)
            val1 = getattr(e1, attr)
            if val0 != val1:
                return False
        return True

    def should_discard(
        event: models.ActionEvent,
        prev_event: models.ActionEvent | None,
        next_event: models.ActionEvent | None,
    ) -> bool:
        return event.name == "move" and (
            is_same_pos(prev_event, event) or is_same_pos(event, next_event)
        )

    def get_merged_events(
        to_merge: list[models.ActionEvent], state: dict[str, Any]
    ) -> list[models.ActionEvent]:
        to_merge = [None, *to_merge, None]
        merged_events = []
        dts = []
        children = []
        for idx, (prev_event, event, next_event) in enumerate(
            zip(
                to_merge,
                to_merge[1:],
                to_merge[2:],
            )
        ):
            if should_discard(event, prev_event, next_event):
                if prev_event:
                    dt = event.timestamp - prev_event.timestamp
                else:
                    dt = next_event.timestamp - event.timestamp
                state["dt"] += dt
                children.append(event)
            else:
                dts.append(state["dt"])
                if children:
                    event.children = children
                    children = []
                merged_events.append(event)

        # update timestamps (doing this in the previous loop double counts)
        assert len(dts) == len(merged_events), (
            len(dts),
            len(merged_events),
        )
        for event, dt in zip(merged_events, dts):
            event.timestamp -= dt

        return merged_events

    return merge_consecutive_action_events(
        "redundant_mouse_move",
        events,
        is_target_event,
        get_merged_events,
    )


# TODO: consolidate with remove_redundant_mouse_move_events above
# these are very similar except for the lines noted below
def remove_move_before_click(
    events: list[models.ActionEvent],
) -> list[models.ActionEvent]:
    """Remove mouse move move immediately followed by click in the same location."""

    def is_target_event(event: models.ActionEvent, state: dict[str, Any]) -> bool:
        return event.name in ("move", "click", "singleclick", "doubleclick")

    def is_same_pos(e0: models.ActionEvent, e1: models.ActionEvent) -> bool:
        if not all([e0, e1]):
            return False
        for attr in ("mouse_x", "mouse_y"):
            val0 = getattr(e0, attr)
            val1 = getattr(e1, attr)
            # this line is distinct from remove_redundant_mouse_move_events
            if abs(val0 - val1) > MOUSE_MOVE_EVENT_MERGE_CLICK_DISTANCE_THRESHOLD:
                return False
        return True

    def should_discard(
        event: models.ActionEvent,
        prev_event: models.ActionEvent | None,
        next_event: models.ActionEvent | None,
    ) -> bool:
        # import ipdb; ipdb.set_trace()
        return event.name == "move" and (
            # this line is distinct from remove_redundant_mouse_move_events
            is_same_pos(event, next_event)
        )

    def get_merged_events(
        to_merge: list[models.ActionEvent], state: dict[str, Any]
    ) -> list[models.ActionEvent]:
        to_merge = [None, *to_merge, None]
        merged_events = []
        dts = []
        children = []
        for idx, (prev_event, event, next_event) in enumerate(
            zip(
                to_merge,
                to_merge[1:],
                to_merge[2:],
            )
        ):
            if should_discard(event, prev_event, next_event):
                if prev_event:
                    dt = event.timestamp - prev_event.timestamp
                else:
                    dt = next_event.timestamp - event.timestamp
                state["dt"] += dt
                children.append(event)
            else:
                dts.append(state["dt"])
                if children:
                    event.children = children
                    children = []
                merged_events.append(event)

        # update timestamps (doing this in the previous loop double counts)
        assert len(dts) == len(merged_events), (
            len(dts),
            len(merged_events),
        )
        for event, dt in zip(merged_events, dts):
            event.timestamp -= dt

        return merged_events

    return merge_consecutive_action_events(
        "remove_move_before_click",
        events,
        is_target_event,
        get_merged_events,
    )


def merge_consecutive_action_events(
    name: str,
    events: list[models.ActionEvent],
    is_target_event: Callable[..., bool],
    get_merged_events: Callable[..., list[models.ActionEvent]],
) -> list[models.ActionEvent]:
    """Merge consecutive action events into one or more parent events."""
    num_events_before = len(events)
    state = {"dt": 0}
    rval = []
    to_merge = []

    def include_merged_events(
        to_merge: list[models.ActionEvent],
    ) -> None:
        merged_events = get_merged_events(to_merge, state)
        for merged_event in merged_events:
            merged_event.reducer_names.add(name)
        rval.extend(merged_events)
        to_merge.clear()

    for event in events:
        assert event.name in common.ALL_EVENTS, event
        if is_target_event(event, state):
            to_merge.append(event)
        else:
            if to_merge:
                include_merged_events(to_merge)
            event.timestamp -= state["dt"]
            rval.append(event)

    if to_merge:
        include_merged_events(to_merge)

    num_events_after = len(rval)
    num_events_removed = num_events_before - num_events_after
    logger.info(f"{name=} {num_events_removed=}")

    return rval


def discard_unused_events(
    referred_events: list[models.ActionEvent],
    action_events: list[models.ActionEvent],
    referred_timestamp_key: str,
) -> list[models.ActionEvent]:
    """Discard unused events based on the referred timestamp key.

    Args:
        referred_events (list): The list of referred events.
        action_events (list): The list of action events.
        referred_timestamp_key (str): The key representing the timestamp
          in referred events.

    Returns:
        list: The filtered list of referred events.
    """
    referred_event_timestamps = set(
        [
            getattr(action_event, referred_timestamp_key)
            for action_event in action_events
        ]
    )
    num_referred_events_before = len(referred_events)
    referred_events = [
        referred_event
        for referred_event in referred_events
        if referred_event.timestamp in referred_event_timestamps
    ]
    num_referred_events_after = len(referred_events)
    num_referred_events_removed = num_referred_events_before - num_referred_events_after
    logger.debug(f"{referred_timestamp_key=} {num_referred_events_removed=}")
    return referred_events


def process_events(
    action_events: list[models.ActionEvent],
    window_events: list[models.WindowEvent],
    screenshots: list[models.Screenshot],
) -> tuple[
    list[models.ActionEvent],
    list[models.WindowEvent],
    list[models.Screenshot],
]:
    """Process action events, window events, and screenshots.

    Args:
        action_events (list): The list of action events.
        window_events (list): The list of window events.
        screenshots (list): The list of screenshots.

    Returns:
        tuple: A tuple containing the processed action events, window events,
          and screenshots.
    """
    # For debugging
    # _action_events = action_events
    # _window_events = window_events
    # _screenshots = screenshots

    num_action_events = len(action_events)
    num_window_events = len(window_events)
    num_screenshots = len(screenshots)
    num_total = num_action_events + num_window_events + num_screenshots
    logger.info(
        f"before {num_action_events=} {num_window_events=} {num_screenshots=} "
        f"{num_total=}"
    )
    process_fns = [
        remove_invalid_keyboard_events,
        remove_redundant_mouse_move_events,
        merge_consecutive_keyboard_events,
        merge_consecutive_mouse_move_events,
        merge_consecutive_mouse_scroll_events,
        merge_consecutive_mouse_click_events,
        # this causes clicks to fail to be registered in NaiveReplayStrategy
        # TODO: remove
        # remove_move_before_click,
    ]
    for process_fn in process_fns:
        action_events = process_fn(action_events)
        # TODO: keep events in which window_event_timestamp is updated
        for prev_event, event in zip(action_events, action_events[1:]):
            try:
                assert prev_event.timestamp <= event.timestamp, (
                    process_fn,
                    prev_event,
                    event,
                )
            except AssertionError as exc:
                logger.exception(exc)
                import ipdb

                ipdb.set_trace()
        window_events = discard_unused_events(
            window_events,
            action_events,
            "window_event_timestamp",
        )
        screenshots = discard_unused_events(
            screenshots,
            action_events,
            "screenshot_timestamp",
        )
    num_action_events_ = len(action_events)
    num_window_events_ = len(window_events)
    num_screenshots_ = len(screenshots)
    num_total_ = num_action_events_ + num_window_events_ + num_screenshots_
    pct_action_events = num_action_events_ / num_action_events
    pct_window_events = num_window_events_ / num_window_events
    pct_screenshots = num_screenshots_ / num_screenshots
    pct_total = num_total_ / num_total
    logger.info(
        f"after {num_action_events_=} {num_window_events_=} {num_screenshots_=} "
        f"{num_total_=}"
    )
    logger.info(
        f"{pct_action_events=} {pct_window_events=} {pct_screenshots=} {pct_total=}"
    )
    return action_events, window_events, screenshots
