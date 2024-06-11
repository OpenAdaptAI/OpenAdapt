from pprint import pformat
import os
import sys
import time

from loguru import logger
from PIL import Image

from openadapt import cache, config, models, plotting, utils, window
from openadapt.adapters import openai


@cache.cache(force_refresh=False)
def get_window_image(window_search_str: str):
    logger.info(f"Waiting for window with title containing {window_search_str=}...")
    while True:
        window_event = models.WindowEvent.get_active_window_event()
        window_title = window_event.title
        if window_search_str.lower() in window_title.lower():
            logger.info(f"found {window_title=}")
            break
        time.sleep(0.1)

    screenshot = models.Screenshot.take_screenshot()
    image = screenshot.crop_active_window(window_event=window_event)
    return window_event, image


def main(window_search_str: str | None):
    if window_search_str:
        window_event, image = get_window_image(window_search_str)
        window_dict = window_event.to_prompt_dict()
        window_dict = utils.normalize_positions(window_dict, -window_event.left, -window_event.top)
    else:
        image_file_path = os.path.join(config.ROOT_DIR_PATH, "../tests/assets/calculator.png")
        #image_file_path = os.path.join(config.ROOT_DIR_PATH, "../tests/assets/excel.png")
        image = Image.open(image_file_path)
        window_dict = None

    system_prompt = utils.render_template_from_file(
        "prompts/system.j2",
    )

    if window_dict:
        window_prompt = (
            f"Consider the corresponding window state:\n```{pformat(window_dict)}```"
        )
    else:
        window_prompt = ""

    prompt = f"""You are a master GUI understander.
Your task is to locate all interactable elements in the supplied screenshot.
{window_prompt}
Return JSON containing an array of segments with the following properties:
- "name": a unique identifier
- "description": enough context to be able to differentiate between similar segments
- "top": top coordinate of bounding box
- "left": left coordinate of bounding box
- "width": width of bouding box
- "height": height of bounding box
Provide as much detail as possible. My career depends on this. Lives are at stake.
Respond with JSON ONLY AND NOTHING ELSE.
"""

    result = openai.prompt(
        prompt,
        system_prompt,
        [image],
    )
    segment_dict = utils.parse_code_snippet(result)
    plotting.plot_segments(image, segment_dict)

    window_dict = window_event.to_prompt_dict()
    import ipdb; ipdb.set_trace()


if __name__ == "__main__":
    main(sys.argv[1])
