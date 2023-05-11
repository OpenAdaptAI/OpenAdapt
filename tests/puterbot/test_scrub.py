"""Module to test scrub.py"""
import os
import pytesseract
from PIL import Image
from io import BytesIO
from puterbot.scrub import scrub, scrub_image


def test_scrub_image_data() -> None:
    """
    Test is to be sure that the scrubbed image data is different
    """
    test_image_path = "test_image.png" # An image with john@deo@gmail.com
    with open(test_image_path, "rb") as f:
        test_image_data = f.read()

    # Convert image data to PIL Image object
    test_image = Image.open(BytesIO(test_image_data))

    # Scrub the image
    scrubbed_image_data = scrub_image(test_image_data)

    # Save the scrubbed image data to a file
    scrubbed_image_path = "scrubbed_image.png"
    with open(scrubbed_image_path, "wb") as f:
        f.write(scrubbed_image_data)
        
    # Load the scrubbed image from file for manual verification
    scrubbed_image = Image.open(scrubbed_image_path)

    # Verify that the scrubbed image data does not contain the email
    # assert b"abc" not in scrubbed_image_data
    
    # Perform OCR on the scrubbed image
    ocr_text = pytesseract.image_to_string(scrubbed_image)
    print(ocr_text)

    scrubbed_image.close()
    test_image.close()


def test_empty_string() -> None:
    """
    Test is to ensure that an empty string is returned
    if an empty string is passed to the scrub function.
    """
    text = ""
    expected_output = ""
    assert scrub(text) == expected_output


def test_no_scrub_string() -> None:
    """
    Test is to ensure that the same string is returned
    """
    text = "This string doesn't have anything to scrub."
    expected_output = "This string doesn't have anything to scrub."
    assert scrub(text) == expected_output


def test_scrub_email() -> None:
    """
    Test is to ensure that the email address is scrubbed
    """
    # Test scrubbing of email address
    assert (
        scrub("My email is john.doe@example.com.")
        == "My email is ********************."
    )


def test_scrub_phone_number() -> None:
    """
    Test is to ensure that the phone number is scrubbed
    """
    assert (
        scrub("My phone number is 123-456-7890.") == "My phone number is ************."
    )


def test_scrub_credit_card() -> None:
    """
    Test is to ensure that the credit card number is scrubbed
    """
    assert (
        scrub("My credit card number is 4234-5678-9012-3456 and ")
    ) == "My credit card number is ******************* and "


def test_scrub_date_of_birth() -> None:
    """
    Test is to ensure that the date of birth is scrubbed
    """
    assert scrub("My date of birth is 01/01/2000.") == "My date of birth is **********."


def test_scrub_address() -> None:
    """
    Test is to ensure that the address is scrubbed
    """
    assert (
        scrub("My address is 123 Main St, Toronto, On, CAN.")
        == "My address is 123 Main St, *******, On, CAN."
    )


def test_scrub_ssn() -> None:
    """
    Test is to ensure that the social security number is scrubbed
    """
    # Test scrubbing of social security number
    assert (
        scrub("My social security number is 923-45-6789")
        == "My social security number is ***********"
    )


def test_scrub_dl() -> None:
    """
    Test is to ensure that the driver's license number is scrubbed
    """
    assert (
        scrub("My driver's license number is A123-456-789-012")
        == "My driver's license number is ****-456-789-012"
    )


def test_scrub_passport() -> None:
    """
    Test is to ensure that the passport number is scrubbed
    """
    assert scrub("My passport number is A1234567.") == "My passport number is ********."


def test_scrub_national_id() -> None:
    """
    Test is to ensure that the national ID number is scrubbed
    """
    assert (
        scrub("My national ID number is 1234567890123.")
        == "My national ID number is *************."
    )


def test_scrub_routing_number():
    """
    Test is to ensure that the bank routing number is scrubbed
    """
    assert (
        scrub("My bank routing number is 123456789.")
        == "My bank routing number is *********."
    )


def test_scrub_bank_account() -> None:
    """
    Test is to ensure that the bank account number is scrubbed
    """
    assert (
        scrub("My bank account number is 635526789012.")
        == "My bank account number is ************."
    )


def test_scrub_all_together() -> None:
    """
    Test is to ensure that all PII/PHI types are scrubbed
    """
    text_with_pii_phi = (
        "John Smith's email is johnsmith@example.com and"
        " his phone number is 555-123-4567."
        "His credit card number is 4831-5538-2996-5651 and"
        " his social security number is 923-45-6789. He was born on 01/01/1980."
    )
    assert scrub(text_with_pii_phi) == (
        "************ email is ********************* and"
        " his phone number is ************."
        "His credit card number is ******************* and"
        " his social security number is ***********. He was born on **********."
    )
