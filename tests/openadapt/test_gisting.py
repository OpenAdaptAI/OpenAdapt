import modal
import pytest
from openadapt.ml.gist_provider.gist_provider import execute, stub


def test_gisting():
    """
    A smoke test for gisting using a sequence of prime numbers
    that also shows the compression factor for the input provided.
    """
    test_prompt = (
        "Please complete the following sequence of numbers: 2, 3, 5, 7, 11, 13"
    )
    with stub.run():
        output = execute(test_prompt)

    print(output)

    assert len(output) > 0
