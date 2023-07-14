from datetime import datetime
from dateutil import parser
from typing import Union

from loguru import logger
import fire

from openadapt import crud, utils


LOG_LEVEL = "INFO"


def replay(
    strategy_name: str,
    timestamp: Union[str, None] = None,
):
    utils.configure_logging(logger, LOG_LEVEL)

    if timestamp:
        recording = crud.get_recording(timestamp)
    else:
        recording = crud.get_latest_recording()
    logger.debug(f"{recording=}")
    assert recording, "No recording found"

    file_signals = crud.get_file_signals(recording)
    for file_signal in file_signals:
        print(file_signal)

    logger.info(f"{strategy_name=}")

    strategy_class_by_name = utils.get_strategy_class_by_name()
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

    # If signals were found in the recording, initialize them as defaults during the replay

    strategy.run()


if __name__ == "__main__":
    fire.Fire(replay)
