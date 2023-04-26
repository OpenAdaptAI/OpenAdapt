import io
from pprint import pformat
import numpy as np
from segment_anything import SamPredictor, sam_model_registry
from strategies.ocr_mixin import OCRReplayStrategyMixin
from transformers import GPTJForCausalLM, GPT2Tokenizer
from paddleocr import PaddleOCR
import time
from PIL import Image

from loguru import logger
from puterbot.events import get_events
from puterbot.utils import display_event, rows2dicts
from puterbot.models import Recording, Screenshot

DISPLAY_EVENTS = False
REPLAY_EVENTS = True
SLEEP = True


class SamReplayStrategy(OCRReplayStrategyMixin):
    def __init__(
            self,
            recording: Recording,
            display_events=DISPLAY_EVENTS,
            replay_events=REPLAY_EVENTS,
            sleep=SLEEP,
    ):
        super().__init__(recording)
        self.processed_input_events = get_events(recording, process=True)
        self.display_events = display_events
        self.replay_events = replay_events
        self.sleep = sleep
        self.prev_timestamp = None
        self.sam_model = sam_model_registry["default"](checkpoint="puterbot/checkpoint")
        self.sam_predictor = SamPredictor(self.sam_model)
        self.tokenizer = GPT2Tokenizer.from_pretrained("EleutherAI/gpt-j-6B")
        self.model = GPTJForCausalLM.from_pretrained("EleutherAI/gpt-j-6B")
        self.processed_input_events = get_events(recording, process=True)
        self.input_event_idx = -1
        self.ocr = PaddleOCR()
        event_dicts = rows2dicts(self.processed_input_events)
        logger.info(f"event_dicts=\n{pformat(event_dicts)}")

    # Define function to convert image to text

    # Define function to generate input events
    def get_next_input_event(self, screenshot: Screenshot):
        self.input_event_idx += 1
        num_input_events = len(self.processed_input_events)
        if self.input_event_idx >= num_input_events:
            # TODO: refactor
            raise StopIteration()

        #Segment the Screenshot with SAM
        self.sam_predictor.set_image(screenshot.array)
        masks, score, logit = self.sam_predictor.predict(mask_input=screenshot.array, multimask_output=False)
        masks = (masks > 0.5).astype(np.uint8) * 255
        segmented_screenshot = Screenshot()
        buffer = io.BytesIO()
        Image.fromarray(masks).save(buffer, format="PNG")
        segmented_screenshot.png_data = buffer.getvalue()
        # Convert the segmented_screenshot to text with ocr_mixin
        text = self.get_ocr_text(segmented_screenshot)
        # get previously recorded input events
        previously_recorded_input_events = ""
        for event in self.processed_input_events[:self.input_event_idx]:
            if previously_recorded_input_events != "":
                previously_recorded_input_events += ", "
            previously_recorded_input_events += event.text

        prompt = "Please generate the next input event based on the following:\n\n" \
                 "Task goal: {}\n\n" \
                 "Previously recorded input events: {}\n\n" \
                 "Screenshot description: {}\n\n" \
                 "Please provide your input event below." \
            .format(self.recording.task_description,
                    previously_recorded_input_events, text)
        encoded_prompt = self.tokenizer.encode(prompt, return_tensors="pt")
        generated_tokens = self.model.generate(encoded_prompt, max_length=1000,do_sample=True)
        generated_text = self.tokenizer.decode(generated_tokens[0],
                                               skip_special_tokens=True)

        #convert generated_text to InputEvent object
        input_event = self.processed_input_events[self.input_event_idx]
        logger.info(
            f"{self.input_event_idx=} of {num_input_events=}: {input_event=}"
        )

        # Replay the input event
        if self.display_events:
            image = display_event(input_event)
            image.show()
        if self.replay_events:
            if self.sleep and self.prev_timestamp:
                sleep_time = input_event.timestamp - self.prev_timestamp
                logger.debug(f"{sleep_time=}")
                time.sleep(sleep_time)
            self.prev_timestamp = input_event.timestamp
            return input_event
        else:
            return None
