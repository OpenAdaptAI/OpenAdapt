import re

def scrub(text: str) -> str:
    """
    This function takes in a string of text as input and returns a scrubbed version of the text with any potential personally
    identifiable information (PII) or protected health information (PHI) replaced with asterisks.
    The function replaces email addresses, phone numbers, credit card numbers, and dates of birth (or any dates) 
    with asterisks. It uses regular expressions to identify the patterns of these types of information in the text and 
    replaces them with the corresponding number of asterisks.
    
    Usage example:
    python -m puterbot.scrub
    :param text: a string of text that may contain PII/PHI
    :return: a scrubbed version of the input text with PII/PHI replaced with asterisks.
    """
    # "\b = a word boundary/complete word"
    scrubbed_text = text
    # Replace email addresses: XYZ@XYZ.XYZ [Format] [XYZ stands for Alphanumeric characters]
    scrubbed_text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '***@***.***', scrubbed_text)

    # Replace phone numbers XXX-XXX-XXXX [Format] [X stands for A digit from 0-9]
    scrubbed_text = re.sub(r'\b\d{3}[-\s]?\d{3}[-\s]?\d{4}\b', '***-***-****', scrubbed_text)

    # Replace credit card numbers XXXX-XXXX-XXXX-XXXX [Format] [X stands for A digit from 0-9]
    scrubbed_text = re.sub(r'\b\d{4}-\d{4}-\d{4}-\d{4}\b', '****-****-****-****', scrubbed_text)

    # Replace dates of birth (or any Dates) mm/dd/yyyy or dd/mm/yyyy  [Format]
    scrubbed_text = re.sub(r'\b\d{2}/\d{2}/\d{4}\b', '**/**/****', scrubbed_text)

    # TODO: Add any other PII or PHI field for scrubbing.


    return scrubbed_text


if __name__ == '__main__':
    import sys
    inp_text = sys.stdin.read()
    scrubbed_text = scrub(inp_text)
    sys.stdout.write(scrubbed_text)

    # Note: This script uses the sys.stdin.read() method to read input from the console, 
    # which means you'll need to 
    # press Ctrl + D on Linux/Mac or 
    # Enter then Ctrl + Z then again hit Enter, on Windows to signal the end of the input when you're done typing or pasting in your text.