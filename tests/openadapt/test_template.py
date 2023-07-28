from griptape.utils.j2 import J2
import os
def test_stateful_template() :
    reference_window_dict={'state': {'title': 'Code', 'left': 0, 'top': 42, 'width': 1710, 'height': 44, 'window_id': 200, 'meta': {'kCGWindowLayer': 0, 'kCGWindowAlpha': 0, 'kCGWindowMemoryUsage': 1264, 'kCGWindowIsOnscreen': True, 'kCGWindowSharingState': 1, 'kCGWindowOwnerPID': 613, 'kCGWindowNumber': 200, 'kCGWindowOwnerName': 'Code', 'kCGWindowStoreType': 1, 'kCGWindowBounds': {'X': 0, 'Height': 44, 'Y': 42, 'Width': 1710}, 'kCGWindowName': ''}}, 'title': 'Code', 'left': 0, 'top': 42, 'width': 1710, 'height': 44}
    reference_action_dicts=[{'name': 'click', 'mouse_x': 609.09765625, 'mouse_y': 844.87109375, 'mouse_button_name': 'left', 'mouse_pressed': True, 'element_state': {}}, {'name': 'click', 'mouse_x': 609.09765625, 'mouse_y': 844.87109375, 'mouse_button_name': 'left', 'mouse_pressed': False, 'element_state': {}}]
    active_window_dict={'state': {'title': 'Code', 'left': 0, 'top': 42, 'width': 1710, 'height': 44, 'window_id': 6559, 'meta': {'kCGWindowLayer': 0, 'kCGWindowAlpha': 0, 'kCGWindowMemoryUsage': 1264, 'kCGWindowIsOnscreen': True, 'kCGWindowSharingState': 1, 'kCGWindowOwnerPID': 613, 'kCGWindowNumber': 6559, 'kCGWindowOwnerName': 'Code', 'kCGWindowStoreType': 1, 'kCGWindowBounds': {'X': 0, 'Height': 44, 'Y': 42, 'Width': 1710}, 'kCGWindowName': ''}}, 'title': 'Code', 'left': 0, 'top': 42, 'width': 1710, 'height': 44}
    prompt = (
            f"{reference_window_dict=}\n"
            f"{reference_action_dicts=}\n"
            f"{active_window_dict=}\n"
            "Provide valid Python3 code containing the action dicts"
            " by completing the following,"
            " and nothing else:\n"
            "active_action_dicts="
        )
    prompt_template = J2(template_name="stateful.j2",templates_dir=os.path.abspath("../../openadapt/templates"))
    j2_prompt = prompt_template.render(
            reference_window_dict=reference_window_dict,
            reference_action_dicts=reference_action_dicts,
            active_window_dict=active_window_dict,)

    assert prompt == j2_prompt