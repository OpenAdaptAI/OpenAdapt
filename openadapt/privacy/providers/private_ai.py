"""A Module for Private AI Scrubbing Provider Class."""

from typing import List

from loguru import logger
import requests

from openadapt import config
from openadapt.privacy.base import Modality, ScrubbingProvider, TextScrubbingMixin
from openadapt.privacy.providers import ScrubProvider

PRIVATE_AI_SCRUB_ENTITIES = [
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

PAI_OUTPUT_FILE_DIR = "assets\\"


class PrivateAIScrubbingProvider(
    ScrubProvider, ScrubbingProvider, TextScrubbingMixin
):  # pylint: disable=W0223
    """A Class for Private AI Scrubbing Provider."""

    name: str = ScrubProvider.PRIVATE_AI  # pylint: disable=E1101
    capabilities: List[Modality] = [Modality.TEXT]

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        """Scrub the text of all PII/PHI using Private AI.

        Args:
            text (str): Text to be scrubbed
            is_separated (bool): Whether the text is separated with special characters

        Returns:
            str: Scrubbed text
        """
        if text == "":
            return text

        url = "https://api.private-ai.com/deid/v3/process/text"  # PrivateAI demo server

        payload = {
            "text": [text],
            "link_batch": False,
            "entity_detection": {
                "accuracy": "high",
                "entity_types": [
                    {
                        "type": "ENABLE",
                        "value": PRIVATE_AI_SCRUB_ENTITIES,
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

        data = response.json()

        logger.debug(data)
        logger.debug(data[0].get("processed_text"))

        return data[0].get("processed_text")  # redacted text

    def scrub_pdf(self, pdf_path: str) -> str:
        """Scrub the text of all PII/PHI using Private AI.

        Args:
            pdf_path (str): Path to the PDF to be scrubbed

        Returns:
            str: Scrubbed text
        """
        import requests

        url = "https://api.private-ai.com/deid/v3/process/files/uri"

        payload = {
            "uri": "assets\sample_llc_1.pdf",
            "entity_detection": {"accuracy": "high", "return_entity": True},
        }

        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": config.PRIVATE_AI_API_KEY,
        }

        response = requests.post(url, json=payload, headers=headers)

        data = response.json()
        print(data)
