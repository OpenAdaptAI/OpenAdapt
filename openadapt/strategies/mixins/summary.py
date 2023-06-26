"""
Implements a ReplayStrategy mixin which summarizes the content of texts.


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
    """
    The summary of text ReplayStrategyMixin.
    """

    def __init__(
        self,
        recording: Recording,
    ):
        """
        See base class.

        Additional Attributes:
            - stemmer:
            - summarizer:
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
