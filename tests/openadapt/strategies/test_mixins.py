import pytest
from PIL import Image, ImageDraw, ImageFont
from openadapt.models import Recording, Screenshot
from openadapt.strategies.mixins import OCRReplayStrategyMixin

def test_ocr_replay_strategy_mixin():
    mixin = OCRReplayStrategyMixin(Recording())
    text = "Sample Text"
    image = create_image_with_text(text)
    screenshot = Screenshot(image=image)
    ocr_text = mixin.get_ocr_text(screenshot)
    assert ocr_text == text

def create_image_with_text(text):
    font_size = 30
    font = ImageFont.truetype("arial.ttf", font_size)
    text_size = font.getsize(text)
    image = Image.new("RGB", (text_size[0] + 10, text_size[1] + 10), color=(255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.text((5, 5), text, font=font, fill=(0, 0, 0))
    return image

if __name__ == "__main__":
    pytest.main(["-v", "test_mixins.py"])