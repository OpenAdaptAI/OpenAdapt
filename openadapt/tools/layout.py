"""Tool for layout extraction."""
from typing import Optional

from loguru import logger
from PIL import Image
from transformers import pipeline
import numpy as np


def document_query(
    image: Optional[np.ndarray], image_path: Optional[str], question: str
) -> str:
    """Function that queries the LayoutLM Document QA model."""
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

    # TODO: output on GUI may not always be correct (empty or just wrong)
    # how do we handle this?
    if not output:
        logger.warning(f"empty {output=}")
    return output
