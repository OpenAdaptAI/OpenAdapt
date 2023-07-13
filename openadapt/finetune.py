from openadapt import crud, models
from loguru import logger
import fire

VALID_MODELS = ("davinci", "mpt-7b")


def main(recording_id: int, model_name: str):
    finetune_recording = crud.get_recording_by_id(recording_id)
    if not finetune_recording:
        raise ValueError("A recording with the given id does not exist")

    if model_name.lower() not in VALID_MODELS:
        raise ValueError("This model is not supported for finetuning yet")

    logger.debug(f"{finetune_recording=}")


def davinci_learn_recording_summary_pipeline(
    summary: str, finetune_recording: models.Recording
):
    # prompt: the recording window states and recordings distilled
    # completion: summary of the recording
    summary_window_states = crud.get_window_events(finetune_recording)
    summary_ref_action_events = crud.get_action_events(finetune_recording)

    prompt = (f"{summary_window_states}\n",f"{summary_ref_action_events}")
    completion = summary

    logger.debug(f"{prompt=}")
    logger.debug(f"{completion=}")

    # TODO


def davinci_get_next_action_event_pipeline():
    # given the summary of an incomplete recording, along 
    # with the window states and action events, 
    # ask the model to generate "next event" completions
    
    # TODO


if __name__ == "__main__":
    fire.Fire(main)
