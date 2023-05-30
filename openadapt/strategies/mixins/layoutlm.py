from PIL import Image
from transformers import LayoutLMv2ImageProcessor, LayoutLMv2TokenizerFast, LayoutLMv2Processor, LayoutLMv2ForTokenClassification
import pytesseract
import numpy as np

# will convert to ReplayStrategy after get_layout is completed. Want to run it for now.
def get_layout(image: str):

        feature_extractor = LayoutLMv2ImageProcessor()  # apply_ocr is set to True by default
        tokenizer = LayoutLMv2TokenizerFast.from_pretrained("microsoft/layoutlmv2-base-uncased")
        processor = LayoutLMv2Processor(feature_extractor, tokenizer, apply_ocr=True)

        img = Image.open(image).convert("RGB")
        encode = processor(img, return_tensors="pt")

        
        #(['input_ids', 'token_type_ids', 'attention_mask', 'bbox', 'image'])
        # document cant be too long.. a drawback. Max token sequence length is 512 for layoutmv2
        print(encode.keys())

        model = LayoutLMv2ForTokenClassification.from_pretrained("microsoft/layoutlmv2-base-uncased")

        output = model(**encode)
        ## interpret output.. but how


if __name__ == "__main__":
        get_layout("test.png")


