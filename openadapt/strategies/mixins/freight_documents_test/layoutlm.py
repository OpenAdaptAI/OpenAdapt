from PIL import Image
from transformers import LayoutLMv2Processor
import pytesseract
import numpy as np
import ipdb


# will convert to ReplayStrategy after get_layout is completed.
def get_layout(image: str):

        from transformers import LayoutLMv2Processor
        processor = LayoutLMv2Processor.from_pretrained(
                "microsoft/layoutlmv2-base-uncased")

        image = Image.open("load_manifest.png").convert("RGB")
        encoding = processor(image, return_tensors="pt") 
        # performs OCR to get the bounding boxes of words and the actual words
        # bounding boxes are normalized

        v = processor.decode(encoding['input_ids'][0])
        print(v)
        # LayoutLMV2 model has no heads on top, just a bare model. 

if __name__ == "__main__":
        get_layout("test.png")