import cv2
import numpy as np
from segment_anything import SamPredictor, sam_model_registry
from transformers import GPTJForCausalLM, GPT2Tokenizer
from paddleocr import PaddleOCR

import json
from typing import Dict

from common import MOUSE_EVENTS, KEY_EVENTS

# Initialize Segment Anything model
sam = sam_model_registry["<model_type>"](checkpoint="<path/to/checkpoint>")
sam_predictor = SamPredictor(sam)

# Initialize GPT-J model
tokenizer = GPT2Tokenizer.from_pretrained("EleutherAI/gpt-j-6B")
model = GPTJForCausalLM.from_pretrained("EleutherAI/gpt-j-6B")

# Initialize PaddleOCR model
ocr = PaddleOCR()

def generate_prompt(new_objects, prev_objects, new_text, prev_text):
    """
    Args:
        I have assumed new_object and prev_object have attributes of point_coords (x,y) and annotations
        according to what I have understood
        
        new_objects (dict):
        prev_objects (dict):
        new_text (str): extracted text from the new screenshot
        prev_text (str): ectracted text from the previous screenshot
    Returns:
        str: The generated textual prompt based on the inputs.
    """
    # Find objects that have appeared, disappeared or changed between new and previous screenshots
    new_labels = set(obj['annotations'] for obj in new_objects.values())
    prev_labels = set(obj['annotations'] for obj in prev_objects.values())
    appeared = new_labels - prev_labels
    disappeared = prev_labels - new_labels
    changed = set()
    for label in new_labels & prev_labels:
        new_coord = new_objects[next(k for k, v in new_objects.items() if v['annotations'] == label)]['point_coords']
        prev_coord = prev_objects[next(k for k, v in prev_objects.items() if v['annotations'] == label)]['point_coords']
        if new_coord != prev_coord:
            changed.add(label)
    
    # Generate textual prompt based on the segmented objects and extracted text
    prompt = ""
    if appeared:
        prompt += f"appeared: {', '.join(appeared)}.\n"
    if disappeared:
        prompt += f"disappeared: {', '.join(disappeared)}.\n"
    if changed:
        prompt += f"changed position: {', '.join(changed)}.\n"
    if new_text:
        prompt += f"new screenshot extracted text: {new_text}\n"
    if prev_text:
        prompt += f"previous screenshot extracted text: {prev_text}\n"
    
    return prompt

def predict_properties(prompt):
    """
    Predicts the properties of the next InputEvent using GPT-J model.
    assumption: MOUSE_EVENTS and KEY_EVENTS 
    Args:
        prompt (str): Textual prompt generated based on the segmented objects and extracted text.
        model (GPTJForCausalLM): GPT-J model used for prediction.
        tokenizer (GPT2Tokenizer): Tokenizer used to tokenize the prompt.

    Returns:
        A dictionary containing the predicted properties of the next InputEvent, including the event type,
        coordinates, and any other relevant properties.
    """
    # Tokenize the prompt and generate input_ids
    input_ids = tokenizer.encode(prompt, return_tensors="pt")

    # Generate properties using GPT-J model
    properties = model.generate(
        input_ids,
        do_sample=True,
        max_length=100,
        top_p=0.95,
        top_k=50,
        temperature=1.0,
        num_return_sequences=1,
    )

    # Decode the predicted properties into a string
    properties_str = tokenizer.decode(properties[0], skip_special_tokens=True)

    # Parse the properties string into a dictionary
    properties_dict = json.loads(properties_str)

    # Map the keys in the dictionary to the MOUSE_EVENTS and KEY_EVENTS constants
    properties_mapped = {}
    for k, v in properties_dict.items():
        if k == "type":
            if v in MOUSE_EVENTS:
                properties_mapped[k] = v
            elif v in KEY_EVENTS:
                properties_mapped[k] = v
            else:
                raise ValueError(f"Invalid event type: {v}")
        elif k == "x" or k == "y":
            properties_mapped[k] = int(v)
        elif k == "duration":
            properties_mapped[k] = float(v)
        elif k == "text":
            properties_mapped[k] = str(v)
        elif k == "scroll_direction":
            properties_mapped[k] = str(v)
        else:
            raise ValueError(f"Invalid property: {k}")

    return properties_mapped


def create_input_event(next_properties):
    """
    Creates a new InputEvent object based on the predicted properties.
    I have assumed InputEvent have attributes from common.py
    Args:
        next_properties (dict): A dictionary containing the predicted properties of the next InputEvent, including the event type, coordinates, and any other relevant properties.

    Returns:
        input_event (InputEvent): A new InputEvent object based on the predicted properties.
    """
    input_event = InputEvent()
    for event_type, event_properties in next_properties.items():
        if event_type in MOUSE_EVENTS:
            event = MouseEvent(event_type)
        elif event_type in KEY_EVENTS:
            event = KeyEvent(event_type)
        else:
            raise ValueError(f"Invalid event type: {event_type}")
        
        for prop_name, prop_value in event_properties.items():
            setattr(event, prop_name, prop_value)
        
        input_event.events.append(event)
    
    return input_event



def generate_input_event(new_screenshot, recording):
    # TODO: Implement the function to generate a new InputEvent based on the new Screenshot and the previous Recording
    #Get the latest screenshot in the recording
    prev_screenshot = recording.screenshots
    
    #Segment the objects in the new and previous screenshots
    new_objects = sam_predictor.set_image(new_screenshot)
    prev_objects = sam_predictor.set_image(prev_screenshot)
    
    #Extract text information from the new and previous screenshots
    new_text = ocr.ocr(new_screenshot)
    prev_text = ocr.ocr(prev_screenshot)
    
    #Generate textual prompts based on the segmented objects and extracted text
    prompt = generate_prompt(new_objects, prev_objects, new_text,prev_text)
    
    #Use the GPT-J model to predict the next InputeEvent properties
    next_properties = predict_properties(prompt)
    
    #Create a new InputEvent object based on the predicted properties
    new_event = create_input_event(next_properties)
    
    return new_event
