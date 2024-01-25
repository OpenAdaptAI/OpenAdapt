"""Large Multimodal Model with Set-of-Mark (SOM) prompting

Usage:

    $ python -m openadapt.replay SOMReplayStrategy
"""

from loguru import logger
from PIL import Image

from openadapt import models, strategies, utils
from openadapt.strategies.mixins.openai import OpenAIReplayStrategyMixin
from openadapt.strategies.mixins.sam import SAMReplayStrategyMixin


class SOMReplayStrategy(
    OpenAIReplayStrategyMixin,
    SAMReplayStrategyMixin,
    strategies.base.BaseReplayStrategy,
):
    """LMM with Set-of-Mark prompting."""

    def __init__(
        self,
        recording: models.Recording,
    ) -> None:
        """Initialize the SOMReplayStrategy.

        Args:
            recording (models.Recording): The recording object.
        """
        super().__init__(recording)

        self.base64_screenshots = [
            utils.image2tf8(action_event.screenshot.image)
            for action_event in recording.processed_action_events
        ]
        self._som_image = None

        """
        self.som_images = [
            self.get_som_image(action_event.screenshot.image)
            for action_event in recording.processed_action_events
        ]
        self.base64_som_images = [
            utils.image2utf8(som_image)
            for som_image in self.som_images
        ]
        """

    def get_som_image(self, image: Image.Image) -> Image.Image:
        if not self._som_image:
            mask_image = self.get_segmentation(image)
            number_mask = self.get_number_mask(mask_image)
            self._som_image = apply_number_mask(image, number_mask)
          return self._som_image

        # convert to masks

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
        completion = self.get_completion(prompt, system_message, base64_images)
        active_action_dicts = get_action_dict_from_completion(completion)
        logger.debug(f"active_action_dicts=\n{pformat(active_action_dicts)}")
        active_action = models.ActionEvent.from_children(active_action_dicts)
        self.recording_action_idx += 1
        return active_action
