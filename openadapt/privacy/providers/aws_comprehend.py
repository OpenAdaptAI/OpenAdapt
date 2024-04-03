"""A Module for AWS Comprehend Scrubbing Provider Class."""

from typing import List

from botocore import client
from botocore.exceptions import ClientError
from loguru import logger
import boto3

from openadapt.config import config
from openadapt.privacy.base import Modality, ScrubbingProvider, TextScrubbingMixin
from openadapt.privacy.providers import ScrubProvider


# snippet-start:[python.example_code.comprehend.ComprehendDetect
class ComprehendDetect:
    """Encapsulates Comprehend detection functions."""

    def __init__(self, comprehend_client: client) -> None:
        """:param comprehend_client: A Boto3 Comprehend client."""
        self.comprehend_client = comprehend_client

    # snippet-end:[python.example_code.comprehend.ComprehendDetect]

    # snippet-start:[python.example_code.comprehend.DetectDominantLanguage]
    def detect_languages(self, text: str) -> List[dict]:
        """Detects languages used in a document.

        :param text: The document to inspect.
        :return: The list of languages along with their confidence scores.
        """
        try:  # pylint: disable=no-else-raise
            response = self.comprehend_client.detect_dominant_language(Text=text)
            languages = response["Languages"]
            logger.info("Detected %s languages.", len(languages))
        except ClientError:
            logger.exception("Couldn't detect languages.")
            raise
        else:
            return languages

    # snippet-end:[python.example_code.comprehend.DetectDominantLanguage]

    # snippet-start:[python.example_code.comprehend.DetectPiiEntities]
    def detect_pii(self, text: str, language_code: str) -> List[dict]:
        """Detects personally identifiable information (PII) in a document.

        PII can be things like names, account numbers, or addresses.

        :param text: The document to inspect.
        :param language_code: The language of the document.
        :return: The list of PII entities along with their confidence scores.
        """
        try:  # pylint: disable=no-else-raise
            response = self.comprehend_client.detect_pii_entities(
                Text=text, LanguageCode=language_code
            )
            entities = response["Entities"]
            logger.info("Detected %s PII entities.", len(entities))
        except ClientError:
            logger.exception("Couldn't detect PII entities.")
            raise
        else:
            return entities


# snippet-end:[python.example_code.comprehend.DetectPiiEntities]


class ComprehendScrubbingProvider(
    ScrubProvider, ScrubbingProvider, TextScrubbingMixin
):  # pylint: disable=abstract-method
    """A Class for AWS Comprehend Scrubbing Provider."""

    name: str = ScrubProvider.COMPREHEND
    capabilities: List[Modality] = [Modality.TEXT]

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        """Scrub the text of all PII/PHI using AWS Comprehend.

        Args:
            text (str): Text to be scrubbed
            is_separated (bool): Whether the text is separated with special characters

        Returns:
            str: Scrubbed text
        """
        if text == "":  # empty text
            return text

        comp_detect = ComprehendDetect(
            boto3.client(self.name.lower())
        )  # pylint: disable=E1101

        languages = comp_detect.detect_languages(text)
        lang_code = languages[0]["LanguageCode"]

        pii_entities = comp_detect.detect_pii(text, lang_code)
        if not pii_entities:  # no pii/phi detected
            return text

        scrubbed_text = text

        # ner = Named Entity Recognition
        for ner in reversed(pii_entities):
            scrubbed_text = (
                scrubbed_text[: ner["BeginOffset"]]
                + config.ACTION_TEXT_NAME_PREFIX  # pylint: disable=E1101
                + ner["Type"]
                + config.ACTION_TEXT_NAME_SUFFIX  # pylint: disable=E1101
                + scrubbed_text[ner["EndOffset"] :]  # noqa: E203
            )

        return scrubbed_text
