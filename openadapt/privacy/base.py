"""The Base (Parent) Class for Privacy Scrubbing Providers"""

from typing import List

from PIL import Image
from pydantic import BaseModel

from openadapt import config


class Modality:
    """A Base Class for Modality Types"""

    TEXT = "TEXT"
    PIL_IMAGE = "PIL_IMAGE"
    PDF = "PDF"
    MP4 = "MP4"


class ScrubbingProvider(BaseModel):
    """A Base Class for Scrubbing Providers"""

    name: str
    capabilities: List[str]

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        """Scrub the text of all PII/PHI.

        Args:
            text (str): Text to be scrubbed
            is_separated (bool): Whether the text is separated with special characters

        Returns:
            str: Scrubbed text
        """
        raise NotImplementedError

    def scrub_image(
        self, image: Image, fill_color: int = config.SCRUB_FILL_COLOR
    ) -> Image:
        """Scrub the image of all PII/PHI.

        Args:
            image (Image): A PIL.Image object to be scrubbed
            fill_color (int): The color used to fill the redacted regions(BGR).

        Returns:
            Image: The scrubbed image with PII and PHI removed.
        """
        raise NotImplementedError

    def scrub_pdf(self, path_to_pdf: str) -> str:
        """Scrub the PDF of all PII/PHI.

        Args:
            path_to_pdf (str): Path to the PDF to be scrubbed

        Returns:
            str: Path to the scrubbed PDF
        """
        raise NotImplementedError

    def scrub_mp4(
        self,
        mp4_file: str,
        scrub_all_entities: bool = False,
        playback_speed_multiplier: float = 1.0,
        crop_start_time: int = 0,
        crop_end_time: int | None = None,
    ):
        """Scrub a mp4 file.

        Args:
            mp4_file_path: Path to the mp4 file.
            scrub_all_entities: True/False. If true, scrubs all entities
            playback_speed_multiplier: Multiplier for playback speed. (float/int)
            crop_start_time: Start Time (in seconds)
            end_start_time: End Time (in seconds)

        Returns:
            Path to the scrubbed (redacted) mp4 file.
        """
        raise NotImplementedError

    class ScrubbingProviderFactory:
        """A Factory Class for Scrubbing Providers"""

        @staticmethod
        def get_for_modality(
            modality: Modality,
        ) -> List[ScrubbingProvider]:  # noqa: F821
            """Get Scrubbing Providers for a given Modality

            Args:
                modality (Modality): Modality Type

            Returns:
                List[ScrubbingProvider]: Scrubbing Providers
            """
            scrubbing_providers = ScrubbingProvider.__subclasses__()

            filtered_providers = [
                provider
                for provider in scrubbing_providers
                if modality in provider.capabilities
            ]

            return filtered_providers
