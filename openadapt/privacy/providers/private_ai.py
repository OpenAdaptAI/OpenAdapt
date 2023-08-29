"""A Module for Private AI Scrubbing Provider."""

from typing import List

from loguru import logger
import requests

from openadapt import config
from openadapt.privacy.base import Modality, ScrubbingProvider, TextScrubbingMixin
from openadapt.privacy.providers import ScrubProvider


class PrivateAIScrubbingProvider(
    ScrubProvider, ScrubbingProvider, TextScrubbingMixin
):  # pylint: disable=abstract-method
    """A Class for Private AI Scrubbing Provider."""

    name: str = ScrubProvider.COMPREHEND
    capabilities: List[Modality] = [Modality.TEXT, Modality.PIL_IMAGE, Modality.PDF]

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        """Scrub the text of all PII/PHI.

        Args:
            text (str): Text to be scrubbed
            is_separated (bool): Whether the text is separated with special characters

        Returns:
            str: Scrubbed text
        """
