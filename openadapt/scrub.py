"""Module to scrub text of all PII/PHI.

Usage:

    $ python openadapt/scrub.py scrub_text str_arg
    $ python openadapt/scrub.py scrub_image Image_arg
    
"""

from PIL import Image
from mss import base
from presidio_anonymizer.entities import OperatorConfig
from presidio_image_redactor import ImageRedactorEngine
import fire

from openadapt import config, utils


def scrub_text(text: str,  is_hyphenated: bool = False) -> str:
    """
    Scrub the text of all PII/PHI using Presidio Analyzer and Anonymizer

    Args:
        text (str): Text to be scrubbed

    Returns:
        str: Scrubbed text
    """

    if text is None:
        return None

    if  is_hyphenated:
        text = ''.join(text.split('-'))

    analyzer_results = config.ANALYZER.analyze(
        text=text, entities=config.SCRUBBING_ENTITIES, language="en"
    )

    operators = {}
    for entity in analyzer_results:
        operators[entity.entity_type] = OperatorConfig(
            "mask",
            {
                "masking_char": "*",
                "chars_to_mask": entity.end - entity.start,
                "from_end": True,
            },
        )

        # TODO: remove this print statement after testing
        print(
            f"Recognized entity: {entity.entity_type} - start: {entity.start} end: {entity.end}"
        )

    anonymized_results = config.ANONYMIZER.anonymize(
        text=text,
        analyzer_results=analyzer_results,
        operators=operators,
    )

    return anonymized_results.text


def scrub_image(image: Image, fill_color=config.DEFAULT_SCRUB_FILL_COLOR) -> Image:
    """
    Scrub the image of all PII/PHI using Presidio Image Redactor

    Args:
        image (PIL.Image): A PIL.Image object to be scrubbed

    Returns:
        PIL.Image: The scrubbed image with PII and PHI removed.
    """

    # Initialize the engine
    engine = ImageRedactorEngine()

    # Redact the image
    redacted_image = engine.redact(
        image, fill=fill_color, entities=config.SCRUBBING_ENTITIES
    )

    # Return the redacted image data
    return redacted_image


def scrub_screenshot(
    screenshot: base.ScreenShot, fill_color=config.DEFAULT_SCRUB_FILL_COLOR
) -> base.ScreenShot:
    """
    Scrub the screenshot of all PII/PHI using Presidio Image Redactor

    Args:
        screenshot (mss.base.ScreenShot): An mss.base.ScreenShot object to be scrubbed

    Returns:
        mss.base.ScreenShot: The scrubbed screenshot with PII and PHI removed.
    """

    # Convert the MSS screenshot object to a PIL Image
    image = Image.frombytes("RGBA", screenshot.size, screenshot.bgra, "raw", "BGRA")

    # Use the scrub_image function to scrub the image
    redacted_image = scrub_image(image, fill_color)

    # Convert the redacted PIL Image back into an mss.base.ScreenShot object
    raw_data = bytearray(redacted_image.tobytes("raw", "RGB"))

    # Prepare monitor information from the original screenshot
    monitor_info = {
        "left": screenshot.left,
        "top": screenshot.top,
        "width": screenshot.width,
        "height": screenshot.height
    }

    # Construct a new screenshot with the redacted image data
    redacted_screenshot = base.ScreenShot(raw_data, monitor_info)

    # Return the redacted screenshot
    return redacted_screenshot


def scrub_dict(input_dict: dict, list_keys: list = None) -> dict:
    """
    Scrub the dict of all PII/PHI using Presidio Analyzer and Anonymizer.

    Args:
        input_dict (dict): A dict to be scrubbed

    Returns:
        dict: The scrubbed dict with PII and PHI removed.
    """

    if list_keys is None:
        list_keys = config.SCRUB_KEYS_HTML

    scrubbed_dict = {}
    for key, value in input_dict.items():
        if isinstance(value, str) and key in list_keys:
            scrubbed_dict[key] = scrub_text(value)
        elif isinstance(value, list):
            scrubbed_list = []
            for item in value:
                if isinstance(item, str) and key in list_keys:
                    scrubbed_list.append(scrub_text(item))
                elif isinstance(item, dict):
                    scrubbed_list.append(scrub_dict(item, list_keys))
                else:
                    scrubbed_list.append(item)
            scrubbed_dict[key] = scrubbed_list
        elif isinstance(value, dict):
            scrubbed_dict[key] = scrub_dict(value, list_keys)
        else:
            scrubbed_dict[key] = value

    return scrubbed_dict


def scrub_list_dicts(input_list: list[dict], list_keys: list = None) -> list[dict]:
    """
    Scrub the list of dicts of all PII/PHI using Presidio Analyzer and Anonymizer.

    Args:
        input_list (list[dict]): A list of dicts to be scrubbed

    Returns:
        list[dict]: The scrubbed list of dicts with PII and PHI removed.
    """

    scrubbed_list_dicts = []
    for input_dict in input_list:
        scrubbed_list_dicts.append(scrub_dict(input_dict, list_keys))

    return scrubbed_list_dicts


if __name__ == "__main__":
    fire.Fire(utils.get_functions(__name__))
