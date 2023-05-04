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

    def __init__(
            self,
            recording: Recording,
    ):
        super().__init__(recording)

    def get_summary(
            self,
            ascii_curr: str,
            ascii_prev: str,
            ocr_curr: str,
            ocr_prev: str
    ):
        """
        Takes the ASCII and OCR text of the current and previous screenshot
        and returns the similarity score between the given screenshot
        and the screenshot before.

        The similarity score is the mean of the following 2 scores:
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
        text,
):
    # replace the irrelevant symbols in the ascii with spaces
    no_symbols = re.sub(r'[^\w\s]+', '', text)

    ascii_words = []

    for word in no_symbols:
        if wordnet.synsets(word):
            ascii_words.append(word)
    return str(ascii_words)


# takes 2 lists of strings and returns how similar they are as a float
def compare_text(
        text1,
        text2
):
    stemmer = Stemmer("english")
    summarizer = LsaSummarizer(stemmer)
    summarizer.stop_words = get_stop_words("english")

    parser1 = PlaintextParser.from_string(text1, Tokenizer("english"))
    summarized1 = summarizer(parser1.document, 1)

    parser2 = PlaintextParser.from_string(text2, Tokenizer("english"))
    summarized2 = summarizer(parser2.document, 1)

    # may want to change to something more complex
    return fuzz.ratio(summarized1, summarized2)
