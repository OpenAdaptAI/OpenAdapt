"""Implements a ReplayStrategy mixin which summarizes the content of texts.

Usage:

    class MyReplayStrategy(SummaryReplayStrategyMixin):
        ...
"""

from loguru import logger
from sumy.nlp.stemmers import Stemmer
from sumy.nlp.tokenizers import Tokenizer
from sumy.parsers.plaintext import PlaintextParser
from sumy.summarizers.lsa import LsaSummarizer
from sumy.utils import get_stop_words
import nltk

from openadapt.models import Recording
from openadapt.strategies.base import BaseReplayStrategy


class SummaryReplayStrategyMixin(BaseReplayStrategy):
    """ReplayStrategy mixin for summarizing text content."""

    def __init__(
        self,
        recording: Recording,
    ) -> None:
        """Initialize the SummaryReplayStrategyMixin.

        Args:
            recording (Recording): The recording object.

        Additional Attributes:
            - stemmer: The stemmer for text processing.
            - summarizer: The summarizer for generating text summaries.
        """
        super().__init__(recording)
        self.stemmer = Stemmer("english")
        summarizer = LsaSummarizer(self.stemmer)
        summarizer.stop_words = get_stop_words("english")
        self.summarizer = summarizer

    def get_summary(
        self,
        text: str,
        num_sentences: int,
    ) -> str:
        """Generate a summary of the given text.

        Args:
            text (str): The text to summarize.
            num_sentences (int): The number of sentences to include in the summary.

        Returns:
            str: The summarized text.
        """
        while True:
            try:
                Tokenizer("english")
                break
            except Exception as e:
                logger.info(e)
                logger.info("Downloading punkt now")
                nltk.download("punkt")

        parser = PlaintextParser.from_string(text, Tokenizer("english"))
        summarized = self.summarizer(parser.document, num_sentences)
        return summarized
