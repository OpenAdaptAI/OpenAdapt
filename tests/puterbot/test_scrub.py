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
    text = "test email is test@utoronto.ca "
    expected_output = "test email is ***@***.*** "
    assert scrub(text) == expected_output


def test_scrub_phone_number():
    text = "test phone number is 123-456-7890 and my"
    expected_output = "test phone number is ***-***-**** and my"
    assert scrub(text) == expected_output


def test_scrub_credit_card_number():
    text = "test credit card number is 1234-1234-1234-1234 sf"
    expected_output = "test credit card number is ****-****-****-**** sf"
    assert scrub(text) == expected_output


def test_scrub_date_of_birth():
    text = "test date of birth is 01/01/1990"
    expected_output = "test date of birth is **/**/****"
    assert scrub(text) == expected_output
    

# Add tests for other fields for newly added PII/PHI in scrub.py [if any]


def test_all_together():
    text = "My email is john@example.com, my phone number is 123-456-7890, " \
           "my credit card number is 1234-5678-9012-3456, and my date of birth is 01/01/1990."
    expected_output = "My email is ***@***.***, my phone number is ***-***-****, " \
                      "my credit card number is ****-****-****-****, and my date of birth is **/**/****."
    assert scrub(text) == expected_output