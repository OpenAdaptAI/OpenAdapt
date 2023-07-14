from transformers import pipeline
from loguru import logger
from PIL import Image
from typing import Optional
import numpy as np


def document_query(
    image: Optional[np.ndarray], image_path: Optional[str], question: str
) -> str:
    assert image or image_path, "At least one of image or image_path must be supplied"
    assert not (image and image_path), "Only one of image or image_path may be supplied"

    if image_path:
        document_image = Image.open(image_path).convert("RGB")
    else:
        document_image = image

    query_pipeline = pipeline(
        "document-question-answering", model="impira/layoutlm-document-qa"
    )

    output = query_pipeline(document_image, question)
    if output:
        return output[0]["answer"]
    logger.warning("Answer may be inaccurate as a non text-based file has been used")
    return output
