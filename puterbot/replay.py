from pprint import pformat
import importlib
import time

from loguru import logger
from pynput import keyboard, mouse
import fire

from puterbot.crud import (
    get_latest_recording,
)
from puterbot.utils import (
    configure_logging,
    get_strategy_class_by_name,
)


LOG_LEVEL = "INFO"


def replay(
    strategy_name: str,
):
    configure_logging(logger, LOG_LEVEL)

    recording = get_latest_recording()
    logger.debug(f"{recording=}")

    logger.info(f"{strategy_name=}")

    strategy_class_by_name = get_strategy_class_by_name()
    if strategy_name not in strategy_class_by_name:
        available_strategy_names = ", ".join(strategy_class_by_name.keys())
        raise ValueError(
            f"Invalid {strategy_name=}; {available_strategy_names=}"
        )

    strategy_class = strategy_class_by_name[strategy_name]
    logger.info(f"{strategy_class=}")

    strategy = strategy_class(recording)
    logger.info(f"{strategy=}")

    strategy.run()


if __name__ == "__main__":
    fire.Fire(replay)
