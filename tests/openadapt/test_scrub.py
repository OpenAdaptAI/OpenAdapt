"""Module to test scrub.py"""

from io import BytesIO
import os
import warnings

from PIL import Image

from openadapt import scrub, config


def test_scrub_image() -> None:
    """
    Test that the scrubbed image data is different
    """

    warnings.filterwarnings("ignore", category=DeprecationWarning)

    # Read test image data from file
    test_image_path = "assets/test_scrub_image.png"
    with open(test_image_path, "rb") as file:
        test_image_data = file.read()

    # Convert image data to PIL Image object
    test_image = Image.open(BytesIO(test_image_data))

    # Scrub the image
    scrubbed_image = scrub.scrub_image(test_image)

    # Save the scrubbed image data to a file
    scrubbed_image_path = "scrubbed_image.png"
    scrubbed_image.save(scrubbed_image_path)

    # Load the scrubbed image from file for manual verification
    scrubbed_image = Image.open(scrubbed_image_path)
    scrubbed_image = scrubbed_image.convert("RGB")

    # Count the number of pixels having the color of the mask
    mask_pixels = sum(
        1
        for pixel in scrubbed_image.getdata()
        if pixel == config.DEFAULT_SCRUB_FILL_COLOR
    )
    total_pixels = scrubbed_image.width * scrubbed_image.height

    test_image.close()
    scrubbed_image.close()
    os.remove(scrubbed_image_path)

    # Assert that the number of mask pixels is approximately 1.5% the total number of pixels
    assert (
        round((mask_pixels / total_pixels), 3) == 0.015
    )  # Change this value as necessary


def test_empty_string() -> None:
    """
    Test that an empty string is returned
    if an empty string is passed to the scrub function.
    """

    text = ""
    expected_output = ""
    assert scrub.scrub_text(text) == expected_output


def test_no_scrub_string() -> None:
    """
    Test that the same string is returned
    """

    text = "This string doesn't have anything to scrub."
    expected_output = "This string doesn't have anything to scrub."
    assert scrub.scrub_text(text) == expected_output


def test_scrub_email() -> None:
    """
    Test that the email address is scrubbed
    """

    # Test scrubbing of email address
    assert (
        scrub.scrub_text("My email is john.doe@example.com.")
        == "My email is ********************."
    )


def test_scrub_phone_number() -> None:
    """
    Test that the phone number is scrubbed
    """

    assert (
        scrub.scrub_text("My phone number is 123-456-7890.")
        == "My phone number is ************."
    )


def test_scrub_credit_card() -> None:
    """
    Test that the credit card number is scrubbed
    """

    assert (
        scrub.scrub_text("My credit card number is 4234-5678-9012-3456 and ")
    ) == "My credit card number is ******************* and "


def test_scrub_date_of_birth() -> None:
    """
    Test that the date of birth is scrubbed
    """

    assert (
        scrub.scrub_text("My date of birth is 01/01/2000.")
        == "My date of birth is 01/01/2000."
    )


def test_scrub_address() -> None:
    """
    Test that the address is scrubbed
    """

    assert (
        scrub.scrub_text("My address is 123 Main St, Toronto, On, CAN.")
        == "My address is 123 Main St, Toro***, On, ***."
    )


def test_scrub_ssn() -> None:
    """
    Test that the social security number is scrubbed
    """

    # Test scrubbing of social security number
    assert (
        scrub.scrub_text("My social security number is 923-45-6789")
        == "My social security number is ***********"
    )


def test_scrub_dl() -> None:
    """
    Test that the driver's license number is scrubbed
    """

    assert (
        scrub.scrub_text("My driver's license number is A123-456-789-012")
        == "My driver's license number is ****-456-789-012"
    )


def test_scrub_passport() -> None:
    """
    Test that the passport number is scrubbed
    """

    assert (
        scrub.scrub_text("My passport number is A1234567.")
        == "My passport number is ********."
    )


def test_scrub_national_id() -> None:
    """
    Test that the national ID number is scrubbed
    """

    assert (
        scrub.scrub_text("My national ID number is 1234567890123.")
        == "My national ID number is *************."
    )


def test_scrub_routing_number():
    """
    Test that the bank routing number is scrubbed
    """

    assert (
        scrub.scrub_text("My bank routing number is 123456789.")
        == "My bank routing number is *********."
    )


def test_scrub_bank_account() -> None:
    """
    Test that the bank account number is scrubbed
    """

    assert (
        scrub.scrub_text("My bank account number is 635526789012.")
        == "My bank account number is ************."
    )


def test_scrub_all_together() -> None:
    """
    Test that all PII/PHI types are scrubbed
    """

    text_with_pii_phi = (
        "John Smith's email is johnsmith@example.com and"
        " his phone number is 555-123-4567."
        "His credit card number is 4831-5538-2996-5651 and"
        " his social security number is 923-45-6789. He was born on 01/01/1980."
    )
    assert scrub.scrub_text(text_with_pii_phi) == (
        "************ email is ********************* and"
        " his phone number is ************."
        "His credit card number is ******************* and"
        " his social security number is ***********. He was born on 01/01/1980."
    )
