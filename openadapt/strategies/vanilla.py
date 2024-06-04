"""Implements a vanilla playback strategy, offloading everything to the model.

Send in a series of screenshots to GPT-4 and then ask GPT-4 to describe what happened.
Then give it the sequence of actions (in concrete coordinates and keyboard inputs),
as well as your proposed modification in natural language.
Ask it to output the new action sequence.
    --LunjunZhang
"""

import time

from loguru import logger

from openadapt import models, strategies, utils
from openadapt.config import config


PROCESS_EVENTS = True


class VanillaReplayStrategy(strategies.base.BaseReplayStrategy):
    """Vanilla replay strategy that replays ActionEvents modified by an LLM directly.

    If AGI or GPT6 happens, this script should be able to suddenly do the work.
		--LunjunZhang
    """

    def __init__(
        self,
        recording: models.Recording,
        replay_instructions: str,
        process_events: bool = PROCESS_EVENTS,
    ) -> None:
        """Initialize the VanillaReplayStrategy.

        Args:
            recording (models.Recording): The recording object.
            replay_instructions (str): Natural language instructions
                for how recording should be replayed.
            process_events (bool): Flag indicating whether to process the events.
              Defaults to True.
        """
        super().__init__(recording)
        self.replay_instructions = replay_instructions
        self.process_events = process_events

        if self.process_events:
            action_events = self.recording.processed_action_events
        else:
            action_events = self.recording.action_events
        self.modified_actions = apply_replay_instructions(
            action_events,
            self.replay_instructions,
        )

    def get_next_action_event(
        self,
        screenshot: models.Screenshot,
        window_event: models.WindowEvent,
    ) -> models.ActionEvent | None:
        """Get the next ActionEvent for replay.

        Args:
            screenshot (models.Screenshot): The screenshot object.
            window_event (models.WindowEvent): The window event object.

        Returns:
            models.ActionEvent or None: The next ActionEvent for replay or None
              if there are no more events.
        """
        action_events = self.modified_actions
        self.action_event_idx += 1
        num_action_events = len(action_events)
        if self.action_event_idx >= num_action_events:
            # TODO: refactor
            raise StopIteration()
        action_event = action_events[self.action_event_idx]
        if config.REPLAY_STRIP_ELEMENT_STATE:
            action_event = utils.strip_element_state(action_event)
        logger.debug(
            f"{self.action_event_idx=} of {num_action_events=}: {action_event=}"
        )
        if self.display_events:
            image = utils.display_event(action_event)
            image.show()
        if self.replay_events:
            if self.sleep and self.prev_timestamp:
                # TODO: subtract processing time
                sleep_time = action_event.timestamp - self.prev_timestamp
                logger.info(f"{sleep_time=} {action_event.timestamp}")
                time.sleep(sleep_time)
            self.prev_timestamp = action_event.timestamp

            # without this, clicks may occur too quickly to be registered correctly
            # (fixed by disabling remove_move_before_click in events.py)
            # if action_event.name in common.MOUSE_CLICK_EVENTS:
            #    time.sleep(self.double_click_interval_seconds + 0.01)

            return action_event
        else:
            return None


# TODO XXX copied from visual.py
def apply_replay_instructions(
    action_events: list[models.ActionEvent],
    replay_instructions: str,
) -> None:
    """Modify the given ActionEvents according to the given replay instructions.

    Args:
        action_events: list of action events to be modified in place.
        replay_instructions: instructions for how action events should be modified.
    """
    action_dicts = [get_action_prompt_dict(action) for action in action_events]
    actions_dict = {"actions": action_dicts}
    system_prompt = utils.render_template_from_file(
        "prompts/system.j2",
    )
    prompt = utils.render_template_from_file(
        "prompts/apply_replay_instructions--vanilla.j2",
        actions=actions_dict,
        replay_instructions=replay_instructions,
    )
    prompt_adapter = adapters.get_default_prompt_adapter()
    content = prompt_adapter.prompt(
        prompt,
        system_prompt,
    )
    content_dict = utils.parse_code_snippet(content)
    try:
        action_dicts = content_dict["actions"]
    except TypeError as exc:
        logger.warning(exc)
        # sometimes OpenAI returns a list of dicts directly, so let it slide
        action_dicts = content_dict
    modified_actions = []
    for action_dict in action_dicts:
        action = models.ActionEvent.from_dict(action_dict)
        modified_actions.append(action)
    return modified_actions


# TODO copied from visual.py
def get_action_prompt_dict(action: models.ActionEvent) -> dict:
    """Convert an ActionEvent into a dict, excluding unnecessary properties.

    Args:
        action: the ActionEvent to convert

    Returns:
        dictionary containing relevant properties from the given ActionEvent.
    """
    action_dict = deepcopy(
        {
            key: val
            for key, val in utils.row2dict(action, follow=False).items()
            if val is not None
            and not key.endswith("timestamp")
            and not key.endswith("id")
            and key not in ["reducer_names"]
            # and not isinstance(getattr(models.ActionEvent, key), property)
        }
    )
    if action.active_segment_description:
        for key in ("mouse_x", "mouse_y", "mouse_dx", "mouse_dy"):
            if key in action_dict:
                del action_dict[key]
    if action.available_segment_descriptions:
        action_dict["available_segment_descriptions"] = (
            action.available_segment_descriptions
        )
    return action_dict
