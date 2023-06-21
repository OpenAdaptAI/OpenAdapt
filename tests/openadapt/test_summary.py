"""
Tests the summarization function in summary.py
"""
from fuzzywuzzy import fuzz

from openadapt.strategies.mixins.summary import SummaryReplayStrategyMixin
from openadapt.models import Recording


RECORDING = Recording()


class SummaryReplayStrategy(SummaryReplayStrategyMixin):
    """Custom Replay Strategy to solely test the Summary Mixin."""

    def __init__(self, recording: Recording):
        super().__init__(recording)

    def get_next_action_event(self):
        pass


REPLAY = SummaryReplayStrategy(RECORDING)


def test_summary_empty():
    empty_text = ""
    actual = REPLAY.get_summary(empty_text, 1)
    assert len(actual) == 0


def test_summary_sentence():
    story = "However, this bottle was not marked “poison,” so Alice ventured to taste it, \
        and finding it very nice, (it had, in fact, a sort of mixed flavour of cherry-tart, \
        custard, pine-apple, roast turkey, toffee, and hot buttered toast,) \
        she very soon finished it off."
    actual = REPLAY.get_summary(story, 1)
    assert fuzz.WRatio(actual, story) > 50
