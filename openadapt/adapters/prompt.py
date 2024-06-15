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
    original_image: Image.Image,
    masked_images: list[Image.Image],
    active_segment_description: str | None,
    exceptions: list[Exception] | None = None,
    dynamic_param_funcs: dict = None,
    **template_params
) -> list[str]:
    images = [original_image] + masked_images
    system_prompt = utils.render_template_from_file("prompts/system.j2")
    if not dynamic_param_funcs:
        dynamic_param_funcs = {}

    results = []
    for driver in DRIVER_ORDER:
        max_images = getattr(driver, 'MAX_IMAGES', sys.maxsize)
        if not isinstance(max_images, int) or max_images <= 0:
            max_images = sys.maxsize

        try:
            for i in range(0, len(images), max_images):
                batch = images[i:i+max_images]
                if len(batch) == 1 and batch[0] == original_image:
                    logger.info("Skipping batch with only the original image.")
                    continue  # Skip batches that only contain the original image

                num_images = len(batch)
                dynamic_params = {param: func(num_images) for param, func in dynamic_param_funcs.items()}
                combined_params = {**template_params, **dynamic_params}

                prompt = utils.render_template_from_file(
                    text_template,
                    active_segment_description=active_segment_description,
                    exceptions=exceptions,
                    **combined_params
                )

                logger.info(f"Processing batch with {num_images} images")
                result = driver.prompt(prompt, system_prompt, batch)
                results.append(result)
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
