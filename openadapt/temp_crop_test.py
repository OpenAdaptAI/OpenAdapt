import fire
from openadapt.crud import get_latest_recording, get_window_events
from openadapt.utils import configure_logging, get_strategy_class_by_name
from openadapt.models import Screenshot


def crop_test():
    recording = get_latest_recording()
    action_event = recording.action_events[0]
    screenshot = Screenshot.take_screenshot()
    screenshot.image.show()
    screenshot.crop_active_window(action_event)
    screenshot.image.show()


if __name__ == "__main__":
    fire.Fire(crop_test)
