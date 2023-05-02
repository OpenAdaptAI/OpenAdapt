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

CHECKPOINT_URL_BASE = "https://dl.fbaipublicfiles.com/segment_anything/"
CHECKPOINT_URL_BY_NAME = {
    "default": f"{CHECKPOINT_URL_BASE}sam_vit_h_4b8939.pth",
    "vit_l": f"{CHECKPOINT_URL_BASE}sam_vit_l_0b3195.pth",
    "vit_b": f"{CHECKPOINT_URL_BASE}sam_vit_b_01ec64.pth",
}
MODEL_NAME = "default"
CHECKPOINT_DIR_PATH = "./checkpoints"


class SamReplayStrategy(OCRReplayStrategyMixin):
    def __init__(
            self,
            recording: Recording,
            display_events=DISPLAY_EVENTS,
            replay_events=REPLAY_EVENTS,
            sleep=SLEEP,
            model_name=MODEL_NAME,
            checkpoint_dir_path=CHECKPOINT_DIR_PATH,
    ):
        super().__init__(recording)
        self.processed_input_events = get_events(recording, process=True)
        self.display_events = display_events
        self.replay_events = replay_events
        self.sleep = sleep
        self.prev_timestamp = None
        self.sam_model = self._initialize_model(model_name, checkpoint_dir_path)
        self.sam_predictor = SamPredictor(self.sam_model)
        self.tokenizer = GPT2Tokenizer.from_pretrained("EleutherAI/gpt-j-6B")
        self.model = GPTJForCausalLM.from_pretrained("EleutherAI/gpt-j-6B")
        self.processed_input_events = get_events(recording, process=True)
        self.input_event_idx = -1
        self.ocr = PaddleOCR()
        event_dicts = rows2dicts(self.processed_input_events)
        logger.info(f"event_dicts=\n{pformat(event_dicts)}")

    def _initialize_model(self, model_name, checkpoint_dir_path):
        checkpoint_url = CHECKPOINT_URL_BY_NAME[model_name]
        checkpoint_file_name = checkpoint_url.split("/")[-1]
        checkpoint_file_path = Path(checkpoint_dir_path, checkpoint_file_name)
        if not Path.exists(checkpoint_file_path):
            Path(checkpoint_dir_path).mkdir(parents=True, exist_ok=True)
            logger.info(
                f"downloading {checkpoint_url=} to {checkpoint_file_path=}")
            urllib.request.urlretrieve(checkpoint_url, checkpoint_file_path)
        return sam_model_registry[model_name](checkpoint=checkpoint_file_path)

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
            if event.name is not None:
                previously_recorded_input_events += f"Event name is ({event.name})"
            if event.timestamp is not None:
                previously_recorded_input_events += f"Timestamp: {event.timestamp}"
            if event.recording_timestamp is not None:
                previously_recorded_input_events += f"Recording timestamp: {event.recording_timestamp}"
            if event.screenshot_timestamp is not None:
                previously_recorded_input_events += f"Screenshot timestamp: {event.screenshot_timestamp}"
            if event.window_event_timestamp is not None:
                previously_recorded_input_events += f"Window event timestamp: {event.window_event_timestamp}"
            if event.mouse_x is not None and event.mouse_y is not None:
                previously_recorded_input_events += f"Mouse click at ({event.mouse_x}, {event.mouse_y})"
            if event.mouse_dx is not None and event.mouse_dy is not None:
                previously_recorded_input_events += f"Mouse movement: ({event.mouse_dx}, {event.mouse_dy})"
            if event.mouse_button_name is not None:
                previously_recorded_input_events += f"Mouse button name: {event.mouse_button_name}"
            if event.mouse_pressed is not None:
                previously_recorded_input_events += f"Mouse pressed: {event.mouse_pressed}"
            if event.key_name is not None:
                previously_recorded_input_events += f"Key name: {event.key_name}"
            if event.key_char is not None:
                previously_recorded_input_events += f"Key character: {event.key_char}"
            if event.key_vk is not None:
                previously_recorded_input_events += f"Key virtual code: {event.key_vk}"
            if event.canonical_key_name is not None:
                previously_recorded_input_events += f"Canonical key name: {event.canonical_key_name}"
            if event.canonical_key_char is not None:
                previously_recorded_input_events += f"Canonical key character: {event.canonical_key_char}"
            if event.canonical_key_vk is not None:
                previously_recorded_input_events += f"Canonical key virtual code: {event.canonical_key_vk}"

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
