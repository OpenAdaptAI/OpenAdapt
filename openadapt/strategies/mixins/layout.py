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

from openadapt.models import Recording, Screenshot, ActionEvent
from openadapt.strategies.base import BaseReplayStrategy


class LayoutExtractionReplayStrategyMixin(BaseReplayStrategy):

    def __init__(self, recording: Recording, image_file_paths: List[str]):
        super().__init__(recording)
        self.images = [
            Image.open(img_file_path).convert("RGB")
            for img_file_path in image_file_paths
        ]

    def document_query(self, image: np.ndarray, question: str) -> str:
        query_pipeline = pipeline(
            "document-question-answering", 
            model="impira/layoutlm-document-qa"
        )
        output = query_pipeline(image, question)
        if output != []:
            return query_pipeline(image, question)[0]['answer']
        return "Unsupported document type, please input a text-based document"
    
    def get_next_action_event(self, screenshot: Screenshot) -> ActionEvent:
        return super().get_next_action_event(screenshot)