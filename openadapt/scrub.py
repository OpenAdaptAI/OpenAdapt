"""Script to scrub text of all PII/PHI.

Usage:

    $ python openadapt/scrub.py scrub_text str_arg
    $ python openadapt/scrub.py scrub_image PIL.Image_arg
    $ python openadapt/scrub.py scrub_dict dict_arg
    $ python openadapt/scrub.py scrub_list_dicts list_of_dicts_arg

"""

from PIL import Image
from presidio_anonymizer.entities import OperatorConfig
import fire

from openadapt import config, utils


def scrub_text(text: str, is_hyphenated: bool = False) -> str:
    """
    Scrub the text of all PII/PHI using Presidio ANALYZER.TRF and Anonymizer

    Args:
        text (str): Text to be scrubbed

    Returns:
        str: Scrubbed text
    """

    if text is None:
        return None

    if is_hyphenated and not (
        text.startswith("<") or text.endswith(">")
    ):
        text = "".join(text.split("-"))

    analyzer_results = config.ANALYZER_TRF.analyze(
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

    anonymized_results = config.ANONYMIZER.anonymize(
        text=text,
        analyzer_results=analyzer_results,
        operators=operators,
    )

    if is_hyphenated and not (
        text.startswith("<") or text.endswith(">")
    ):
        anonymized_results.text = "-".join(anonymized_results.text)

    return anonymized_results.text


def scrub_image(
    image: Image, fill_color=config.DEFAULT_SCRUB_FILL_COLOR
) -> Image:
    """
    Scrub the image of all PII/PHI using Presidio Image Redactor

    Args:
        image (PIL.Image): A PIL.Image object to be scrubbed

    Returns:
        PIL.Image: The scrubbed image with PII and PHI removed.
    """

    # Redact the image
    redacted_image = config.IMAGE_REDACTOR.redact(
        image, fill=fill_color, entities=config.SCRUBBING_ENTITIES
    )

    # Return the redacted image data
    return redacted_image


def _should_scrub_text(key, value, list_keys, scrub_all=False):
    return (
        isinstance(value, str)
        and isinstance(key, str)
        and (key in list_keys or scrub_all)
    )


def _scrub_text_item(value, key):
    if key in ("text", "canonical_text"):
        return scrub_text(value, is_hyphenated=True)
    return scrub_text(value)


def _should_scrub_list_item(item, key, list_keys):
    return (
        isinstance(item, (str, dict))
        and isinstance(key, str)
        and key in list_keys
    )


def _scrub_list_item(item, key, list_keys):
    if isinstance(item, dict):
        return scrub_dict(item, list_keys)
    return _scrub_text_item(item, key)


def scrub_dict(
    input_dict: dict, list_keys: list = None, scrub_all: bool = False
) -> dict:
    """
    Scrub the dict of all PII/PHI using Presidio ANALYZER.TRF and Anonymizer.

    Args:
        input_dict (dict): A dict to be scrubbed

    Returns:
        dict: The scrubbed dict with PII and PHI removed.
    """

    if list_keys is None:
        list_keys = config.SCRUB_KEYS_HTML

    scrubbed_dict = {}
    for key, value in input_dict.items():
        if _should_scrub_text(key, value, list_keys, scrub_all):
            scrubbed_dict[key] = _scrub_text_item(value, key)
        elif isinstance(value, list):
            scrubbed_list = [
                _scrub_list_item(item, key, list_keys)
                if _should_scrub_list_item(item, key, list_keys)
                else item
                for item in value
            ]
            scrubbed_dict[key] = scrubbed_list
        elif isinstance(value, dict):
            if isinstance(key, str) and key == "state":
                scrubbed_dict[key] = scrub_dict(
                    value, list_keys, scrub_all=True
                )
            else:
                scrubbed_dict[key] = scrub_dict(value, list_keys)
        else:
            scrubbed_dict[key] = value

    return scrubbed_dict


def scrub_list_dicts(
    input_list: list[dict], list_keys: list = None
) -> list[dict]:
    """
    Scrub the list of dicts of all PII/PHI
    using Presidio ANALYZER.TRF and Anonymizer.

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
