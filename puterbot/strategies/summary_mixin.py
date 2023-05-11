"""
Implements a ReplayStrategy mixin which identifies whether windows are different
from summarizing their contents.

Usage:

    $ python puterbot/summary_mixin.py SummaryReplayStrategyMixin
"""
from puterbot.models import Recording
from puterbot.strategies.base import BaseReplayStrategy


from sumy.summarizers.lsa import LsaSummarizer
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.nlp.stemmers import Stemmer
from sumy.utils import get_stop_words
from nltk.corpus import wordnet
import re
from fuzzywuzzy import fuzz
import statistics


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
        """
        super().__init__(recording)

    def get_summary(
        self, ascii_curr: str, ascii_prev: str, ocr_curr: str, ocr_prev: str
    ) -> float:
        """Returns how similar the contents of 2 screenshots are.

        Args:
            self: the SummaryReplayStrategyMixin
            ascii_curr: the ascii text of the current screenshot
            ascii_prev: the ascii text of the previous screenshot
            ocr_curr: the ocr text of the current screenshot
            ocr_prev: the ocr text of the previous screenshot

        Returns:
            A float which represents the mean of the following 2 similarity values:
                1. how similar the summaries of the current and previous ASCII text are
                2. how similar the summaries of the current and previous OCR text are
        """
        cleaned_ascii_curr = clean_ascii(ascii_curr)
        cleaned_ascii_prev = clean_ascii(ascii_prev)

        ascii_similarity = compare_text(cleaned_ascii_curr, cleaned_ascii_prev)
        ocr_similarity = compare_text(ocr_curr, ocr_prev)

        similarity_list = [ascii_similarity, ocr_similarity]

        # may want the required minimum similarity
        return statistics.fmean(similarity_list)


def clean_ascii(
    text: str,
) -> str:
    """
    Returns a string of the words in the inputted text.
    """
    # remove the irrelevant symbols in the ascii and returns a list of strings
    no_symbols = re.sub(r"[^\w\s]+", "", text)

    ascii_words = []

    for word in no_symbols:
        if wordnet.synsets(word):
            ascii_words.append(word)
    return "".join(ascii_words)


def compare_text(text1: str, text2: str) -> float:
    """
    Returns a float value representing how similar the 2 strings are.
    """
    stemmer = Stemmer("english")
    summarizer = LsaSummarizer(stemmer)
    summarizer.stop_words = get_stop_words("english")

    parser1 = PlaintextParser.from_string(text1, Tokenizer("english"))
    summarized1 = summarizer(parser1.document, 1)

    parser2 = PlaintextParser.from_string(text2, Tokenizer("english"))
    summarized2 = summarizer(parser2.document, 1)

    # may want to change to something more complex
    return fuzz.ratio(summarized1, summarized2)
