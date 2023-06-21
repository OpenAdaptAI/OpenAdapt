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
import nltk
from loguru import logger


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
