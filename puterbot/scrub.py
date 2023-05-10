"""Module to scrub text of all PII/PHI"""
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

# python -m spacy download en_core_web_lg (before running the scrub module)

MAX_MASK_LEN = 1024


def scrub(
    text: str
) -> str:
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
        text=text, entities=analyzer.get_supported_entities(), language="en")
    anonymizer = AnonymizerEngine()

    operators = {}
    for entity in analyzer_results:
        operators[entity.entity_type] = OperatorConfig(
            "mask", {"masking_char": "*", "chars_to_mask": MAX_MASK_LEN, "from_end": True})

    anonymized_results = anonymizer.anonymize(
        text=text,
        analyzer_results=analyzer_results,     # type: ignore
        operators=operators
    )

    return anonymized_results.text


if __name__ == "__main__":
    # MasterCard
    print(scrub("John Smith's email is johnsmith@example.com and"
     "his phone number is 555-123-4567."
     "His credit card number is 4534-5678-9012-3456 and"
     " his social security number is 923-45-6789. He was born on 01/01/1980."))
