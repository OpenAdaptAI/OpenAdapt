from segment_anything import SamPredictor, sam_model_registry
from transformers import GPTJForCausalLM, GPT2Tokenizer
from paddleocr import PaddleOCR
from puterbot.models import InputEvent
import json
import numpy as np



# Initialize Segment Anything model
sam = sam_model_registry["<model_type>"](checkpoint="<path/to/checkpoint>")
sam_predictor = SamPredictor(sam)

# Initialize GPT-J model
tokenizer = GPT2Tokenizer.from_pretrained("EleutherAI/gpt-j-6B")
model = GPTJForCausalLM.from_pretrained("EleutherAI/gpt-j-6B")

# Initialize PaddleOCR model
ocr = PaddleOCR()

def generate_input_event(new_screenshot, recording):

    # load all relevant variables
    newImage = new_screenshot.image
    prevImage = recording.screenshots.screenshot.image
    newImageText = ocr.ocr(newImage)
    prevImageText = ocr.ocr(prevImage)

    InputEventList = recording.screenshots

    # Use the Segment Anything library to segment the objects in the new and previous screenshots.
    # create masks for new and previous images
    newImageMask = sam_predictor.predict(new_screenshot)
    prevImageMask = sam_predictor.predict(recording.screenshots.screenshot)
    
    


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