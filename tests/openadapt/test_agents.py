"""
Note: requires valid OpenAI API key to run tests. May use disk space for caching.
"""

import pytest
import sys
import io
from PIL import Image
from openadapt import config
from openadapt.crud import get_latest_recording
from openadapt.strategies.mixins.agents import TransformersAgentsMixin


# requires valid recording
@pytest.mark.skipif(get_latest_recording() is None, reason="no recording")
def test_prompt():
    agent = TransformersAgentsMixin(
        recording=get_latest_recording(), api_key=config.OPENAI_API_KEY
    )
    assert agent.prompt(debug=False)


def test_captioning():
    agent = TransformersAgentsMixin(recording=None, api_key=config.OPENAI_API_KEY)
    assert agent.chat("caption the image", image=Image.open("assets/visualize.png"))


if __name__ == "__main__":
    agent = TransformersAgentsMixin(
        recording=get_latest_recording(), api_key=config.OPENAI_API_KEY
    )
    agent.prompt(debug=True)
