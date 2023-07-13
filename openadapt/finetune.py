from openadapt import models
from openadapt.crud import get_recording_by_id
from loguru import logger
import fire

VALID_MODELS = ("davinci", "mpt-7b")


def main(recording_id: int, model_name: str):
    finetune_recording = get_recording_by_id(recording_id)
    if not finetune_recording:
        raise ValueError("A recording with the given id does not exist")

    if model_name.lower() not in VALID_MODELS:
        raise ValueError("This model is not supported for finetuning yet")

    logger.debug(f"{finetune_recording=}")


if __name__ == "__main__":
    fire.Fire(main)
