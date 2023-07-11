from typing import List
from openadapt.models import Recording
from PIL import Image
from transformers import pipeline
import numpy as np


class TestQAPipeline:
    def __init__(self, image_file_paths: List[str]):
        self.image_list = [
            Image.open(img_file_path).convert("RGB")
            for img_file_path in image_file_paths
        ]

    def document_query(self, image: np.ndarray, question: str) -> str:
        query_pipeline = pipeline(
            "document-question-answering", model="impira/layoutlm-document-qa"
        )
        output = query_pipeline(image, question)
        if output:
            return query_pipeline(image, question)[0]["answer"]
        return "Unsupported document type, please input a text-based document"


IMAGE_LIST = [
    "test_ladingbill.png",
    "test_invoice.png",
    "test_calendar.png",
    "test_calc.png",
]

LAYOUT_OBJ = TestQAPipeline(image_file_paths=IMAGE_LIST)


def test_lading_bill_screenshot():
    questions = [
        "What is the bill of lading number?",
        "What is the carrier name?",
        "What is the ship date?",
        "What is the sender's phone number?",
    ]

    output_answers = []
    for q in questions:
        output = LAYOUT_OBJ.document_query(image=LAYOUT_OBJ.image_list[0], question=q)
        output_answers.append(output)

    expected_output = [
        "21099992723",
        "EXAMPLE CARRIER",
        "September 13, 2021",
        "456-7890",
    ]
    assert output_answers == expected_output


def test_invoice_screenshot():
    questions = [
        "What is the sender's phone number?",
        "What is the sender's address?",
        "What is surcharge?",
        "What is the total cost?",
        "What is the daily interest rate on overdue balances?",
    ]

    answers = ["321-956-7331,", "MELBOURNE,", "45.00", "$ 3,165.00", "0.5%"]

    output_answers = []
    for q in questions:
        output = LAYOUT_OBJ.document_query(image=LAYOUT_OBJ.image_list[1], question=q)
        output_answers.append(output)
    assert output_answers == answers


def test_calendar_screenshot():
    output = LAYOUT_OBJ.document_query(
        image=LAYOUT_OBJ.image_list[2], question="What month is it?"
    )
    assert output == "June" or output == "JUNE"


def test_calc_screenshot():
    output = LAYOUT_OBJ.document_query(
        image=LAYOUT_OBJ.image_list[3],
        question="What is the current number on the screen?",
    )
    assert (
        output == "10"
        or output == "Unsupported document type, please input a text-based document"
    )


if __name__ == "__main__":
    test_calc_screenshot()
    test_calendar_screenshot()
    test_invoice_screenshot()
    test_lading_bill_screenshot()
