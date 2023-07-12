from openadapt import models
from openadapt.strategies.mixins.mpt7b import MPT7BReplayStrategy
from loguru import logger

TEST_X_VALS = [400, 399, 398, 397]
TEST_Y_VALS = [500, 499, 498, 497]
SMOKE_TEST_STR = "This sentence is "


class TestMPT7B(MPT7BReplayStrategy):
    def get_next_action_event(
        self, screenshot: models.Screenshot
    ) -> models.ActionEvent:
        pass


def mpt7b_smoke_test():
    mpt7b_test_instance = TestMPT7B(models.Recording())

    smoke_test_completion_response = mpt7b_test_instance.get_completion(SMOKE_TEST_STR)

    assert len(smoke_test_completion_response) > 0


def mpt7b_test_action_event():
    mpt7b_test_instance = TestMPT7B(models.Recording())

    test_action_events = [
        {
            "name": "move",
            "mouse_x": TEST_X_VALS[0],
            "mouse_y": TEST_Y_VALS[0],
            "element_state": {},
        },
        {
            "name": "move",
            "mouse_x": TEST_X_VALS[1],
            "mouse_y": TEST_Y_VALS[1],
            "element_state": {},
        },
        {
            "name": "move",
            "mouse_x": TEST_X_VALS[2],
            "mouse_y": TEST_Y_VALS[2],
            "element_state": {},
        },
        {
            "name": "move",
            "mouse_x": TEST_X_VALS[3],
            "mouse_y": TEST_Y_VALS[3],
            "element_state": {},
        },
    ]
    test_action_str = f"I will respond only using Python objects. The next action event in the given list: {test_action_events} is "
    action_completion = mpt7b_test_instance.get_completion(test_action_str)

    logger.debug(f"{action_completion=}")

    assert len(action_completion) > 0


if __name__ == "__main__":
    mpt7b_smoke_test()
    mpt7b_test_action_event()
