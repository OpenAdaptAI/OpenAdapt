from pprint import pformat
import time

from loguru import logger
from pynput import keyboard, mouse

from puterbot.common import KEY_EVENTS, MOUSE_EVENTS
from puterbot.crud import (
    get_latest_recording,
)
from puterbot.events import (
    get_events,
)
from puterbot.utils import (
    configure_logging,
    display_event,
    rows2dicts,
)


DISPLAY_EVENTS = False
LOG_LEVEL = "INFO"
PROCESS_EVENTS = True
REPLAY_EVENTS = True
SLEEP = False


def replay_mouse_event(event, mouse_controller):
    name = event.name
    assert name in MOUSE_EVENTS, event
    x = event.mouse_x
    y = event.mouse_y
    dx = event.mouse_dx
    dy = event.mouse_dy
    button_name = event.mouse_button_name
    pressed = event.mouse_pressed
    logger.debug(f"{name=} {x=} {y=} {dx=} {dy=} {button_name=} {pressed=}")

    mouse_controller.position = (x, y)
    if name == "move":
        pass
    elif name == "click":
        button = mouse.Button[button_name]
        if pressed:
            mouse_controller.press(button)
        else:
            mouse_controller.release(button)
    elif name == "singleclick":
        button = mouse.Button[button_name]
        mouse_controller.click(button, 1)
    elif name == "doubleclick":
        button = mouse.Button[button_name]
        mouse_controller.click(button, 2)
    elif event.name == "scroll":
        mouse_controller.scroll(dx, dy)
    else:
        raise Exception(f"unhandled {event.name=}")


def replay_key_event(event, keyboard_controller, canonical=True):
    assert event.name in KEY_EVENTS, event

    key = event.canonical_key if canonical else event.key

    if event.name == "press":
        keyboard_controller.press(key)
    elif event.name == "release":
        keyboard_controller.release(key)
    elif event.name == "type":
        keyboard_controller.type(key)
    else:
        raise Exception(f"unhandled {event.name=}")


def replay_input_event(event, mouse_controller, keyboard_controller):
    if event.children:
        for child in event.children:
            replay_input_event(child, mouse_controller, keyboard_controller)
    else:
        assert event.name in MOUSE_EVENTS + KEY_EVENTS, event
        if event.name in MOUSE_EVENTS:
            replay_mouse_event(event, mouse_controller)
        elif event.name in KEY_EVENTS:
            replay_key_event(event, keyboard_controller)
        else:
            raise Exception(f"unhandled {event.name=}")


def click_event_handler(
    input_event,
    mouse_controller,
    keyboard_controller,
):
    logger.debug(f"{input_event=}")
    replay_input_event(input_event, mouse_controller, keyboard_controller)


def default_event_handler(
    input_event,
    mouse_controller,
    keyboard_controller,
):
    logger.debug(f"{input_event=}")
    replay_input_event(input_event, mouse_controller, keyboard_controller)


def main():
    configure_logging(logger, LOG_LEVEL)

    EVENT_NAME_TO_HANDLER = {
        "click": click_event_handler,
        "doubleclick": click_event_handler,
        # TODO: handle other events
    }

    keyboard_controller = keyboard.Controller()
    mouse_controller = mouse.Controller()

    recording = get_latest_recording()
    logger.debug(f"{recording=}")

    input_events = get_events(recording, process=PROCESS_EVENTS)
    event_dicts = rows2dicts(input_events)
    logger.info(f"event_dicts=\n{pformat(event_dicts)}")

    prev_timestamp = None
    for input_event in input_events:
        if DISPLAY_EVENTS:
            image = display_event(input_event)
            image.show()
        if REPLAY_EVENTS:
            if SLEEP and prev_timestamp:
                sleep_time = input_event.timestamp - prev_timestamp
                logger.debug(f"{sleep_time=}")
                time.sleep(sleep_time)
            # TODO: account for replay time?
            event_handler = EVENT_NAME_TO_HANDLER.get(
                input_event.name, default_event_handler,
            )
            event_handler(
                input_event,
                mouse_controller,
                keyboard_controller,
            )
            prev_timestamp = input_event.timestamp


if __name__ == "__main__":
    main()
