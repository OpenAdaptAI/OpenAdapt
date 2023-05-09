from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

MAX_MASK_LEN = 1024

# Required Packages: 
# pip install presidio_analyzer
# pip install presidio_anonymizer
# pip install presidio_image_redactor
# Note: they have been added in requirement.txt
# python -m spacy download en_core_web_lg (after installing the required packages)

def scrub(text: str) -> str:
    """
        This function takes in a string of text as input and returns a scrubbed version of the text with any potential personally
        identifiable information (PII) or protected health information (PHI) replaced with asterisks.
        The function uses Presidio to identify and replace PII/PHI types such as email addresses, phone numbers, credit card numbers,
        dates of birth, addresses, social security numbers, driver's license numbers, passport numbers, national ID numbers,
        bank routing numbers, and bank account numbers.
        
        Usage example:
        python -m puterbot.scrub
        :param text: a string of text that may contain PII/PHI
        :return: a scrubbed version of the input text with PII/PHI replaced with asterisks.
    """
    
    analyzer = AnalyzerEngine()
    analyzer_results = analyzer.analyze(text=text, entities=analyzer.get_supported_entities(), language="en")
    anonymizer = AnonymizerEngine()

    operators = {}
    for entity in analyzer_results:
        operators[entity.entity_type] = OperatorConfig("mask", {"masking_char": "*", "chars_to_mask": MAX_MASK_LEN, "from_end": True})
        
    anonymized_results = anonymizer.anonymize(
        text=text,
        analyzer_results=analyzer_results,     # type: ignore
        operators=operators
    )

    return anonymized_results.text


if __name__ == '__main__':
    import sys
    inp_text = "My credit card number is 1234-5678-9012-3456 and "
    scrubbed_text = scrub(inp_text)
    print(scrubbed_text)

    # Note: This script uses the sys.stdin.read() method to read input from the console, 
    # which means you'll need to 
    # press Ctrl + D on Linux/Mac or 
    # Enter then Ctrl + Z then again hit Enter, on Windows to signal the end of the input when you're done typing or pasting in your text.