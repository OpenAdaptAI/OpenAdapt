"""
Tests the get_summary function in summary_mixin.py
"""
from fuzzywuzzy import fuzz

from puterbot.strategies.demo import DemoReplayStrategy
from puterbot.models import Recording, Screenshot

RECORDING = Recording()
REPLAY = DemoReplayStrategy(RECORDING)

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
