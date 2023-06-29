from typing import List
import openai

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


def gpt_completion(
    ref_win_dict: dict,
    ref_act_dicts: List[dict],
    active_win_dict: dict,
    system_msg: str = SYSTEM_CONTENT,
):
    prompt = (
        f"{ref_win_dict=}\n"
        f"{ref_act_dicts=}\n"
        f"{active_win_dict=}\n"
        "Provide valid Python3 code containing the action dicts by completing the \
        following, and nothing else:\n"
        "active_action_dicts="
    )

    completion = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[
            {
                "role": "system",
                "content": system_msg,
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
    )
    return completion["choices"][0]["message"]["content"]


def test_active_window_no_diff():
    """
    A test using the latest user recording where the
    reference window events are the same as the
    active window events. The model must thus return the same
    action events as the REFERENCE action events.

    PRECONDITION: Recording cannot be too long due to max input
    size limitations of the LLM (in this case, GPT-3.5-Turbo/GPT-4)
    """

    # STILL NOT GENERALIZABLE TO OTHER SIMPLER TESTS, WIP

    reference_window_dict = {
        "state": {
            "title": "Terminal openadapt — poetry shell ▸ Python — 198×55",
            "left": 36,
            "top": 39,
            "width": 1396,
            "height": 805,
            "window_id": 6247,
            "meta": {
                "kCGWindowLayer": 0,
                "kCGWindowAlpha": 1,
                "kCGWindowMemoryUsage": 1264,
                "kCGWindowIsOnscreen": True,
                "kCGWindowSharingState": 1,
                "kCGWindowOwnerPID": 591,
                "kCGWindowNumber": 6247,
                "kCGWindowOwnerName": "Terminal",
                "kCGWindowStoreType": 1,
                "kCGWindowBounds": {"X": 36, "Height": 805, "Y": 39, "Width": 1396},
                "kCGWindowName": "openadapt — poetry shell ▸ Python — 198×55",
            },
        },
        "title": "Terminal openadapt — poetry shell ▸ Python — 198×55",
        "left": 36,
        "top": 39,
        "width": 1396,
        "height": 805,
    }

    reference_action_dicts = [
        {
            "name": "click",
            "mouse_x": 406.3359375,
            "mouse_y": 52.16796875,
            "mouse_button_name": "left",
            "mouse_pressed": True,
            "element_state": {},
        },
        {
            "name": "click",
            "mouse_x": 406.3359375,
            "mouse_y": 30.5078125,
            "mouse_button_name": "left",
            "mouse_pressed": False,
            "element_state": {},
        },
    ]

    active_window_dict = {
        "state": {
            "title": "Terminal openadapt — poetry shell ▸ Python — 198×55",
            "left": 36,
            "top": 39,
            "width": 1396,
            "height": 805,
            "window_id": 6247,
            "meta": {
                "kCGWindowLayer": 0,
                "kCGWindowAlpha": 1,
                "kCGWindowMemoryUsage": 1264,
                "kCGWindowIsOnscreen": True,
                "kCGWindowSharingState": 1,
                "kCGWindowOwnerPID": 591,
                "kCGWindowNumber": 6247,
                "kCGWindowOwnerName": "Terminal",
                "kCGWindowStoreType": 1,
                "kCGWindowBounds": {"X": 36, "Height": 805, "Y": 39, "Width": 1396},
                "kCGWindowName": "openadapt — poetry shell ▸ Python — 198×55",
            },
        },
        "title": "Terminal openadapt — poetry shell ▸ Python — 198×55",
        "left": 36,
        "top": 39,
        "width": 1396,
        "height": 805,
    }

    test_action_dict = gpt_completion(
        reference_window_dict, reference_action_dicts, active_window_dict
    )

    test_dict = eval(
        test_action_dict[test_action_dict.find("[") : test_action_dict.find("]") + 1]
    )
    expected_action_dict = reference_action_dicts

    assert test_dict == expected_action_dict


if __name__ == "__main__":
    test_active_window_no_diff()
