"""
Implements a ReplayStrategy mixin for converting images to ASCII.

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

    def __init__(
        self,
        recording: Recording,
    ):
        super().__init__(recording)

    def get_ascii_text(
        self,
        screenshot: Screenshot,
        monochrome: bool = MONOCHROME,
        columns: int = COLUMNS,
        width_ratio: float = WIDTH_RATIO,

    ):
        ascii_art = AsciiArt.from_pillow_image(screenshot.image)
        ascii_text = ascii_art.to_ascii(
            monochrome=monochrome,
            columns=columns,
            width_ratio=width_ratio,
        )
        logger.debug(f"ascii_text=\n{ascii_text}")

        return ascii_text