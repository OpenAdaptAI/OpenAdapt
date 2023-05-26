"""Module to scrub text of all PII/PHI.

Usage:

    $ python openadapt/scrub.py scrub_text str_arg
    $ python openadapt/scrub.py scrub_image Image_arg
    
"""

from PIL import Image
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig
from presidio_image_redactor import ImageRedactorEngine
import fire

from openadapt import config, utils

analyzer = AnalyzerEngine()

MAX_MASK_LEN = 1024
SCRUBBING_ENTITIES = [
    entity
    for entity in analyzer.get_supported_entities()
    if entity not in config.SCRUB_IGNORE_ENTITIES
]


def scrub_text(text: str) -> str:
    """Scrubs the text of all PII/PHI.

    Scrub the text of all PII/PHI using Presidio Analyzer and Anonymizer

    Args:
        text (str): Text to be scrubbed

    Returns:
        str: Scrubbed text

    Raises:
        None
    """

    analyzer_results = analyzer.analyze(
        text=text, entities=SCRUBBING_ENTITIES, language="en"
    )
    anonymizer = AnonymizerEngine()

    operators = {}
    for entity in analyzer_results:
        operators[entity.entity_type] = OperatorConfig(
            "mask",
            {
                "masking_char": "*",
                "chars_to_mask": MAX_MASK_LEN,
                "from_end": True,
            },
        )

    anonymized_results = anonymizer.anonymize(
        text=text,
        analyzer_results=analyzer_results,
        operators=operators,
    )

    return anonymized_results.text


def scrub_image(
    image: Image, fill_color=config.DEFAULT_SCRUB_FILL_COLOR
) -> Image:
    """Scrub the image of all PII/PHI.

    Scrub the image of all PII/PHI using Presidio Image Redactor

    Args:
        image (PIL.Image): A PIL.Image object to be scrubbed

    Returns:
        PIL.Image: The scrubbed image with PII and PHI removed.

    Raises:
        None
    """

    # Initialize the engine
    engine = ImageRedactorEngine()

    # Redact the image
    redacted_image = engine.redact(
        image, fill=fill_color, entities=SCRUBBING_ENTITIES
    )

    # Return the redacted image data
    return redacted_image


if __name__ == "__main__":
    fire.Fire(utils.get_functions(__name__))
