import PIL

import pytesseract
import numpy as np
import ipdb


# will convert to ReplayStrategy after get_layout is completed.
def get_layout(image: str):

        from transformers import LayoutLMv2Processor, LayoutLMv2ForTokenClassification
        import tensorflow as tf

        image = PIL.Image.open("rate_confirmation.png").convert("RGB")
        
        classes = ['TABLE','CONTACT_INFO','PRICE','FAX_NUMBER','PHONE_NUMBER','ADDRESS','LOGO']
        id2label = {v: k for v, k in enumerate(classes)}

        processor = LayoutLMv2Processor.from_pretrained(
                "microsoft/layoutlmv2-base-uncased")

        encoding = processor(image,truncation=True,return_offsets_mapping=True,return_tensors="pt")
        # Can't pre load word labels if we're doing OCR, basically need to train 
        # the model otherwise it just mislabels/mistokenizes as observed.


        # we have to truncate the data otherwise classification doesnt work
        offsets = encoding.pop('offset_mapping')
        token_classifier = LayoutLMv2ForTokenClassification.from_pretrained('microsoft/layoutlmv2-base-uncased')
        # iffy on the model tbh

        ocr_output = processor.tokenizer.decode(encoding['input_ids'][0])
        #print(ocr_output)

        bounding_boxes_normalized = encoding.bbox.squeeze().tolist()
        width, height = image.size

        # offset map, subword
        subword = np.array(offsets.squeeze().tolist())[:,0] != 0
        bboxes = [unnormalize_box(box, width, height) for idx, box in enumerate(bounding_boxes_normalized) if not subword[idx]]
        #print(bboxes)
        

        classification_output = token_classifier(**encoding)

        prediction_indices = classification_output.logits.argmax(-1).squeeze().tolist()
        true_predictions = [id2label[pred] for idx, pred in enumerate(prediction_indices) if not subword[idx]]
        # this is just 0's and 1's.
        print(true_predictions)

        class_color = {'TABLE':"red",'CONTACT_INFO':"blue",'PRICE':"green",'FAX_NUMBER':"purple",'PHONE_NUMBER':"pink",'ADDRESS':"violet",
        'LOGO':"yellow"}
        test_img = PIL.ImageDraw.Draw(image)

        for prediction, box in zip(true_predictions, bboxes):
                test_img.rectangle(box, outline=class_color[prediction])
                test_img.text((box[0]+12, box[1]-12), text=prediction, fill=class_color[prediction])
        test_img._image.show()


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