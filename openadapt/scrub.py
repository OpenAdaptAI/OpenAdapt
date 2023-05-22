"""Module to scrub text of all PII/PHI"""
from PIL import Image
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig
from presidio_image_redactor import ImageRedactorEngine
from openadapt.config import SCRUB_IGNORE_ENTITIES

MAX_MASK_LEN = 1024
analyzer = AnalyzerEngine()
SCRUBBING_ENTITIES = [
    entity
    for entity in analyzer.get_supported_entities()
    if entity not in SCRUB_IGNORE_ENTITIES
]


# PREREQUISITES:
# Download the TesseractOCR: https://github.com/tesseract-ocr/tesseract#installing-tesseract
# python -m spacy download en_core_web_lg


def scrub(text: str) -> str:
    """Scrubs the text of all PII/PHI

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
            {"masking_char": "*", "chars_to_mask": MAX_MASK_LEN, "from_end": True},
        )

    anonymized_results = anonymizer.anonymize(
        text=text,
        analyzer_results=analyzer_results,
        operators=operators,
    )

    return anonymized_results.text


def scrub_image(image: Image) -> Image:
    """Scrub the png_data of all PII/PHI

    Scrub the png_data of all PII/PHI using Presidio Image Redactor

    Args:
        png_data (bytes): PNG data to be scrubbed

    Returns:
        bytes: Scrubbed PNG data

    Raises:
        None
    """
    # Initialize the engine
    engine = ImageRedactorEngine()

    # Redact the image with red color
    redacted_image = engine.redact(image, fill=(255,), entities=SCRUBBING_ENTITIES)

    # Return the redacted image data
    return redacted_image
