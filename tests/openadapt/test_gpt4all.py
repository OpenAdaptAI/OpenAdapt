from openadapt.strategies.mixins.gpt4all import GPT4ALLReplayStrategyMixin
from openadapt.models import Recording


RECORDING = Recording()


class GPT4AllReplayStrategy(GPT4ALLReplayStrategyMixin):
    """Custom Replay Strategy to solely test the GPT4All Mixin."""

    def __init__(self, recording: Recording):
        super().__init__(recording)

    def get_next_action_event(self):
        pass


def test_gpt4all_initiation():
    """A smoke test for gpt4all."""
    recording = Recording()
    replay = GPT4AllReplayStrategy(recording)

    response = replay.get_gpt4all_completion("Does it work?")

    assert len(response) > 0
