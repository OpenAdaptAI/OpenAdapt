import json
import fire

from openadapt.db import crud
from openadapt.config import print_config, LOG_LEVEL
from openadapt.custom_logger import logger
from openadapt import utils, adapters

def generate_recording_documentation(recording_timestamp: str) -> None:
    """
    Generates human-readable documentation for a given recording using an LLM.

    Args:
        recording_timestamp: The timestamp of the recording to document.
    """
    utils.configure_logging(logger, LOG_LEVEL)
    print_config() # Consider if this is needed or too verbose for a script output

    logger.info(f"Generating documentation for recording: {recording_timestamp}")

    session = crud.get_new_session(read_only=True)
    recording = crud.get_recording(session, recording_timestamp)

    if not recording:
        logger.error(f"No recording found with timestamp: {recording_timestamp}")
        return

    logger.info(f"Loaded recording ID: {recording.id}, Task: {recording.task_description}")

    action_window_data_list = []
    screenshot_images = []

    # Ensure processed_action_events are loaded, which also preloads screenshots
    processed_actions = recording.processed_action_events
    logger.info(f"Processing {len(processed_actions)} action events.")

    for action_event in processed_actions:
        action_dict = action_event.to_prompt_dict()
        window_dict = {}
        if action_event.window_event:
            # include_data=False to keep it concise for this prompt, can be tuned
            window_dict = action_event.window_event.to_prompt_dict(include_data=False)

        action_window_data_list.append({
            "action": action_dict,
            "window_state_before_action": window_dict,
            "action_timestamp": action_event.timestamp # For reference
        })

        if action_event.screenshot and hasattr(action_event.screenshot, 'image'):
            # Ensure image is PIL Image, not None
            if action_event.screenshot.image:
                screenshot_images.append(action_event.screenshot.image)
            else:
                logger.warning(f"Action event {action_event.id} has screenshot object but no image data.")
        elif action_event.screenshot:
             logger.warning(f"Action event {action_event.id} has screenshot object but .image is missing/None.")
        else:
            logger.warning(f"Action event {action_event.id} is missing screenshot attribute.")


    if not action_window_data_list:
        logger.warning("No action events with sufficient data found to generate documentation.")
        return

    if not screenshot_images:
        logger.warning("No screenshots found to accompany actions. LLM results may be poor.")
        # Decide if to proceed without images or stop. For now, proceed.

    prompt_context = {
        "action_windows": json.dumps(action_window_data_list, indent=2)
    }

    system_prompt_template = "prompts/system.j2" # Assuming a generic system prompt
    user_prompt_template = "prompts/describe_recording.j2"

    system_prompt = utils.render_template_from_file(system_prompt_template)
    user_prompt = utils.render_template_from_file(user_prompt_template, **prompt_context)

    logger.info("Sending request to LLM for documentation generation...")
    # logger.debug(f"System Prompt: {system_prompt}") # Can be verbose
    # logger.debug(f"User Prompt: {user_prompt}") # Can be very verbose

    llm_adapter = adapters.get_default_prompt_adapter()
    try:
        documentation = llm_adapter.prompt(
            user_prompt,
            system_prompt=system_prompt,
            images=screenshot_images
        )

        print("\n--- Generated Recording Documentation ---")
        print(documentation)
        print("--- End of Documentation ---")

        logger.info("Documentation generated successfully.")

    except Exception as e:
        logger.exception(f"Failed to generate documentation from LLM: {e}")

if __name__ == "__main__":
    fire.Fire(generate_recording_documentation)
