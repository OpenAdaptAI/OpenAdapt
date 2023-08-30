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

PRIVATE_AI_ENTITY_LIST = [
    "ACCOUNT_NUMBER",
    "AGE",
    "DATE",
    "DATE_INTERVAL",
    "DOB",
    "DRIVER_LICENSE",
    "DURATION",
    "EMAIL_ADDRESS",
    "EVENT",
    "FILENAME",
    "GENDER_SEXUALITY",
    "HEALTHCARE_NUMBER",
    "IP_ADDRESS",
    "LANGUAGE",
    "LOCATION",
    "LOCATION_ADDRESS",
    "LOCATION_CITY",
    "LOCATION_COORDINATE",
    "LOCATION_COUNTRY",
    "LOCATION_STATE",
    "LOCATION_ZIP",
    "MARITAL_STATUS",
    "MONEY",
    "NAME",
    "NAME_FAMILY",
    "NAME_GIVEN",
    "NAME_MEDICAL_PROFESSIONAL",
    "NUMERICAL_PII",
    "ORGANIZATION",
    "ORGANIZATION_MEDICAL_FACILITY",
    "OCCUPATION",
    "ORIGIN",
    "PASSPORT_NUMBER",
    "PASSWORD",
    "PHONE_NUMBER",
    "PHYSICAL_ATTRIBUTE",
    "POLITICAL_AFFILIATION",
    "RELIGION",
    "SSN",
    "TIME",
    "URL",
    "USERNAME",
    "VEHICLE_ID",
    "ZODIAC_SIGN",
    "BLOOD_TYPE",
    "CONDITION",
    "DOSE",
    "DRUG",
    "INJURY",
    "MEDICAL_PROCESS",
    "STATISTICS",
    "BANK_ACCOUNT",
    "CREDIT_CARD",
    "CREDIT_CARD_EXPIRATION",
    "CVV",
    "ROUTING_NUMBER",
]


class PrivateAIScrubbingProvider(
    ScrubProvider, ScrubbingProvider, TextScrubbingMixin
):  # pylint: disable=abstract-method
    """A Class for Private AI Scrubbing Provider."""

    name: str = ScrubProvider.PRIVATE_AI
    capabilities: List[Modality] = [Modality.TEXT, Modality.PIL_IMAGE, Modality.PDF]

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        """Scrub the text of all PII/PHI.

        Args:
            text (str): Text to be scrubbed
            is_separated (bool): Whether the text is separated with special characters

        Returns:
            str: Scrubbed text
        """
        url = "https://api.private-ai.com/deid/v3/process/text"

        payload = {
            "text": text,
            "link_batch": False,
            "entity_detection": {
                "accuracy": "high",
                "entity_types": [
                    {
                        "type": "ENABLE",
                        "value": [
                            "ACCOUNT_NUMBER",
                            "AGE",
                            "DATE",
                            "DATE_INTERVAL",
                            "DOB",
                            "DRIVER_LICENSE",
                            "DURATION",
                            "EMAIL_ADDRESS",
                            "EVENT",
                            "FILENAME",
                            "GENDER_SEXUALITY",
                            "HEALTHCARE_NUMBER",
                            "IP_ADDRESS",
                            "LANGUAGE",
                            "LOCATION",
                            "LOCATION_ADDRESS",
                            "LOCATION_CITY",
                            "LOCATION_COORDINATE",
                            "LOCATION_COUNTRY",
                            "LOCATION_STATE",
                            "LOCATION_ZIP",
                            "MARITAL_STATUS",
                            "MONEY",
                            "NAME",
                            "NAME_FAMILY",
                            "NAME_GIVEN",
                            "NAME_MEDICAL_PROFESSIONAL",
                            "NUMERICAL_PII",
                            "ORGANIZATION",
                            "ORGANIZATION_MEDICAL_FACILITY",
                            "OCCUPATION",
                            "ORIGIN",
                            "PASSPORT_NUMBER",
                            "PASSWORD",
                            "PHONE_NUMBER",
                            "PHYSICAL_ATTRIBUTE",
                            "POLITICAL_AFFILIATION",
                            "RELIGION",
                            "SSN",
                            "TIME",
                            "URL",
                            "USERNAME",
                            "VEHICLE_ID",
                            "ZODIAC_SIGN",
                            "BLOOD_TYPE",
                            "CONDITION",
                            "DOSE",
                            "DRUG",
                            "INJURY",
                            "MEDICAL_PROCESS",
                            "STATISTICS",
                            "BANK_ACCOUNT",
                            "CREDIT_CARD",
                            "CREDIT_CARD_EXPIRATION",
                            "CVV",
                            "ROUTING_NUMBER",
                        ],
                    }
                ],
                "return_entity": True,
            },
            "processed_text": {
                "type": "MARKER",
                "pattern": "[UNIQUE_NUMBERED_ENTITY_TYPE]",
            },
        }

        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": config.PRIVATE_AI_API_KEY,
        }

        response = requests.post(url, json=payload, headers=headers)
        if response is None:
            raise ValueError("Private AI request returned None")

        data = response.json()
        logger.debug(data)
        if "detail" in data.keys():
            raise ValueError(data["detail"])

        redacted_text = data[0].get("processed_text")
        logger.debug(redacted_text)

        return redacted_text

    def scrub_image(
        self,
        image: Image,
        fill_color: int = config.SCRUB_FILL_COLOR,  # pylint: disable=no-member
    ) -> Image:
        """Scrub the image of all PII/PHI.

        Args:
            image (Image): A PIL.Image object to be scrubbed
            fill_color (int): The color used to fill the redacted regions(BGR).

        Returns:
            Image: The scrubbed image with PII and PHI removed.
        """
        url = "https://api.private-ai.com/deid/v3/process/files/base64"
        file_dir = "assets/"
        file_name = "temp_image_to_scrub.png"

        # save file as "temp_image_to_scrub.png in assets/
        temp_image_path = os.path.join(file_dir, file_name)
        image.save(temp_image_path)

        file_type = "image/png"

        # Read from file
        with open(temp_image_path, "rb") as b64_file:
            file_data = base64.b64encode(b64_file.read())
            file_data = file_data.decode("ascii")

        payload = {
            "file": {"data": file_data, "content_type": file_type},
            "entity_detection": {"accuracy": "high", "return_entity": True},
            "pdf_options": {"density": 150, "max_resolution": 2000},
            "audio_options": {"bleep_start_padding": 0, "bleep_end_padding": 0},
        }

        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": config.PRIVATE_AI_API_KEY,
        }

        response = requests.post(url, json=payload, headers=headers)
        if response is None:
            raise ValueError("Private AI request returned None")
        response = response.json()
        logger.debug(response)
        if "detail" in response.keys():
            raise ValueError(response["detail"])

        redact_file_path = os.path.join(file_dir, f"redacted-{file_name}")

        # Write to file
        with open(redact_file_path, "wb") as redacted_file:
            processed_file = response.get("processed_file").encode("ascii")
            processed_file = base64.b64decode(processed_file, validate=True)
            redacted_file.write(processed_file)

        # return PIL_IMAGE type of redacted image
        with open(redact_file_path, "rb") as file:
            redacted_file_data = file.read()

        redact_pil_image_data = Image.open(BytesIO(redacted_file_data))
        return redact_pil_image_data

    def scrub_pdf(self, path_to_pdf: str) -> str:
        """Scrub the PDF of all PII/PHI.

        Args:
            path_to_pdf (str): Path to the PDF to be scrubbed

        Returns:
            str: Path to the scrubbed PDF
        """
        url = "https://api.private-ai.com/deid/v3/process/files/base64"

        file_type = "application/pdf"

        # Read from file
        with open(path_to_pdf, "rb") as b64_file:
            file_data = base64.b64encode(b64_file.read())
            file_data = file_data.decode("ascii")

        payload = {
            "file": {"data": file_data, "content_type": file_type},
            "entity_detection": {"accuracy": "high", "return_entity": True},
            "pdf_options": {"density": 150, "max_resolution": 2000},
            "audio_options": {"bleep_start_padding": 0, "bleep_end_padding": 0},
        }

        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": config.PRIVATE_AI_API_KEY,
        }

        response = requests.post(url, json=payload, headers=headers)
        if response is None:
            raise ValueError("Private AI request returned None")
        response = response.json()
        logger.debug(response)
        if "detail" in response.keys():
            raise ValueError(response["detail"])

        redacted_file_path = path_to_pdf.split(".")[0], "_redacted.pdf"

        # Write to file
        with open(redacted_file_path, "wb") as redacted_file:
            processed_file = response.get("processed_file").encode("ascii")
            processed_file = base64.b64decode(processed_file, validate=True)
            redacted_file.write(processed_file)

        return redacted_file_path