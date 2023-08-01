from loguru import logger
import lmql
from lmql.runtime.bopenai import get_stats

from openadapt.models import Recording, ActionEvent
from openadapt.strategies.mixins.lmql_action import LMQLReplayStrategyMixin, lmql_json

RECORDING = Recording()


class LMQLReplayStrategy(LMQLReplayStrategyMixin):
    """Custom Replay Strategy to solely test the LMQL Mixin."""

    def __init__(self, recording: Recording):
        super().__init__(recording)

    def get_next_action_event(self):
        pass


REPLAY = LMQLReplayStrategy(RECORDING)


def test_json():
    """Smoke test."""
    prompt = "Penelope entered 'P' into the website."
    actual = REPLAY.get_valid_json(prompt)
