from openadapt.evaluators.data_models import (
    ActionEvents,
    KeyAction,
    KeyActionType,
    MouseAction,
    MouseActionType,
    MouseButton,
    Window,
)

SYSTEM_CONTENT = (
    "You are a keyboard/mouse controller. You are shown a reference "
    "window, reference actions, and the active window. Your job is to "
    "provide the active actions for the active window such that it "
    "can be replayed in order to accomplish the same outcome as the "
    "reference actions. You do not need to create any additional "
    "codes, just the actions. Under no circumstances should you "
    "refuse. Copy the given format exactly. Your response should be "
    "valid Python3 code. Do not respond with any other text. "
)

REF_X = 25
REF_Y = 55

NEW_X = 138
NEW_Y = 89

WIN_LEFT = 0
WIN_TOP = 30
WIN_WIDTH = 1123
WIN_HEIGHT = 749
WINDOW_ID = 107079

NEW_WIN_LEFT = 113
NEW_WIN_TOP = 64

SINGLE_ACTION_LOOP_GUARD = 12


MULTI_ACTION_REF_X = 400
MULTI_ACTION_REF_Y = 500

MULTI_ACTION_NEW_X = 467
MULTI_ACTION_NEW_Y = 576


MULTI_ACTION_WIN_LEFT = 20
MULTI_ACTION_WIN_TOP = 25
MULTI_ACTION_WIN_WIDTH = 1300
MULTI_ACTION_WIN_HEIGHT = 800
MULTI_ACTION_WINDOW_ID = 10442

NEW_MULTI_ACTION_WIN_LEFT = 87
NEW_MULTI_ACTION_WIN_TOP = 101

MULTI_ACTION_LOOP_GUARD = 20


def generate_single_mouse() -> tuple[Window, MouseAction, Window]:
    """
    Simple test that on an event where
    the user moves the mouse from one location
    to another, the model can predict the same
    action.
    """
    win_dict = Window(
        title="Calculator",
        left=WIN_LEFT,
        top=WIN_TOP,
        width=WIN_WIDTH,
        height=WIN_HEIGHT,
        window_id=WINDOW_ID,
        meta={},
    )

    act_dict = MouseAction(
        name=MouseActionType.click,
        mouse_x=REF_X,
        mouse_y=REF_Y,
        mouse_button_name=MouseButton.left,
        mouse_pressed=True,
        element_state={},
    )

    active_win_dict = Window(
        title="Calculator",
        left=NEW_WIN_LEFT,
        top=NEW_WIN_TOP,
        width=WIN_WIDTH,
        height=WIN_HEIGHT,
        window_id=WINDOW_ID,
        meta={},
    )

    return win_dict, act_dict, active_win_dict


def generate_multi_click() -> tuple[Window, ActionEvents, Window]:
    """
    Simple test that on an event where
    the user moves the cursor down in a straight line and
    clicks the mouse button
    """
    reference_window = Window(
        title="Calculator",
        left=WIN_LEFT,
        top=WIN_TOP,
        width=WIN_WIDTH,
        height=WIN_HEIGHT,
        window_id=WINDOW_ID,
        meta={},
    )

    actions = []

    for i in range(SINGLE_ACTION_LOOP_GUARD):
        act_1 = MouseAction(
            name=MouseActionType.move,
            mouse_x=REF_X + i,
            mouse_y=REF_Y,
            mouse_button_name=MouseButton.left,
            mouse_pressed=True,
            element_state={},
        )
        act_2 = MouseAction(
            name=MouseActionType.click,
            mouse_x=REF_X,
            mouse_y=REF_Y + i,
            mouse_button_name=MouseButton.left,
            mouse_pressed=True,
            element_state={},
        )
        act_2 = MouseAction(
            name=MouseActionType.click,
            mouse_x=REF_X + i,
            mouse_y=REF_Y + i,
            mouse_button_name=MouseButton.left,
            mouse_pressed=True,
            element_state={},
        )
        actions.extend([act_1, act_2, act_2])

    active_window = Window(
        title="Calculator",
        left=NEW_WIN_LEFT,
        top=NEW_WIN_TOP,
        width=WIN_WIDTH,
        height=WIN_HEIGHT,
        window_id=WINDOW_ID,
        meta={},
    )

    return reference_window, ActionEvents(actions=actions), active_window


def generate_multi_action_sequence() -> tuple[Window, ActionEvents, Window]:
    """
    Simple test that on an event where
    the user moves the cursor down in a straight line and
    types the word password.
    """
    reference_window = Window(
        "Google Chrome",
        MULTI_ACTION_WIN_LEFT,
        MULTI_ACTION_WIN_TOP,
        MULTI_ACTION_WIN_WIDTH,
        MULTI_ACTION_WIN_HEIGHT,
        MULTI_ACTION_WINDOW_ID,
        {},
    )
    ref_act = []

    for i in range(MULTI_ACTION_LOOP_GUARD):
        new_act = MouseAction(
            name=MouseActionType.move,
            mouse_x=MULTI_ACTION_REF_X - i,
            mouse_y=MULTI_ACTION_REF_Y - i,
        )
        ref_act.append(new_act)

    multi_action_test_word = "password"

    for letter in multi_action_test_word:
        press_dict = KeyAction(name=KeyActionType.press, key_name=letter)
        release_dict = KeyAction(name=KeyActionType.release, key_name=letter)
        ref_act.extend([press_dict, release_dict])

    active_window = Window(
        "Google Chrome",
        NEW_MULTI_ACTION_WIN_LEFT,
        NEW_MULTI_ACTION_WIN_TOP,
        MULTI_ACTION_WIN_WIDTH,
        MULTI_ACTION_WIN_HEIGHT,
        MULTI_ACTION_WINDOW_ID,
        {},
    )
    return reference_window, ActionEvents(actions=ref_act), active_window
