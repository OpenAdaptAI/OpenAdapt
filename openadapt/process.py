from pprint import pformat

from loguru import logger
import json
import fire

from openadapt import db, models, utils
from openadapt.adapters import openai


MAX_TOKENS = 1024


def describe_action(
    action: models.ActionEvent,
    max_tokens: int | None = MAX_TOKENS,
):
    screenshot = action.screenshot
    image_base64 = screenshot.base64
    images = [image_base64]
    system_prompt = utils.render_template_from_file(
        "openadapt/prompts/analysis/system.j2"
    )
    logger.info(f"{action=}")
    window_state = action.window_event.state
    for key in ("meta", "data"):
        if key in window_state:
            del window_state[key]
    prompt = utils.render_template_from_file(
        "openadapt/prompts/analysis/describe.j2",
        action=action,
        window_state=window_state,
    )
    logger.info(f"prompt=\n{prompt}")
    payload = openai.create_payload(
        prompt,
        system_prompt,
        #["<redacted>"],
        images,
        max_tokens=max_tokens,
    )
    #logger.info(f"payload=\n{pformat(payload)}")
    #import ipdb; ipdb.set_trace()
    result = openai.get_completion(payload)
    logger.info(f"result=\n{pformat(result)}")
    choices = result["choices"]
    choice = choices[0]
    message = choice["message"]
    content = message["content"]
    content_dict = parse_json_snippet(content)
    logger.info(f"content_dict=\n{pformat(content_dict)}")
    return content_dict


def main(
    recording_timestamp: float | None = None,
):
    utils.configure_logging(logger, "INFO")

    if recording_timestamp:
        recording = db.crud.get_recording(timestamp)
    else:
        recording = db.crud.get_latest_recording()

    actions = recording.processed_action_events
    for action in actions:
        action_node_dict = describe_action(action)
        import ipdb; ipdb.set_trace()


def parse_json_snippet(snippet):
    # Remove Markdown code block syntax
    json_string = (
        snippet
        .replace('```json\n', '')
        .replace('```', '')
        .strip()
    )
    # Parse the JSON string
    return json.loads(json_string)


if __name__ == "__main__":
    fire.Fire(main)
