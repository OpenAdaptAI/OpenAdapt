import pytest
from openadapt.strategies.mixins import ascii, huggingface, ocr, openai
from openadapt.models import Recording, Screenshot
from PIL import Image
import numpy as np

# Constants
COLUMNS = 120
WIDTH_RATIO = 2.2
MONOCHROME = True
MODEL_NAME = "gpt2"
MAX_INPUT_SIZE = 1024
TEST_IMAGE_PATH = "tests/data/test_image.png"


# Test data
recording = Recording()
screenshot = Screenshot(image=Image.open(TEST_IMAGE_PATH), array=np.array(Image.open(TEST_IMAGE_PATH)))


def test_ascii_replay_strategy_mixin():
    ascii_mixin = ascii.ASCIIReplayStrategyMixin(recording)
    ascii_text = ascii_mixin.get_ascii_text(screenshot, monochrome=MONOCHROME, columns=COLUMNS, width_ratio=WIDTH_RATIO)
    assert isinstance(ascii_text, str)


def test_hugging_face_replay_strategy_mixin():
    hf_mixin = huggingface.HuggingFaceReplayStrategyMixin(recording, model_name=MODEL_NAME, max_input_size=MAX_INPUT_SIZE)
    prompt = "What is the capital of France?"
    max_tokens = 10
    completion = hf_mixin.get_completion(prompt, max_tokens)
    assert isinstance(completion, str)


def test_ocr_replay_strategy_mixin():
    ocr_mixin = ocr.OCRReplayStrategyMixin(recording)
    ocr_text = ocr_mixin.get_ocr_text(screenshot)
    assert isinstance(ocr_text, str)


def test_openai_replay_strategy_mixin():
    openai_mixin = openai.OpenAIReplayStrategyMixin(recording, model_name=openai.MODEL_NAME)
    prompt = "What is the capital of France?"
    system_message = "You are an AI language model and will answer questions."
    completion = openai_mixin.get_completion(prompt, system_message)
    assert isinstance(completion, str)
