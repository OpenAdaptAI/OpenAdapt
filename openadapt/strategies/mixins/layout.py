"""
Implements a ReplayStrategy mixin for extracting layout and important
information from text documents and GUI images.

Usage:

    class MyReplayStrategy(LayoutExtractionReplayStrategyMixin):
        ...
"""
from typing import List
import numpy as np

from openadapt.models import Recording, Screenshot
from openadapt.strategies.base import BaseReplayStrategy
from transformers import pipeline


class LayoutExtractionReplayStrategyMixin(BaseReplayStrategy):

    def __init__(
        self, 
        recording: Recording,
        img_list: List[np.ndarray]
    ):
        super.__init__(recording)
        self.img_list = img_list

    def document_query(
        self, 
        image: np.ndarray,
        question: str
    ) -> str:

        query_pipeline = pipeline("document-question-answering",
            model = "impira/layoutlm-document-qa")

        return query_pipeline(image, question)[0]['answer']