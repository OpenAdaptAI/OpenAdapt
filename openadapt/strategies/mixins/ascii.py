"""Implements a ReplayStrategy mixin for converting images to ASCII.

Usage:

    class MyReplayStrategy(ASCIIReplayStrategyMixin):
        ...
"""

from ascii_magic import AsciiArt
from loguru import logger

from openadapt.models import Recording, Screenshot
from openadapt.strategies.base import BaseReplayStrategy

COLUMNS = 120
WIDTH_RATIO = 2.2
MONOCHROME = True


class ASCIIReplayStrategyMixin(BaseReplayStrategy):
    """ReplayStrategy mixin for converting images to ASCII."""

    def __init__(
        self,
        recording: Recording,
    ) -> None:
        """Initialize the ASCIIReplayStrategyMixin.

        Args:
            recording (Recording): The recording to replay.
        """
        super().__init__(recording)

    def get_ascii_text(
        self,
        screenshot: Screenshot,
        monochrome: bool = MONOCHROME,
        columns: int = COLUMNS,
        width_ratio: float = WIDTH_RATIO,
    ) -> str:
        """Convert the screenshot image to ASCII text.

        Args:
            screenshot (Screenshot): The screenshot to convert.
            monochrome (bool): Flag to indicate monochrome conversion (default: True).
            columns (int): Number of columns for the ASCII text (default: 120).
            width_ratio (float): Width ratio for the ASCII text (default: 2.2).

        Returns:
            str: The ASCII representation of the screenshot image.
        """
        ascii_art = AsciiArt.from_pillow_image(screenshot.image)
        ascii_text = ascii_art.to_ascii(
            monochrome=monochrome,
            columns=columns,
            width_ratio=width_ratio,
        )
        logger.debug(f"ascii_text=\n{ascii_text}")
        return ascii_text
