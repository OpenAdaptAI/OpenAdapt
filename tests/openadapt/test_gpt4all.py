from openadapt.strategies.demo import DemoReplayStrategy
from openadapt.models import Recording


def test_gpt4all_initiation():
    """A smoke test for gpt4all.
    """
    recording = Recording()
    replay = DemoReplayStrategy(recording) 

    response = replay.get_gpt4all_completion("Does it work?")

    assert len(response) > 0