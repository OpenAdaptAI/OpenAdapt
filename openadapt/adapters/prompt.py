import sys

from loguru import logger
from typing import Any, Type
from PIL import Image


from openadapt import utils
from openadapt.drivers import anthropic, google, openai


# Define a list of drivers in the order they should be tried
DRIVER_ORDER: list[Type] = [openai, anthropic, google]


def prompt(
    text: str,
    images: list[Image.Image] | None = None,
    system_prompt: str | None = None,
) -> str:
    """Attempt to fetch a prompt completion from various services in order of priority.

    Args:
        text: The main text prompt.
        images: list of images to include in the prompt.
        system_prompt: An optional system-level prompt.

    Returns:
        The result from the first successful driver.
    """
    for driver in DRIVER_ORDER:
        try:
            logger.info(f"Trying driver: {driver.__name__}")
            return driver.prompt(text, images=images, system_prompt=system_prompt)
        except Exception as e:
            logger.error(f"Driver {driver.__name__} failed with error: {e}")
            continue
    raise Exception("All drivers failed to provide a response")


def prompt_template(
    text_template: str,
    images: list[Image.Image] | None = None,
    system_prompt: str | None = None,
    **template_params
) -> list:
    """Attempt to fetch prompt completions from various services using a template, handling image batching as needed.

    Args:
        text_template: Path or identifier for the text template.
        images: Optional list of images to include in the prompt.
        system_prompt: An optional system-level prompt.
        template_params: Additional parameters required for rendering the template.

    Returns:
        A list of raw results from the first successful driver.
    """
    if images is None:
        images = []  # Ensure images is always a list

    results = []
    for driver in DRIVER_ORDER:
        max_images = getattr(driver, 'MAX_IMAGES', sys.maxsize)
        if not isinstance(max_images, int) or max_images <= 0:
            max_images = sys.maxsize

        try:
            for i in range(0, len(images), max_images):
                batch = images[i:i+max_images]
                adjusted_params = {**template_params, "num_segments": len(batch)}
                user_prompt = utils.render_template_from_file(text_template, **adjusted_params)

                # Attempt the prompt with the current driver
                result = driver.prompt(user_prompt, images=batch, system_prompt=system_prompt)
                results.append(result)  # Store raw results
        except Exception as e:
            logger.error(f"Driver {driver.__name__} failed with error: {e}")
            continue

    if not results:
        raise Exception("All drivers failed to provide a response")

    return results


def batch(prompt_function: callable, **kwargs: dict) -> list[Any]:
    for driver in DRIVER_ORDER:
        # off by one to account for original image
        if driver.MAX_IMAGES and (
            len(masked_images) + 1 > driver.MAX_IMAGES
        ):
            masked_images_batches = utils.split_list(
                masked_images,
                driver.MAX_IMAGES - 1,
            )
            descriptions = []
            for masked_images_batch in masked_images_batches:
                descriptions_batch = prompt_for_descriptions(
                    original_image,
                    masked_images_batch,
                    active_segment_description,
                    exceptions,
                )
                descriptions += descriptions_batch
            return descriptions



        try:
            logger.info(f"Trying driver: {driver.__name__}")
            return driver.prompt(text, images=images, system_prompt=system_prompt)
        except Exception as e:
            logger.error(f"Driver {driver.__name__} failed with error: {e}")
            continue
    raise Exception("All drivers failed to provide a response")

###



if __name__ == "__main__":
    # This could be extended to use command-line arguments or other input methods
    print(prompt("Describe the solar system."))
