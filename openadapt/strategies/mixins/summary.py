"""
Implements a ReplayStrategy mixin which summarizes the content of texts.

Usage:

    class MyReplayStrategy(SummaryReplayStrategyMixin):
        ...
"""

from openadapt.models import Recording
from openadapt.strategies.base import BaseReplayStrategy
from sumy.summarizers.lsa import LsaSummarizer
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.nlp.stemmers import Stemmer
from sumy.utils import get_stop_words


class SummaryReplayStrategyMixin(BaseReplayStrategy):
    """ReplayStrategy mixin for summarizing text content."""

    def __init__(
        self, recording: Recording,
    ):
        """
        Initialize the SummaryReplayStrategyMixin.

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

    def get_summary(self, text: str, num_sentences: int,) -> str:
        """
        Generate a summary of the given text.

        Args:
            text (str): The text to summarize.
            num_sentences (int): The number of sentences to include in the summary.

        Returns:
            str: The summarized text.
        """
        parser = PlaintextParser.from_string(text, Tokenizer("english"))
        summarized = self.summarizer(parser.document, num_sentences)
        return summarized
