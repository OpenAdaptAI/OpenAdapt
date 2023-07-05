"""
Tests the summarization function in summary.py
"""
from fuzzywuzzy import fuzz
import pytest

from openadapt.strategies.mixins.summary import SummaryReplayStrategyMixin
from openadapt.models import Recording


class SummaryReplayStrategy(SummaryReplayStrategyMixin):
    """Custom Replay Strategy to solely test the Summary Mixin."""

    def __init__(self, recording: Recording):
        super().__init__(recording)

    def get_next_action_event(self):
        pass


@pytest.fixture(scope="module")
def replay_fixture(setup_database):
    recording = Recording()  # Use the Recording class from your code
    setup_database()  # Set up the database
    replay = setup_database(recording)
    yield replay


def test_summary_empty(replay_fixture):
    empty_text = ""
    actual = replay_fixture.get_summary(empty_text, 1)
    assert len(actual) == 0


def test_summary_sentence(replay_fixture):
    story = "However, this bottle was not marked “poison,” so Alice ventured to taste it, \
        and finding it very nice, (it had, in fact, a sort of mixed flavour of cherry-tart, \
        custard, pine-apple, roast turkey, toffee, and hot buttered toast,) \
        she very soon finished it off."
    actual = replay_fixture.get_summary(story, 1)
    assert fuzz.WRatio(actual, story) > 50
