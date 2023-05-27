"""Utilities for playing back ActionEvents"""

from loguru import logger
from pynput import mouse

from openadapt.common import KEY_EVENTS, MOUSE_EVENTS


def play_mouse_event(event, mouse_controller):
    """

    :param event: 
    :param mouse_controller: 

    """
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


def play_key_event(event, keyboard_controller, canonical=True):
    """

    :param event: 
    :param keyboard_controller: 
    :param canonical:  (Default value = True)

    """
    assert event.name in KEY_EVENTS, event

    key = (
        event.canonical_key if canonical and event.canonical_key else
        event.key
    )

    if event.name == "press":
        keyboard_controller.press(key)
    elif event.name == "release":
        keyboard_controller.release(key)
    elif event.name == "type":
        keyboard_controller.type(key)
    else:
        raise Exception(f"unhandled {event.name=}")


def play_action_event(event, mouse_controller, keyboard_controller):
    """

    :param event: 
    :param mouse_controller: 
    :param keyboard_controller: 

    """
    # currently we use children to replay type events
    if event.children and event.name in KEY_EVENTS:
        for child in event.children:
            play_action_event(child, mouse_controller, keyboard_controller)
    else:
        assert event.name in MOUSE_EVENTS + KEY_EVENTS, event
        if event.name in MOUSE_EVENTS:
            play_mouse_event(event, mouse_controller)
        elif event.name in KEY_EVENTS:
            play_key_event(event, keyboard_controller)
        else:
            raise Exception(f"unhandled {event.name=}")
