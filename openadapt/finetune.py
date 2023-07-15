from openadapt import crud, models
from loguru import logger
import fire

VALID_MODELS = ("davinci", "mpt-7b")


class EventType:
    WINDOW = "WINDOW"
    ACTION = "ACTION"


def main(recording_id: int, model_name: str):
    finetune_recording = crud.get_recording_by_id(recording_id)

    assert finetune_recording, "A recording with the given id does not exist"
    assert (
        model_name.lower() not in VALID_MODELS
    ), "This model is not supported for finetuning yet"

    logger.debug(f"{finetune_recording=}")


def davinci_learn_recording_summary_pipeline(
    summary: str, finetune_recording: models.Recording
):
    # prompt: the recording window states and recordings distilled
    # completion: summary of the recording
    summary_window_states = crud.get_window_events(finetune_recording)
    summary_ref_action_events = crud.get_action_events(finetune_recording)

    prompt = (f"{summary_window_states}\n", f"{summary_ref_action_events}")
    completion = summary

    logger.debug(f"{prompt=}")
    logger.debug(f"{completion=}")

    sanitized_window_states = event_sanitizer(summary_window_states, "window")
    sanitized_ref_action_events = event_sanitizer(summary_ref_action_events, "action")

    # write to a json file in the format
    # {prompt: finetune_prompt, completion: incomplete_recording}
    # where finetune_prompt is the same as in get_next_action_event in stateful

    # TODO


def davinci_get_next_action_event_pipeline():
    # given the summary of an incomplete recording, along
    # with the window states and action events,
    # ask the model to generate "next event" completions

    # For now, this method must only be called after guaranteeing that we have 
    # a certain number of recordings stored in the json file (i.e we have finetuned
    # on enough examples.)
    # TODO
    pass


def event_sanitizer(event, event_type: EventType):
    assert (
        event_type.upper() == EventType.WINDOW or event_type.upper() == EventType.ACTION
    ), "Not a valid event type"


if __name__ == "__main__":
    fire.Fire(main)
