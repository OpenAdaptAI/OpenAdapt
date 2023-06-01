"""Script to scrub text of all PII/PHI.

Usage:

    $ python openadapt/scrub.py scrub_text str_arg
    $ python openadapt/scrub.py scrub_image PIL.Image_arg
    $ python openadapt/scrub.py scrub_dict dict_arg
    $ python openadapt/scrub.py scrub_list_dicts list_of_dicts_arg

"""

from typing import List, Dict, Union, Any
from PIL import Image
from presidio_anonymizer.entities import OperatorConfig
import fire

from openadapt import config, utils


def scrub_text(text: str, is_separated: bool = False) -> str:
    """
    Scrub the text of all PII/PHI using Presidio ANALYZER.TRF and Anonymizer

    Args:
        text (str): Text to be scrubbed

    Returns:
        str: Scrubbed text
    """

    if text is None:
        return None

    if is_separated and not (
        text.startswith(config.TEXT_NAME_PREFIX)
        or text.endswith(config.TEXT_NAME_SUFFIX)
    ):
        text = "".join(text.split(config.TEXT_SEP))

    analyzer_results = config.ANALYZER_TRF.analyze(
        text=text,
        entities=config.SCRUBBING_ENTITIES,
        language=config.SCRUB_LANGUAGE,
    )

    operators = {}
    for entity in analyzer_results:
        operators[entity.entity_type] = OperatorConfig(
            "mask",
            {
                "masking_char": config.SCRUB_CHAR,
                "chars_to_mask": entity.end - entity.start,
                "from_end": True,
            },
        )

    anonymized_results = config.ANONYMIZER.anonymize(
        text=text,
        analyzer_results=analyzer_results,
        operators=operators,
    )

    if is_separated and not (
        text.startswith(config.TEXT_NAME_PREFIX)
        or text.endswith(config.TEXT_NAME_SUFFIX)
    ):
        anonymized_results.text = config.TEXT_SEP.join(
            anonymized_results.text
        )

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


def _should_scrub_text(
    key: Any, value: Any, list_keys: List[str], scrub_all: bool = False
) -> bool:
    """
    Check if the key and value should be scrubbed and are of correct instance.

    Args:
        key (Any): The key of the dict item
        value (Any): The value of the dict item
        list_keys (list): A list of keys that are needed to be scrubbed
        scrub_all (bool): Whether to scrub
            all sub-field/keys/values of that particular key

    Returns:
        bool: True if the key and value should be scrubbed, False otherwise
    """
    return (
        isinstance(value, str)
        and isinstance(key, str)
        and (key in list_keys or scrub_all)
    )


def _scrub_text_item(value: str, key: Any) -> str:
    """
    Scrubs the value of a dict item.

    Args:
        value (str): The value of the dict item
        key (Any): The key of the dict item

    Returns:
        str: The scrubbed value
    """
    if key in ("text", "canonical_text"):
        return scrub_text(value, is_separated=True)
    return scrub_text(value)


def _should_scrub_list_item(
    item: Any, key: Any, list_keys: List[str]
) -> bool:
    """
    Check if the key and item should be scrubbed and are of correct instance.

    Args:
        item (str/dict/other): The value of the dict item
        key (str): The key of the dict item
        list_keys (list): A list of keys that are needed to be scrubbed

    Returns:
        bool: True if the key and value should be scrubbed, False otherwise
    """
    return (
        isinstance(item, (str, dict))
        and isinstance(key, str)
        and key in list_keys
    )


def _scrub_list_item(
    item: Union[str, Dict], key: str, list_keys: List[str]
) -> Union[Dict, str]:
    """
    Scrubs the value of a dict item.

    Args:
        item (str/dict): The value of the dict item
        key (str): The key of the dict item
        list_keys (list): A list of keys that are needed to be scrubbed

    Returns:
        dict/str: The scrubbed dict/value respectively
    """
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
    input_list: List[Dict], list_keys: List = None
) -> List[Dict]:
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
