"""Automated process documentation."""

from loguru import logger
import cohere

from openadapt.config import config
from openadapt.db import crud
from openadapt.strategies.visual import (
    add_active_segment_descriptions,
    get_action_prompt_dict,
    get_window_prompt_dict,
)
from openadapt import adapters, cache, models, utils


API_KEY = config.COHERE_API_KEY


@cache.cache()
def get_action_description(action: models.ActionEvent):
    system_prompt = utils.render_template_from_file(
        "openadapt/procdoc/system.j2",
    )
    action_dict = get_action_prompt_dict(action)
    try:
        del action_dict["available_segment_descriptions"]
    except:
        pass
    window_dict = get_window_prompt_dict(action.window_event)
    action_description_prompt = utils.render_template_from_file(
        "openadapt/procdoc/describe_action.j2",
        action=action_dict,
        window=window_dict,
    )
    logger.info(f"action_description_prompt=\n{action_description_prompt}")
    if 0:
        prompt_adapter = adapters.get_default_prompt_adapter()
    else:
        prompt_adapter = adapters.cohere
    action_description = prompt_adapter.prompt(
        action_description_prompt,
        system_prompt,
    )
    logger.info(f"action_description=\n{action_description}")
    return action_description


@cache.cache()
def get_process_description(recording: models.Recording):
    add_active_segment_descriptions(
        recording.processed_action_events,
    )
    actions = recording.processed_action_events
    action_descriptions = [
        get_action_description(action)
        for action in actions
    ]
    process_description_prompt = utils.render_template_from_file(
        "openadapt/procdoc/describe_process.j2",
        action_descriptions=action_descriptions,
    )
    process_description = prompt_adapter.prompt(
        process_description_prompt,
        system_prompt,
    )
    logger.info(f"process_description=\n{process_description}")
    return process_description



def main():
    recordings = crud.get_all_recordings()
    process_descriptions = [
        get_process_description(recording)
        for recording in recordings
    ]
    #vanilla_rag(process_descriptions)

    import ipdb; ipdb.set_trace()


if __name__ == "__main__":
    main()
