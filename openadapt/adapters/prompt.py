from loguru import logger
from typing import Type
from PIL import Image


from openadapt.drivers import anthropic, google, openai


# Define a list of drivers in the order they should be tried
DRIVER_ORDER: list[Type] = [openai, google, anthropic]


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


if __name__ == "__main__":
    # This could be extended to use command-line arguments or other input methods
    print(prompt("Describe the solar system."))
