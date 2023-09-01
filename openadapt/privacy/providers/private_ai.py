"""A Module for Private AI Scrubbing Provider."""

from io import BytesIO
from typing import List
import base64
import os

from loguru import logger
from PIL import Image
import requests

from openadapt import config
from openadapt.privacy.base import Modality, ScrubbingProvider, TextScrubbingMixin
from openadapt.privacy.providers import ScrubProvider

BASE64_URL = "https://api.private-ai.com/deid/v3/process/files/base64"
FILES_DIR = "assets/"
HEADER_CONTENT_TYPE = "application/json"
IMAGE_CONTENT_TYPE = "image/png"
PDF_CONTENT_TYPE = "application/pdf"
TEMP_IMAGEFILE_NAME = "temp_image_to_scrub.png"
TEXT_URL = "https://api.private-ai.com/deid/v3/process/text"


class PrivateAIScrubbingProvider(
    ScrubProvider, ScrubbingProvider, TextScrubbingMixin
):  # pylint: disable=abstract-method
    """A Class for Private AI Scrubbing Provider."""

    name: str = ScrubProvider.PRIVATE_AI
    capabilities: List[Modality] = [Modality.TEXT, Modality.PIL_IMAGE, Modality.PDF]

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        """Scrub the text of all PII/PHI.

        Args:
            text (str): Text to be redacted
            is_separated (bool): Whether the text is separated with special characters

        Returns:
            str: redacted text
        """
        payload = {
            "text": [text],
            "link_batch": False,
            "entity_detection": {
                "accuracy": "high",
                "return_entity": True,
            },
            "processed_text": {
                "type": "MARKER",
                "pattern": "[UNIQUE_NUMBERED_ENTITY_TYPE]",
            },
        }

        headers = {
            "Content-Type": HEADER_CONTENT_TYPE,
            "X-API-KEY": config.PRIVATE_AI_API_KEY,
        }

        response = requests.post(TEXT_URL, json=payload, headers=headers)
        response.raise_for_status()

        data = response.json()
        logger.debug(f"{data=}")

        # According to the API documentation,
        # the response is a list of dicts when there is no error/issue in the request
        # else it is a dict with a key "detail" containing the error message

        if type(data) is dict and "detail" in data:
            raise ValueError(data.get("detail"))

        redacted_text = data[0].get("processed_text")
        logger.debug(f"{redacted_text=}")

        return redacted_text

    def scrub_image(
        self,
        image: Image,
        fill_color: int = config.SCRUB_FILL_COLOR,  # pylint: disable=no-member
    ) -> Image:
        """Scrub the image of all PII/PHI.

        Args:
            image (Image): A PIL.Image object to be redacted
            fill_color (int): The color used to fill the redacted regions(BGR).

        Returns:
            Image: The redacted image with PII and PHI removed.
        """
        # save file as "temp_image_to_scrub.png in assets/
        temp_image_path = os.path.join(FILES_DIR, TEMP_IMAGEFILE_NAME)

        buffer = BytesIO()

        image.save(buffer, format="PNG")
        # Get the image data as bytes
        image_data = buffer.getvalue()

        file_data = base64.b64encode(image_data)
        file_data = file_data.decode("ascii")

        # Clean up by closing the BytesIO buffer
        buffer.close()

        payload = {
            "file": {"data": file_data, "content_type": IMAGE_CONTENT_TYPE},
            "entity_detection": {"accuracy": "high", "return_entity": True},
            "pdf_options": {"density": 150, "max_resolution": 2000},
            "audio_options": {"bleep_start_padding": 0, "bleep_end_padding": 0},
        }

        headers = {
            "Content-Type": HEADER_CONTENT_TYPE,
            "X-API-KEY": config.PRIVATE_AI_API_KEY,
        }

        response = requests.post(BASE64_URL, json=payload, headers=headers)
        response.raise_for_status()
        response = response.json()
        logger.debug(f"{response=}")

        # According to the API documentation,
        # the response is a list of dicts when there is no error/issue in the request
        # else it is a dict with a key "detail" containing the error message

        if type(response) is dict and "detail" in response:
            raise ValueError(response.get("detail"))

        redact_file_path = os.path.join(FILES_DIR, f"redacted-{TEMP_IMAGEFILE_NAME}")

        # Write to file
        with open(redact_file_path, "wb") as redacted_file:
            processed_file = response.get("processed_file").encode("ascii")
            processed_file = base64.b64decode(processed_file, validate=True)
            redacted_file.write(processed_file)

        # return PIL.IMAGE type of redacted image
        with open(redact_file_path, "rb") as file:
            redacted_file_data = file.read()
        os.remove(redact_file_path)  # remove redacted image after reading data

        redact_pil_image_data = Image.open(BytesIO(redacted_file_data))

        return redact_pil_image_data

    def scrub_pdf(self, path_to_pdf: str) -> str:
        """Scrub the PDF of all PII/PHI.

        Args:
            path_to_pdf (str): Path to the PDF to be redacted

        Returns:
            str: Path to the redacted PDF
        """
        # Read from file
        with open(path_to_pdf, "rb") as b64_file:
            file_data = base64.b64encode(b64_file.read())
            file_data = file_data.decode("ascii")

        payload = {
            "file": {"data": file_data, "content_type": PDF_CONTENT_TYPE},
            "entity_detection": {"accuracy": "high", "return_entity": True},
            "pdf_options": {"density": 150, "max_resolution": 2000},
            "audio_options": {"bleep_start_padding": 0, "bleep_end_padding": 0},
        }

        headers = {
            "Content-Type": HEADER_CONTENT_TYPE,
            "X-API-KEY": config.PRIVATE_AI_API_KEY,
        }

        response = requests.post(BASE64_URL, json=payload, headers=headers)
        response.raise_for_status()
        # This will raise an HTTPError
        # if the response status code indicates an error (4xx or 5xx)
        response_data = response.json()

        # According to the API documentation,
        # the response is a list of dicts when there is no error/issue in the request
        # else it is a dict with a key "detail" containing the error message

        if isinstance(response_data, dict) and "details" in response_data:
            raise response_data.get("detail")

        logger.debug(f"{response_data.get('entities')=}")
        logger.debug(f"{len(response_data.get('entities'))=}")

        redacted_file_path = path_to_pdf.split(".")[0] + "_redacted.pdf"

        # Write to file
        with open(redacted_file_path, "wb") as redacted_file:
            processed_file = response.get("processed_file").encode("ascii")
            processed_file = base64.b64decode(processed_file, validate=True)
            redacted_file.write(processed_file)

        return redacted_file_path
