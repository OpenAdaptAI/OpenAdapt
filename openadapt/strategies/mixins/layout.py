"""
Implements a ReplayStrategy mixin for extracting layout and important
information from text documents and GUI images.

Usage:

    class MyReplayStrategy(LayoutExtractionReplayStrategyMixin):
        ...
"""
from PIL import Image
from typing import List
from transformers import pipeline
import numpy as np

from openadapt.models import Recording, Screenshot
from openadapt.strategies.base import BaseReplayStrategy


class LayoutExtractionReplayStrategyMixin(BaseReplayStrategy):

    def __init__(
        self,
        recording: Recording,
        img_list: List[str]
    ):
        super.__init__(recording)
        self.img_list = img_list
        
        for i in range(len(self.img_list)):
            wrongform = self.img_list[i]
            self.img_list[i] = Image.open(wrongform).convert('RGB')

    def document_query(
        self, 
        image: np.ndarray,
        question: str
    ) -> str:

        query_pipeline = pipeline("document-question-answering",
            model = "impira/layoutlm-document-qa")

        return query_pipeline(image, question)[0]['answer']