"""Module to test PrivateAIScrubbingProvider."""

from io import BytesIO
import base64
import os

from loguru import logger
from PIL import Image
import easyocr
import requests

from openadapt import config
from openadapt.privacy.providers.private_ai import PrivateAIScrubbingProvider

scrub = PrivateAIScrubbingProvider()

try:
    scrub.scrub_text("hello Bob smith")
except (ValueError, requests.exceptions.HTTPError):
    import pytest

    pytestmark = pytest.mark.skip(
        reason="Private AI API key not found or invalid usage."
    )


URL = "https://api.private-ai.com/deid/v3/process/files/base64"
CONTENT_TYPE = "application/pdf"
TEST_PDF_PATH = "tests/assets/sample_llc_1.pdf"


def test_pdf_redaction() -> None:
    """Test to check that the PDF redaction works."""
    redacted_pdf_path = scrub.scrub_pdf(TEST_PDF_PATH)

    # Read from file
    with open(redacted_pdf_path, "rb") as b64_file:
        file_data = base64.b64encode(b64_file.read())
        file_data = file_data.decode("ascii")
    os.remove(redacted_pdf_path)

    payload = {
        "file": {"data": file_data, "content_type": CONTENT_TYPE},
        "entity_detection": {"accuracy": "high", "return_entity": True},
        "pdf_options": {"density": 150, "max_resolution": 2000},
        "audio_options": {"bleep_start_padding": 0, "bleep_end_padding": 0},
    }

    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": config.PRIVATE_AI_API_KEY,
    }

    response = requests.post(URL, json=payload, headers=headers)

    response = response.json()
    if isinstance(response, dict) and "details" in response:
        raise ValueError(response.get("details"))
    assert response.get("entities_present") is False


def test_image_redaction() -> None:
    """Test to check that the image redaction works."""
    image_path = "tests/assets/test_image_redaction_privateai.png"

    reader = easyocr.Reader(["en"])
    results = reader.readtext(image_path, detail=0)
    extract_original_text = " ".join(results)
    logger.debug(f"{extract_original_text=}")

    # Create a BytesIO buffer to read the image file
    with open(image_path, "rb") as image_file:
        image_buffer = BytesIO(image_file.read())

    # Open the image from the BytesIO buffer
    test_image_pil_data = Image.open(image_buffer)

    # Scrub the image using BytesIO
    scrubbed_test_image_pil_data = scrub.scrub_image(test_image_pil_data)

    # Create a BytesIO buffer to save the redacted image
    redacted_image_buffer = BytesIO()
    scrubbed_test_image_pil_data.save(redacted_image_buffer, format="PNG")

    # Read the redacted image data from the BytesIO buffer
    redacted_image_data = redacted_image_buffer.getvalue()
    redacted_image_buffer.close()

    # Save the redacted image to a temporary file
    redacted_image_path = image_path.split(".")[0] + "_redacted.png"
    with open(redacted_image_path, "wb") as redacted_file:
        redacted_file.write(redacted_image_data)

    # Read text from the redacted image
    results = reader.readtext(redacted_image_path, detail=0)
    extract_redacted_text = " ".join(results)
    logger.debug(f"{extract_redacted_text=}")
    os.remove(redacted_image_path)

    redact_text = scrub.scrub_text(extract_redacted_text)

    assert redact_text == extract_redacted_text


def test_empty_string() -> None:
    """Test empty string input for scrub function returns empty string."""
    text = ""
    expected_output = ""
    assert scrub.scrub_text(text) == expected_output


def test_no_scrub_string() -> None:
    """Test that the same string is returned."""
    text = "This string doesn't have anything to scrub."
    expected_output = "This string doesn't have anything to scrub."
    assert scrub.scrub_text(text) == expected_output


def test_scrub_email() -> None:
    """Test that the email address is scrubbed."""
    assert (
        scrub.scrub_text("My email is john.doe@example.com.")
        == "My email is [EMAIL_ADDRESS_1]."
    )


def test_scrub_phone_number() -> None:
    """Test that the phone number is scrubbed."""
    assert (
        scrub.scrub_text("My phone number is 123-456-7890.")
        == "My phone number is [PHONE_NUMBER_1]."
    )


def test_scrub_credit_card() -> None:
    """Test that the credit card number is scrubbed."""
    assert (
        scrub.scrub_text("My credit card number is 4234-5678-9012-3456 and ")
    ) == "My credit card number is [CREDIT_CARD_1] and "


def test_scrub_date_of_birth() -> None:
    """Test that the date of birth is scrubbed."""
    assert (
        scrub.scrub_text("My date of birth is 01/01/2000.")
        == "My date of birth is [DOB_1]."
    )


def test_scrub_address() -> None:
    """Test that the address is scrubbed."""
    assert (
        scrub.scrub_text("My address is 123 Main St, Toronto, On, CAN.")
        == "My address is [LOCATION_ADDRESS_1]."
    )


def test_scrub_ssn() -> None:
    """Test that the social security number is scrubbed."""
    # Test scrubbing of social security number
    assert (
        scrub.scrub_text("My social security number is 923-45-6789")
        == "My social security number is [SSN_1]"
    )


def test_scrub_dl() -> None:
    """Test that the driver's license number is scrubbed."""
    assert (
        scrub.scrub_text("My driver's license number is A123-456-789-012")
        == "My driver's license number is [DRIVER_LICENSE_1]"
    )


def test_scrub_passport() -> None:
    """Test that the passport number is scrubbed."""
    assert (
        scrub.scrub_text("My passport number is A1234567.")
        == "My passport number is [PASSPORT_NUMBER_1]."
    )


def test_scrub_national_id() -> None:
    """Test that the national ID number is scrubbed."""
    assert (
        scrub.scrub_text("My national ID number is 1234567890123.")
        == "My national ID number is [HEALTHCARE_NUMBER_1]."
    )


def test_scrub_routing_number() -> None:
    """Test that the bank routing number is scrubbed."""
    assert (
        scrub.scrub_text("My bank routing number is 123456789.")
        == "My bank routing number is [ROUTING_NUMBER_1]."
    )


def test_scrub_bank_account() -> None:
    """Test that the bank account number is scrubbed."""
    assert (
        scrub.scrub_text("My bank account number is 635526789012.")
        == "My bank account number is [BANK_ACCOUNT_1]."
    )


def test_scrub_all_together() -> None:
    """Test that all PII/PHI types are scrubbed."""
    text_with_pii_phi = (
        "John Smith's email is johnsmith@example.com and"
        " his phone number is 555-123-4567."
        "His credit card number is 4831-5538-2996-5651 and"
        " his social security number is 923-45-6789."
        " He was born on 01/01/1980."
    )
    assert (
        scrub.scrub_text(text_with_pii_phi)
        == "[NAME_1]'s email is [EMAIL_ADDRESS_1] and"
        " his phone number is [PHONE_NUMBER_1].His"
        " credit card number is [CREDIT_CARD_1] and"
        " his social security number is [SSN_1]."
        " He was born on [DOB_1]."
    )


def test_scrub_dict() -> None:
    """Test that the scrub_dict function works."""
    text_with_pii_phi = {"title": "hi my name is Bob Smith."}

    expected_output = {"title": "hi my name is [NAME_1]."}

    assert scrub.scrub_dict(text_with_pii_phi) == expected_output
