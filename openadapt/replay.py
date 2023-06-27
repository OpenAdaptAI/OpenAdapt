"""Replay recorded events.

Usage:
python openadapt/replay.py <strategy_name> [--timestamp=<timestamp>]

Arguments:
strategy_name Name of the replay strategy to use.

Options:
--timestamp=<timestamp> Timestamp of the recording to replay.

"""

from typing import Union

from loguru import logger
import fire

from openadapt import crud, utils

LOG_LEVEL = "INFO"


def replay(strategy_name: str, timestamp: Union[str, None] = None):
    """
    Replay recorded events using the specified strategy.

    Args:
        strategy_name: Name of the replay strategy to use.
        timestamp: Timestamp of the recording to replay.
    """
    utils.configure_logging(logger, LOG_LEVEL)

    if timestamp:
        recording = crud.get_recording(timestamp)
    else:
        recording = crud.get_latest_recording()
    logger.debug(f"{recording=}")
    assert recording, "No recording found"

    logger.info(f"{strategy_name=}")

    strategy_class_by_name = utils.get_strategy_class_by_name()
    if strategy_name not in strategy_class_by_name:
        strategy_names = [
            name
            for name in strategy_class_by_name.keys()
            if not name.lower().endswith("mixin")
        ]
        available_strategies = ", ".join(strategy_names)
        raise ValueError(f"Invalid {strategy_name=}; {available_strategies=}")

    strategy_class = strategy_class_by_name[strategy_name]
    logger.info(f"{strategy_class=}")

    strategy = strategy_class(recording)
    logger.info(f"{strategy=}")

    strategy.run()


# Entry point
def start():
    """Starts the replay."""
    fire.Fire(replay)


if __name__ == "__main__":
    fire.Fire(replay)
