"""Module to test scrub.py"""
from puterbot.scrub import scrub


def test_empty_string() -> None:
    """ This test is to ensure that an empty string is returned 
    if an empty string is passed to the scrub function.
    
    Args:
        None
    Returns:
        None
    Raises: 
        None
    """
    text = ""
    expected_output = ""
    assert scrub(text) == expected_output


def test_no_scrub_string() -> None:
    """
    This test is to ensure that the same string is returned
    
    Args:
        None
    Returns:
        None
    Raises: 
        None
    """
    text = "This string doesn't have anything to scrub."
    expected_output = "This string doesn't have anything to scrub."
    assert scrub(text) == expected_output


def test_scrub_email() -> None:
    """
    This test is to ensure that the email address is scrubbed
    
    Args:
        None
    Returns:
        None
    Raises: 
        None
    """
    # Test scrubbing of email address
    assert scrub(
        "My email is john.doe@example.com.") == "My email is ********************."


def test_scrub_phone_number() -> None:
    """
    This test is to ensure that the phone number is scrubbed
    
    Args:
        None
    Returns:
        None
    Raises: 
        None
    """
    assert scrub(
        "My phone number is 123-456-7890.") == "My phone number is ************."


def test_scrub_credit_card() -> None:
    """
    This test is to ensure that the credit card number is scrubbed
    
    Args:
        None
    Returns:
        None
    Raises: 
        None
    """
    assert ((scrub(
        "My credit card number is 4234-5678-9012-3456 and "))
    == "My credit card number is ******************* and ")


def test_scrub_date_of_birth() -> None:
    """
    This test is to ensure that the date of birth is scrubbed
    
    Args:
        None
    Returns:
        None
    Raises: 
        None
    """
    assert scrub(
        "My date of birth is 01/01/2000.") == "My date of birth is **********."


def test_scrub_address() -> None:
    """
    This test is to ensure that the address is scrubbed
    
    Args:
        None
    Returns:
        None
    Raises: 
        None
    """
    assert (scrub(
        "My address is 123 Main St, Toronto, On, CAN.")
            == "My address is 123 Main St, *******, On, CAN.")


def test_scrub_ssn() -> None:
    """
    This test is to ensure that the social security number is scrubbed
    
    Args:
        None
    Returns:
        None
    Raises: 
        None
    """
    # Test scrubbing of social security number
    assert scrub(
        "My social security number is 923-45-6789") == "My social security number is ***********"


def test_scrub_dl() -> None:
    """
    This test is to ensure that the driver's license number is scrubbed
    
    Args:
        None
    Returns:
        None
    Raises: 
        None
    """
    assert (scrub(
        "My driver's license number is A123-456-789-012")
            == "My driver's license number is ****-456-789-012")


def test_scrub_passport() -> None:
    """
    This test is to ensure that the passport number is scrubbed
    
    Args:
        None
    Returns:
        None
    Raises: 
        None
    """
    assert scrub(
        "My passport number is A1234567.") == "My passport number is ********."


def test_scrub_national_id() -> None:
    """
    This test is to ensure that the national ID number is scrubbed
    
    Args:
        None
    Returns:
        None
    Raises: 
        None
    """
    assert scrub(
        "My national ID number is 1234567890123.") == "My national ID number is *************."


def test_scrub_routing_number():
    """
    This test is to ensure that the bank routing number is scrubbed
    
    Args:
        None
    Returns:
        None
    Raises: 
        None
    """
    assert scrub(
        "My bank routing number is 123456789.") == "My bank routing number is *********."


def test_scrub_bank_account() -> None:
    """
    This test is to ensure that the bank account number is scrubbed
    
    Args:
        None
    Returns:
        None
    Raises: 
        None
    """
    assert scrub(
        "My bank account number is 635526789012.") == "My bank account number is ************."


def test_scrub_all_together() -> None:
    """
    This test is to ensure that all PII/PHI types are scrubbed
    
    Args:
        None
    Returns:
        None
    Raises: 
        None
    """
    text_with_pii_phi = ("John Smith's email is johnsmith@example.com and"
     " his phone number is 555-123-4567."
     "His credit card number is 4534-5678-9012-3456 and"
     " his social security number is 923-45-6789. He was born on 01/01/1980.")
    assert (scrub(text_with_pii_phi) == "************ email is ********************* and"
            " his phone number is ************."
            "His credit card number is 4534-5678-9012-3456 and"
            " his social security number is ***********. He was born on **********.")
