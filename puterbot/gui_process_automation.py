from segment_anything import SamPredictor, sam_model_registry
#from transformers import GPTJForCausalLM, GPT2Tokenizer
from transformers import GPT2LMHeadModel, GPT2Tokenizer
from paddleocr import PaddleOCR
from puterbot.models import InputEvent
from puterbot.crud import get_screenshots
import json
import numpy as np
import cv2

import tempfile
from PIL import Image



# Initialize Segment Anything model
sam = sam_model_registry["vit_b"](checkpoint="C:/Users/avide/Downloads/sam_vit_b_01ec64.pth")
sam_predictor = SamPredictor(sam)

# Initialize GPT-J model
# tokenizer = GPT2Tokenizer.from_pretrained("EleutherAI/gpt-j-6B") # <-- consider using smaller model
# model = GPTJForCausalLM.from_pretrained("EleutherAI/gpt-j-6B") # <-- consider using smaller model
tokenizer = GPT2Tokenizer.from_pretrained("gpt2")
model = GPT2LMHeadModel.from_pretrained("gpt2")

# Initialize PaddleOCR model
ocr = PaddleOCR(lang='en') #specify languaget to ease processing

def save_temp_image(image):
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
    temp_file_path = temp_file.name

    image.save(temp_file_path)

    return temp_file_path

def generate_input_event(new_screenshot, recording):

    # load all relevant variables
    newImage = new_screenshot.image
    prevImage = get_screenshots(recording, True)[0].image

    # create temporary image paths
    newImage = save_temp_image(newImage)
    prevImage = save_temp_image(prevImage)

    newImageText = ocr.ocr(newImage)
    prevImageText = ocr.ocr(prevImage)

    
    #InputEventList = recording.screenshots

    # Use the Segment Anything library to segment the objects in the new and previous screenshots.
    # create masks for new and previous images
    sam_predictor.set_image(newImage)
    # newImageMask = sam_predictor.predict(new_screenshot)
    # prevImageMask = sam_predictor.predict(recording.screenshots.screenshot)
    
    


    recording = sam_predictor.predict(recording)
    new_screenshot = sam_predictor.predict(new_screenshot)
    # Use the PaddleOCR library to extract text from the new and previous screenshots.

    newImageText = json.dumps(newImageText)
    prevImageText = json.dumps(prevImageText)

    # Generate textual prompts based on the segmented objects and extracted text
    prompt = f"New Screenshot: {newImageText}, Previous Screenshot: {prevImageText}, Predict the next input event: "

    # Tokenize the prompt
    input_ids = tokenizer.encode(prompt, return_tensors="pt")

    # Use the GPT-J library to generate the next input event.
    output = model.generate(input_ids, max_length=100, num_return_sequences=1)
    output_text = tokenizer.decode(output[0], skip_special_tokens=True)

    # Parse the output text to extract the predicted InputEvent properties
    predicted_properties = json.loads(output_text)

    # Create a new InputEvent object based on the predicted properties
    new_input_event = InputEvent(**predicted_properties)

    return new_input_event