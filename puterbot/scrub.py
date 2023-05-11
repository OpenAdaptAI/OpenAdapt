"""Module to scrub text of all PII/PHI"""
from io import BytesIO
from PIL import Image
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig
from presidio_image_redactor import ImageRedactorEngine


# PREREQUISITES:
# Download the TesseractOCR: https://github.com/tesseract-ocr/tesseract#installing-tesseract
# python -m spacy download en_core_web_lg (before running the scrub module)

MAX_MASK_LEN = 1024


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
    analyzer = AnalyzerEngine()
    analyzer_results = analyzer.analyze(
        text=text, entities=analyzer.get_supported_entities(), language="en"
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
        analyzer_results=analyzer_results,  # type: ignore
        operators=operators,
    )

    return anonymized_results.text


def scrub_image(png_data: bytes) -> bytes:
    """Scrub the png_data of all PII/PHI

    Scrub the png_data of all PII/PHI using Presidio Image Redactor

    Args:
        png_data (bytes): PNG data to be scrubbed

    Returns:
        bytes: Scrubbed PNG data

    Raises:
        None
    """
    # Load image from the input png_data
    image = Image.open(BytesIO(png_data))

    # Initialize the engine
    engine = ImageRedactorEngine()

    # Redact the image with red color
    redacted_image = engine.redact(image, (255, 0, 0))  # type: ignore

    # Save the redacted image to an in-memory buffer
    output_buffer = BytesIO()
    redacted_image.save(output_buffer, format="PNG")  # type: ignore

    # Get the redacted image data from the buffer
    redacted_png_data = output_buffer.getvalue()

    # Return the redacted image data
    return redacted_png_data
