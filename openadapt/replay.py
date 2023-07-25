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
from openadapt.models import Recording

LOG_LEVEL = "INFO"


@logger.catch
def replay(
    strategy_name: str,
    timestamp: Union[str, None] = None,
    recording: Recording = None,
) -> None:
    utils.configure_logging(logger, LOG_LEVEL)

    if timestamp and recording is None:
        recording = crud.get_recording(timestamp)
    elif recording is None:
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
def start() -> None:
    """Starts the replay."""
    fire.Fire(replay)


if __name__ == "__main__":
    fire.Fire(replay)
