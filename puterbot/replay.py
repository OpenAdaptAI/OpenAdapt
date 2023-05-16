from datetime import datetime
from dateutil import parser
from typing import Union

from loguru import logger
import fire

from puterbot import crud, utils
from puterbot.crud import get_latest_recording
from puterbot.utils import configure_logging, get_strategy_class_by_name


LOG_LEVEL = "INFO"


def replay(
    strategy_name: str,
    timestamp: Union[str, None] = None,
):
    configure_logging(logger, LOG_LEVEL)

    if timestamp:
        try:
            timestamp = parser.parse(timestamp)
        except:
            timestamp = eval(timestamp)
        logger.info(f"{timestamp=} {type(timestamp)=}")
        recording = crud.get_recording(timestamp)
    else:
        recording = get_latest_recording()
    logger.debug(f"{recording=}")
    assert recording, "No recording found"

    logger.info(f"{strategy_name=}")

    strategy_class_by_name = get_strategy_class_by_name()
    if strategy_name not in strategy_class_by_name:
        strategy_names = [
            name
            for name in strategy_class_by_name.keys()
            if not name.lower().endswith("mixin")
        ]
        available_strategies = ", ".join(strategy_names)
        raise ValueError(
            f"Invalid {strategy_name=}; {available_strategies=}"
        )

    strategy_class = strategy_class_by_name[strategy_name]
    logger.info(f"{strategy_class=}")

    strategy = strategy_class(recording)
    logger.info(f"{strategy=}")

    strategy.run()


if __name__ == "__main__":
    fire.Fire(replay)
