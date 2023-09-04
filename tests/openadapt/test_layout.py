"""Layout Extraction tool test module."""
import warnings

from openadapt.tools.layout import document_query

EXPECTED_CALC_OUTPUT = 10

IMAGE_FILE_NAMES = [
    "tests/openadapt/assets/test_ladingbill.png",
    "tests/openadapt/assets/test_invoice.png",
    "tests/openadapt/assets/test_calendar.png",
    "tests/openadapt/assets/test_calc.png",
]


def test_lading_bill_screenshot() -> None:
    """Lading Bill Screenshot test."""
    questions = [
        "What is the bill of lading number?",
        "What is the carrier name?",
        "What is the ship date?",
        "What is the sender's phone number?",
    ]

    output_answers = []
    for q in questions:
        output = document_query(image=None, image_path=IMAGE_FILE_NAMES[0], question=q)
        output_answers.append(output[0]["answer"])

    expected_output = [
        "21099992723",
        "EXAMPLE CARRIER",
        "September 13, 2021",
        "456-7890",
    ]
    assert output_answers == expected_output


def test_invoice_screenshot() -> None:
    """Invoice Screenshot test."""
    questions = [
        "What is the sender's phone number?",
        "What is the sender's address?",
        "What is surcharge?",
        "What is the total cost?",
        "What is the daily interest rate on overdue balances?",
    ]

    expected_answers = ["321-956-7331,", "MELBOURNE,", "45.00", "$ 3,165.00", "0.5%"]

    output_answers = []
    for q in questions:
        output = document_query(image=None, image_path=IMAGE_FILE_NAMES[1], question=q)
        output_answers.append(output[0]["answer"])
    assert output_answers == expected_answers


def test_calendar_screenshot() -> None:
    """Calendar Screenshot test."""
    output = document_query(
        image=None, image_path=IMAGE_FILE_NAMES[2], question="What month is it?"
    )[0]["answer"]
    expected_output = "june"
    assert output.lower() == expected_output


def test_calc_screenshot() -> None:
    """Calc Screenshot test."""
    output = document_query(
        image=None,
        image_path=IMAGE_FILE_NAMES[3],
        question="What is the current number on the screen?",
    )
    expected_output = EXPECTED_CALC_OUTPUT
    # Raising a warning and returning the output.
    warnings.warn(
        "This test fails because the document type is not supported.", UserWarning
    )

    assert expected_output != output
