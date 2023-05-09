import pytest
from puterbot.scrub import scrub

def test_empty_string():
    text = ""
    expected_output = ""
    assert scrub(text) == expected_output

def test_no_scrub_string():
    text = "This string doesn't have anything to scrub."
    expected_output = "This string doesn't have anything to scrub."
    assert scrub(text) == expected_output

def test_scrub_email():
    # Test scrubbing of email address
    assert scrub("My email is john.doe@example.com.") == "My email is ********************."

def test_scrub_phone_number():
    # Test scrubbing of phone number
    assert scrub("My phone number is 123-456-7890.") == "My phone number is ************."

def test_scrub_credit_card():
    # Test scrubbing of credit card number
    assert scrub("My credit card number is 1234-5678-9012-3456 and ") == "My credit card number is ****-****-****-**** and "

def test_scrub_date_of_birth():
    # Test scrubbing of date of birth
    assert scrub("My date of birth is 01/01/2000.") == "My date of birth is **********."

def test_scrub_address():
    # Test scrubbing of address
    assert scrub("My address is 123 Main St, Toronto, On, CAN.") == "My address is 123 Main St, *******, On, CAN."

def test_scrub_ssn():
    # Test scrubbing of social security number
    assert scrub("My social security number is 123-45-6789") == "My social security number is ***********"

def test_scrub_dl():
    # Test scrubbing of driver's license number
    assert scrub("My driver's license number is A123-456-789-012") == "My driver's license number is ****-456-789-012"

def test_scrub_passport():
    # Test scrubbing of passport number
    assert scrub("My passport number is A1234567.") == "My passport number is ********."

def test_scrub_national_id():
    # Test scrubbing of national ID number
    assert scrub("My national ID number is 1234567890123.") == "My national ID number is *************."

def test_scrub_routing_number():
    # Test scrubbing of bank routing number
    assert scrub("My bank routing number is 123456789.") == "My bank routing number is *********."

def test_scrub_bank_account():
    # Test scrubbing of bank account number
    assert scrub("My bank account number is 123456789012.") == "My bank account number is ************."

def test_scrub_all_together():
    # Text with all PII/PHI types
    text_with_pii_phi = "John Smith's email is johnsmith@example.com and his phone number is 555-123-4567. His credit card number is 1234-5678-9012-3456 and his social security number is 123-45-6789. He was born on 01/01/1980."
    assert scrub(text_with_pii_phi) == "****'s email is ***@***.*** and his phone number is ***-***-****. His credit card number is ****-****-****-**** and his social security number is ***-**-****. He was born on **/**/****."
