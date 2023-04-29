import pytesseract
from PIL import Image

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

# Load pre-trained GPT-2 model
model_name = 'gpt2'
tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoModelForCausalLM.from_pretrained(model_name)

class OCRReplayMixin:
    def get_text_from_screenshot(self, screenshot):
        # Convert the screenshot to text using OCR
        text = pytesseract.image_to_string(screenshot)
        
        # Encode the OCR text using the tokenizer
        input_ids = tokenizer.encode(text, return_tensors='pt')
        
        # Generate text using the pre-trained model
        output_ids = model.generate(input_ids=input_ids, max_length=100)
        output_text = tokenizer.decode(output_ids[0], skip_special_tokens=True)
        
        return output_text
