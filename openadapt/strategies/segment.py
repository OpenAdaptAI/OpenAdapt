"""Extends vanilla strategy to use segmentation to convert description -> coordinate.

Uses FastSAM for segmentation.
"""

from pprint import pformat

from loguru import logger

from openadapt import adapters, common, models, strategies, utils
from openadapt.strategies.visual import (
    add_active_segment_descriptions,
    get_window_segmentation,
    apply_replay_instructions,
)


INCLUDE_RAW_RECORDING = False
INCLUDE_RAW_RECORDING_DESCRIPTION = False
INCLUDE_MODIFIED_RECORDING = True
INCLUDE_MODIFIED_RECORDING_DESCRIPTION = False
INCLUDE_REPLAY_INSTRUCTIONS = False
INCLUDE_WINDOW = False
INCLUDE_WINDOW_DATA = False
FILTER_MASKS = True
INCLUDE_CURRENT_SCREENSHOT = False


class SegmentReplayStrategy(strategies.base.BaseReplayStrategy):
    """Segment replay strategy that performs segmentation in addition to vanilla."""

    def __init__(
        self,
        recording: models.Recording,
        instructions: str = "",
    ) -> None:
        """Initialize the SegmentReplayStrategy.

        Args:
            recording (models.Recording): The recording object.
            instructions(str): Natural language instructions
                for how recording should be replayed.
        """
        super().__init__(recording)
        self.replay_instructions = instructions
        self.action_history = []
        self.action_event_idx = 0

        add_active_segment_descriptions(recording.processed_action_events)
        self.modified_actions = apply_replay_instructions(
            recording.processed_action_events,
            self.replay_instructions,
        )

        if INCLUDE_RAW_RECORDING_DESCRIPTION:
            self.recording_description = describe_recording(
                self.recording.processed_action_events
            )
        else:
            self.recording_description = None

        if INCLUDE_MODIFIED_RECORDING_DESCRIPTION:
            self.modified_recording_description = describe_recording(
                self.modified_actions
            )
        else:
            self.modified_recording_description = None

    def get_next_action_event(
        self,
        screenshot: models.Screenshot,
        window_event: models.WindowEvent,
        include_raw_recording: bool = INCLUDE_RAW_RECORDING,
        include_raw_recording_description: bool = INCLUDE_RAW_RECORDING_DESCRIPTION,
        include_modified_recording: bool = INCLUDE_MODIFIED_RECORDING,
        include_modified_recording_description: bool = (
            INCLUDE_MODIFIED_RECORDING_DESCRIPTION
        ),
        include_active_window: bool = INCLUDE_WINDOW,
        include_active_window_data: bool = INCLUDE_WINDOW_DATA,
        include_replay_instructions: bool = INCLUDE_REPLAY_INSTRUCTIONS,
        include_current_screenshot: bool = INCLUDE_CURRENT_SCREENSHOT,
    ) -> models.ActionEvent | None:
        """Get the next ActionEvent for replay.

        Args:
            screenshot (models.Screenshot): The screenshot object.
            window_event (models.WindowEvent): The window event object.
            include_raw_recording (bool): Whether to include the raw recording in the
                prompt.
            include_raw_recording_description (bool): Whether to include the raw
                recording description in the prompt.
            include_modified_recording (bool): Whether to include the modified
                recording in the prompt.
            include_modified_recording_description (bool): Whether to include the
                modified recording description in the prompt.
            include_active_window (bool): Whether to include window metadata in the
                prompt.
            include_active_window_data (bool): Whether to retain window a11y data in
                the prompt.
            include_replay_instructions (bool): Whether to include replay instructions
                in the prompt.
            include_current_screenshot (bool): Whether to include the current screenshot
                in the prompt.

        Returns:
            models.ActionEvent or None: The next ActionEvent for replay or None
              if there are no more events.
        """
        reference_actions = self.recording.processed_action_events
        num_action_events = max(
            len(reference_actions),
            len(self.modified_actions),
        )
        self.action_event_idx += 1
        if self.action_event_idx >= num_action_events:
            raise StopIteration()
        logger.debug(f"{self.action_event_idx=} of {num_action_events=}")

        generated_action_event = generate_action_event(
            screenshot,
            window_event,
            reference_actions,
            self.modified_actions,
            self.action_history,
            self.replay_instructions,
            self.recording_description,
            self.modified_recording_description,
            include_raw_recording,
            include_raw_recording_description,
            include_modified_recording,
            include_modified_recording_description,
            include_active_window,
            include_active_window_data,
            include_replay_instructions,
            include_current_screenshot,
        )
        if not generated_action_event:
            raise StopIteration()

        # convert segment -> coordinate
        # (based on visual.py)
        active_window = models.WindowEvent.get_active_window_event(
            include_active_window_data
        )

        active_screenshot = models.Screenshot.take_screenshot()
        logger.info(f"{active_window=}")

        if (
            generated_action_event.name in common.MOUSE_EVENTS
            and generated_action_event.active_segment_description
        ):
            generated_action_event.screenshot = active_screenshot
            generated_action_event.window_event = active_window
            generated_action_event.recording = self.recording
            exceptions = []
            while True:
                active_window_segmentation = get_window_segmentation(
                    generated_action_event,
                    exceptions=exceptions,
                )
                try:
                    target_segment_idx = active_window_segmentation.descriptions.index(
                        generated_action_event.active_segment_description
                    )
                except ValueError as exc:
                    exceptions.append(exc)
                    # TODO XXX this does not update the prompts, even though it should
                    logger.exception(exc)
                    import ipdb; ipdb.set_trace()
                    logger.warning(f"{exc=} {len(exceptions)=}")
                else:
                    break
            target_centroid = active_window_segmentation.centroids[target_segment_idx]
            # <image space position> = scale_ratio * <window/action space position>
            width_ratio, height_ratio = utils.get_scale_ratios(generated_action_event)
            target_mouse_x = target_centroid[0] / width_ratio + active_window.left
            target_mouse_y = target_centroid[1] / height_ratio + active_window.top
            generated_action_event.mouse_x = target_mouse_x
            generated_action_event.mouse_y = target_mouse_y
        else:
            # just click wherever the mouse already is
            pass

        self.action_history.append(generated_action_event)
        return generated_action_event

    def __del__(self) -> None:
        """Log the action history."""
        action_history_dicts = [
            action.to_prompt_dict() for action in self.action_history
        ]
        logger.info(f"action_history=\n{pformat(action_history_dicts)}")


def describe_recording(
    action_events: list[models.ActionEvent],
    include_window: bool = INCLUDE_WINDOW,
    include_window_data: bool = INCLUDE_WINDOW_DATA,
) -> str:
    """Generate a natural language description of the actions in the recording.

    Given the recorded states and actions, describe what happened.

    Args:
        action_events (list[models.ActionEvent]): the list of actions to describe.
        include_window (bool): flag indicating whether to include window metadata.
        include_window_data (bool): flag indicating whether to include accessibility
            API data in each window event.

    Returns:
        (str) natural language description of the what happened in the recording.
    """
    action_dicts = [action.to_prompt_dict() for action in action_events]
    window_dicts = [
        (
            action.window_event.to_prompt_dict(include_window_data)
            # this may be a modified action, in which case there is no window event
            if action.window_event
            else {}
        )
        for action in action_events
    ]
    action_window_dicts = [
        {
            "action": action_dict,
            "window": window_dict if include_window else {},
        }
        for action_dict, window_dict in zip(action_dicts, window_dicts)
    ]
    images = [action.screenshot.image for action in action_events if action.screenshot]
    system_prompt = utils.render_template_from_file(
        "prompts/system.j2",
    )
    prompt = utils.render_template_from_file(
        "prompts/describe_recording.j2",
        action_windows=action_window_dicts,
    )
    prompt_adapter = adapters.get_default_prompt_adapter()
    recording_description = prompt_adapter.prompt(
        prompt,
        images=images,
        system_prompt=system_prompt,
    )
    return recording_description


def generate_action_event(
    current_screenshot: models.Screenshot,
    current_window_event: models.WindowEvent,
    recorded_actions: list[models.ActionEvent],
    modified_actions: list[models.ActionEvent],
    replayed_actions: list[models.ActionEvent],
    replay_instructions: str,
    recording_description: str,
    modified_recording_description: str,
    include_raw_recording: bool,
    include_raw_recording_description: bool,
    include_modified_recording: bool,
    include_modified_recording_description: bool,
    include_active_window: bool,
    include_active_window_data: bool,
    include_replay_instructions: str,
    include_current_screenshot: bool,
) -> models.ActionEvent:
    """Modify the given ActionEvents according to the given replay instructions.

    Given the description of what happened, proposed modifications in natural language
    instructions, the current state, and the actions produced so far, produce the next
    action.

    Args:
        current_screenshot (models.Screenshot): current state screenshot
        current_window_event (models.WindowEvent): current state window data
        recorded_actions (list[models.ActionEvent]): list of action events from the
            recording
        replayed_actions (list[models.ActionEvent]): list of actions produced during
            current replay
        replay_instructions (str): proposed modifications in natural language
            instructions
        include_raw_recording (bool): Whether to include the raw recording in the
            prompt.
        include_raw_recording_description (bool): Whether to include the raw
            recording description in the prompt.
        include_modified_recording (bool): Whether to include the modified
            recording in the prompt.
        include_modified_recording_description (bool): Whether to include the
            modified recording description in the prompt.
        include_active_window (bool): Whether to include window metadata in the
            prompt.
        include_active_window_data (bool): Whether to retain window a11y data in
            the prompt.
        include_replay_instructions (bool): Whether to include replay instructions
            in the prompt.
        include_current_screenshot (bool): Whether to include the current screenshot
            in the prompt.

    Returns:
        (models.ActionEvent) the next action event to be played, produced by the model
    """
    current_image = current_screenshot.image
    current_window_dict = current_window_event.to_prompt_dict(
        include_active_window_data,
    )
    recorded_action_dicts = [action.to_prompt_dict() for action in recorded_actions]
    replayed_action_dicts = [action.to_prompt_dict() for action in replayed_actions]
    modified_action_dicts = [action.to_prompt_dict() for action in modified_actions]

    system_prompt = utils.render_template_from_file(
        "prompts/system.j2",
    )
    prompt = utils.render_template_from_file(
        "prompts/generate_action_event--segment.j2",
        current_window=current_window_dict,
        recorded_actions=recorded_action_dicts,
        modified_actions=modified_action_dicts,
        replayed_actions=replayed_action_dicts,
        replay_instructions=replay_instructions,
        recording_description=recording_description,
        modified_recording_description=modified_recording_description,
        include_raw_recording=include_raw_recording,
        include_raw_recording_description=include_raw_recording_description,
        include_modified_recording=include_modified_recording,
        include_modified_recording_description=include_modified_recording_description,
        include_active_window=include_active_window,
        include_replay_instructions=include_replay_instructions,
    )
    prompt_adapter = adapters.get_default_prompt_adapter()
    images = [current_image] if include_current_screenshot else []
    content = prompt_adapter.prompt(
        prompt,
        images=images,
        system_prompt=system_prompt,
    )
    action_dict = utils.parse_code_snippet(content)
    logger.info(f"{action_dict=}")
    if not action_dict:
        # allow early stopping
        return None
    action = models.ActionEvent.from_dict(action_dict)
    logger.info(f"{action=}")
    return action
