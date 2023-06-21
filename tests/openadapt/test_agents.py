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
    f = io.StringIO()
    old = sys.stdout
    sys.stdout = f
    agent.prompt(debug=False)
    sys.stdout = old
    assert "print" in f.getvalue()


def test_captioning():
    agent = TransformersAgentsMixin(recording=None, api_key=config.OPENAI_API_KEY)
    f = io.StringIO()
    old = sys.stdout
    sys.stdout = f
    agent.chat("caption the image", image=Image.open("assets/visualize.png"))
    sys.stdout = old
    assert "print" in f.getvalue()
