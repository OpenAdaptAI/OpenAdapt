from openadapt.models import Recording
from openadapt.strategies.mixins.mpt7b import MPT7BReplayStrategy


def mpt7b_smoke_test():
    mpt7b_test_instance = MPT7BReplayStrategy(Recording())

    test_str = "This sentence is "

    completion_response = mpt7b_test_instance.get_completion(test_str)

    assert len(completion_response) > 0
