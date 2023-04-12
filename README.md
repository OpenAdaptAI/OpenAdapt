# GUI Process Automation with Transformers

Welcome to GUI Process Automation with Transformers! We are working with a dataset of user input events, screenshots, and window events. Our task is to generate the appropriate InputEvent(s) based on the previously recorded InputEvents and associated Screenshots, such that the original goal of the user in the recording is accomplished, while accounting for differences in screen resolution, window size, and application behavior.

## Problem Statement

Given a new Screenshot, we want to generate the appropriate InputEvent(s) based on the previously recorded InputEvents, where each Screenshot is taken immediately before its associated InputEvent. We need to account for differences in screen resolution, window size, and application behavior. InputEvents contain raw mouse and keyboard data which have been aggregated to remove unnecessary events.

## Dataset

The dataset consists of the following entities: 
1. `Recording`: Contains information about the screen dimensions, platform, and other metadata. 
2. `InputEvent`: Represents a user input event such as a mouse click or key press. Each InputEvent has an associated Screenshot taken immediately before the event. 
3. `Screenshot`: Contains the PNG data of a screenshot taken during the recording. 
4. `WindowEvent`: Represents a window event such as a change in window title, position, or size.

You can assume that you have access to the following functions: 
- `get_recording()`: Gets the latest recording. 
- `get_events(recording)`: Returns a list of `InputEvent` objects for the given recording.

## Requirements 

1. Fork this repository and clone it to your local machine. 
2. Get puterbot up and running by following the instructions in puterbot/README.md
3. Implement a Python function `generate_input_event(new_screenshot, input_events)`, where: 
- `new_screenshot`: A `Screenshot` object representing the new screenshot. 
- `input_events`: A list of `InputEvent` objects from the original recording, with each InputEvent having an associated Screenshot.

This function should return a new `InputEvent` object that can be used to replay the recording, taking into account differences in screen resolution, window size, and application behavior.

3. Integrate the [Segment Anything](https://github.com/facebookresearch/segment-anything)  library, [HuggingFace GPT-J](https://huggingface.co/transformers/model_doc/gptj.html)  (or a similar transformer model), and [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)  to assist in processing the screenshots and improving the generation of new InputEvents. These tools will help you determine the properties of the next InputEvent by segmenting the objects in the screenshots, generating textual prompts for GPT-J, and extracting text information from the images, respectively. Follow the installation instructions provided in their READMEs to set up the libraries in your environment. 

4. Write unit tests for your implementation.

## Evaluation Criteria

Your submission will be evaluated based on the following criteria: 

1. **Functionality** : Your implementation should correctly generate the new `InputEvent` objects based on the provided data and the Segment Anything, GPT-J, and/or PaddleOCR libraries. 

2. **Code Quality** : Your code should be well-structured, clean, and easy to understand. 

3. **Scalability** : Your solution should be efficient and scale well with large datasets. 

4. **Testing** : Your tests should cover various edge cases and scenarios to ensure the correctness of your implementation.

## Submission

1. Commit your changes to your forked repository.

2. Create a pull request to the original repository with your changes.

3. In your pull request, include a brief summary of your approach, any assumptions you made, and how you integrated the SegmentAnything, GPT-J, and PaddleOCR libraries.

4. Bonus: interacting with ChatGPT and/or other language transformer models in order to generate code and/or evaluate design decisions is strongly encouraged. If you choose to do so, please include the full transcript.

## Getting Started

Here are some stubs and suggestions to help you get started with your implementation: 

1. Set up your Python environment and install the required libraries (Segment Anything, HuggingFace Transformers, and PaddleOCR). 

2. Create a new file, `gui_process_automation.py`, and import the necessary libraries:

```python

import cv2
import numpy as np
from segment_anything import SamPredictor, sam_model_registry
from transformers import GPTJForCausalLM, GPT2Tokenizer
from paddleocr import PaddleOCR
```


1. Initialize the models:

```python

# Initialize Segment Anything model
sam = sam_model_registry["<model_type>"](checkpoint="<path/to/checkpoint>")
sam_predictor = SamPredictor(sam)

# Initialize GPT-J model
tokenizer = GPT2Tokenizer.from_pretrained("EleutherAI/gpt-j-6B")
model = GPTJForCausalLM.from_pretrained("EleutherAI/gpt-j-6B")

# Initialize PaddleOCR model
ocr = PaddleOCR()
```

 
1. Create the `generate_input_event` function:

```python

def generate_input_event(new_screenshot, input_events):
    # TODO: Implement the function to generate a new InputEvent based on the previous InputEvents and the new Screenshot
    pass
```

 
1. In the `generate_input_event` function, you may want to follow these steps:

a. Use the Segment Anything library to segment the objects in the new screenshot.

b. Use the PaddleOCR library to extract text information from the new screenshot.

c. Generate textual prompts based on the segmented objects and extracted text, and use the GPT-J model to predict the next InputEvent properties.

d. Create a new InputEvent object based on the predicted properties and return it. 

2. Write unit tests for your implementation in a separate file, `test_gui_process_automation.py`.

## Wrapping Up

Once you have implemented the `generate_input_event` function and written unit tests, commit your changes to your forked repository, create a pull request, and provide a brief summary of your approach, assumptions, and library integrations.

We hope that these stubs and suggestions will help you get started with your implementation. Good luck!

## Submitting an Issue

Please submit any issues to https://github.com/MLDSAI/puterbot/issues with the
following information:

- Problem description (include any relevant console output and/or screenshots)
- Steps to reproduce (required in order for others to help you)
