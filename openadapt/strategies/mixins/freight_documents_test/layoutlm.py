from PIL import Image

import pytesseract
import numpy as np
import ipdb


# will convert to ReplayStrategy after get_layout is completed.
def get_layout(image: str):

        from transformers import LayoutLMv2Processor, LayoutLMv2ForTokenClassification
        processor = LayoutLMv2Processor.from_pretrained(
                "microsoft/layoutlmv2-base-uncased")

        image = Image.open("rate_confirmation.png").convert("RGB")
        encoding = processor(image, truncation=True, return_offsets_mapping=True, return_tensors="pt") 
        # performs OCR to get the bounding boxes of words and the actual words
        # bounding boxes are normalized
        offset_map = encoding.pop('offset-mapping')

        # we have encoded keys now 


        model = LayoutLMv2ForTokenClassification.from_pretrained(
                 "microsoft/layoutlmv2-base-uncased")      
        
        # forward pass ?
        output = model(**encoding)

        # predictions and token boxes 
        predictions = output.logits.argmax(-1).squeeze().tolist()
        print(predictions)

        token_boxes = encoding.bbox.squeeze().tolist()
        print(token_boxes)

        # have both predictions and token boxes, but i want the labels
        # to be generated themselves..

 


def unnormalize_box(bbox, width, height):
     return [
         width * (bbox[0] / 1000),
         height * (bbox[1] / 1000),
         width * (bbox[2] / 1000),
         height * (bbox[3] / 1000),
     ]
        # LayoutLMV2 model has no heads on top, just a bare model. 

if __name__ == "__main__":
        get_layout("test.png")