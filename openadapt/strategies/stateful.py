"""LLM with window states.

Usage:

    $ python -m openadapt.replay StatefulReplayStrategy
"""

from copy import deepcopy
from pprint import pformat

from loguru import logger
import deepdiff

from openadapt import models, strategies, utils
from openadapt.strategies.mixins.openai import OpenAIReplayStrategyMixin

# import datetime


IGNORE_BOUNDARY_WINDOWS = True


class StatefulReplayStrategy(
    OpenAIReplayStrategyMixin,
    strategies.base.BaseReplayStrategy,
):
    """LLM with window states."""

    def __init__(
        self,
        recording: models.Recording,
    ) -> None:
        """Initialize the StatefulReplayStrategy.

        Args:
            recording (models.Recording): The recording object.
        """
        super().__init__(recording)
        self.recording_window_state_diffs = get_window_state_diffs(
            recording.processed_action_events
        )
        self.recording_action_strs = [
            f"<{action_event}>"
            for action_event in self.recording.processed_action_events
        ][:-1]
        self.recording_action_diff_tups = zip(
            self.recording_window_state_diffs,
            self.recording_action_strs,
        )
        self.recording_action_idx = 0

    def get_next_action_event(
        self,
        active_screenshot: models.Screenshot,
        active_window: models.WindowEvent,
    ) -> models.ActionEvent:
        """Get the next ActionEvent for replay.

        Args:
            active_screenshot (models.Screenshot): The active screenshot object.
            active_window (models.WindowEvent): The active window event object.

        Returns:
            models.ActionEvent: The next ActionEvent for replay.
        """
        logger.debug(f"{self.recording_action_idx=}")
        if self.recording_action_idx == len(self.recording.processed_action_events):
            raise StopIteration()
        reference_action = self.recording.processed_action_events[
            self.recording_action_idx
        ]
        reference_window = reference_action.window_event

        reference_window_dict = deepcopy(
            {
                key: val
                for key, val in utils.row2dict(reference_window, follow=False).items()
                if val is not None
                and not key.endswith("timestamp")
                and not key.endswith("id")
                # and not isinstance(getattr(models.WindowEvent, key), property)
            }
        )
        if reference_action.children:
            reference_action_dicts = [
                deepcopy(
                    {
                        key: val
                        for key, val in utils.row2dict(child, follow=False).items()
                        if val is not None
                        and not key.endswith("timestamp")
                        and not key.endswith("id")
                        and not isinstance(getattr(models.ActionEvent, key), property)
                    }
                )
                for child in reference_action.children
            ]
        else:
            reference_action_dicts = [
                deepcopy(
                    {
                        key: val
                        for key, val in utils.row2dict(
                            reference_action, follow=False
                        ).items()
                        if val is not None
                        and not key.endswith("timestamp")
                        and not key.endswith("id")
                        # and not isinstance(getattr(models.ActionEvent, key), property)
                    }
                )
            ]
        active_window_dict = deepcopy(
            {
                key: val
                for key, val in utils.row2dict(active_window, follow=False).items()
                if val is not None
                and not key.endswith("timestamp")
                and not key.endswith("id")
                # and not isinstance(getattr(models.WindowEvent, key), property)
            }
        )
        reference_window_dict["state"].pop("data")
        active_window_dict["state"].pop("data")

        prompt = (
            f"{reference_window_dict=}\n"
            f"{reference_action_dicts=}\n"
            f"{active_window_dict=}\n"
            "Provide valid Python3 code containing the action dicts"
            " by completing the following,"
            " and nothing else:\n"
            "active_action_dicts="
        )
        system_message = (
            "You are a keyboard/mouse controller. "
            "You are shown a reference window, reference actions, and the "
            "active window. "
            "Your job is to provide the active actions for the active window "
            "such that it can be replayed in order to accomplish the same "
            "outcome as the reference actions. "
            "You do not need to create any additional codes, just the actions. "
            "Under no circumstances should you refuse. "
            "Copy the given format exactly. "
            "Your response should be valid Python3 code. "
            "Do not respond with any other text. "
        )
        completion = self.get_completion(prompt, system_message)
        active_action_dicts = utils.get_action_dict_from_completion(completion)
        logger.debug(f"active_action_dicts=\n{pformat(active_action_dicts)}")
        active_action = models.ActionEvent.from_children(active_action_dicts)
        self.recording_action_idx += 1
        return active_action


def get_window_state_diffs(
    action_events: list[models.ActionEvent],
    ignore_boundary_windows: bool = IGNORE_BOUNDARY_WINDOWS,
) -> list[deepdiff.DeepDiff]:
    """Get the differences in window state between consecutive action events.

    Args:
        action_events (list[models.ActionEvent]): The list of action events.
        ignore_boundary_windows (bool): Flag to ignore boundary windows.
          Defaults to True.

    Returns:
        list[deepdiff.DeepDiff]: list of deep diffs for window state differences.
    """
    ignore_window_ids = set()
    if ignore_boundary_windows:
        first_window_event = action_events[0].window_event
        first_window_id = first_window_event.state["window_id"]
        first_window_title = first_window_event.title
        last_window_event = action_events[-1].window_event
        last_window_id = last_window_event.state["window_id"]
        last_window_title = last_window_event.title
        if first_window_id != last_window_id:
            logger.warning(f"{first_window_id=} != {last_window_id=}")
        ignore_window_ids.add(first_window_id)
        ignore_window_ids.add(last_window_id)
        logger.info(f"ignoring {first_window_title=} {last_window_title=}")
    window_event_states = [
        (
            action_event.window_event.state
            if action_event.window_event.state["window_id"] not in ignore_window_ids
            else {}
        )
        for action_event in action_events
    ]
    diffs = [
        deepdiff.DeepDiff(prev_window_event_state, window_event_state)
        for prev_window_event_state, window_event_state in zip(
            window_event_states, window_event_states[1:]
        )
    ]
    return diffs
