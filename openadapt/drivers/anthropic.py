"""Adapter for Anthropic API with vision support."""

from pprint import pprint

from PIL import Image
import anthropic

from openadapt import cache
from openadapt.config import config
from openadapt.custom_logger import logger

MAX_TOKENS = 4096
# from https://docs.anthropic.com/claude/docs/vision
MAX_IMAGES = 20
MODEL_NAME = "claude-3-5-sonnet-20241022"


@cache.cache()
def create_payload(
    prompt: str,
    system_prompt: str | None = None,
    images: list[Image.Image] | None = None,
    model: str = MODEL_NAME,
    max_tokens: int | None = None,
) -> dict:
    """Creates the payload for the Anthropic API request with image support."""
    from openadapt import utils

    messages = []

    user_message_content = []

    max_tokens = max_tokens or MAX_TOKENS
    if max_tokens > MAX_TOKENS:
        logger.warning(f"{max_tokens=} > {MAX_TOKENS=}")
        max_tokens = MAX_TOKENS

    # Add base64 encoded images to the user message content
    if images:
        for image in images:
            image_base64 = utils.image2utf8(image, "PNG")
            # Extract media type and base64 data
            # TODO: don't add it to begin with
            media_type, image_base64_data = image_base64.split(";base64,", 1)
            media_type = media_type.split(":")[-1]  # Remove 'data:' prefix

            user_message_content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_base64_data,
                    },
                }
            )

    # Add text prompt
    user_message_content.append(
        {
            "type": "text",
            "text": prompt,
        }
    )

    # Construct user message
    messages.append(
        {
            "role": "user",
            "content": user_message_content,
        }
    )

    # Prepare the full payload
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
        "betas": ["computer-use-2024-10-22"],
    }

    # Add system_prompt as a top-level parameter if provided
    if system_prompt:
        payload["system"] = system_prompt

    return payload


@cache.cache()
def get_completion(
    payload: dict, dev_mode: bool = False, api_key: str = config.ANTHROPIC_API_KEY
) -> str:
    """Sends a request to the Anthropic API and returns the response."""
    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.beta.messages.create(**payload)
    except Exception as exc:
        logger.exception(exc)
        if dev_mode:
            import ipdb

            ipdb.set_trace()
        else:
            raise
    """
    Message(
        id='msg_01L55ai2A9q92687mmjMSch3',
        content=[
            ContentBlock(
                text='{
                    "action": [
                        {
                            "name": "press",
                            "key_name": "cmd",
                            "canonical_key_name": "cmd"
                        },
                        ...
                    ]
                }',
                type='text'
            )
        ],
        model='claude-3-opus-20240229',
        role='assistant',
        stop_reason='end_turn',
        stop_sequence=None,
        type='message',
        usage=Usage(input_tokens=4379, output_tokens=109))
    """
    texts = [content_block.text for content_block in response.content]
    return "\n".join(texts)


def prompt(
    prompt: str,
    system_prompt: str | None = None,
    images: list[Image.Image] | None = None,
    max_tokens: int | None = None,
) -> str:
    """Public method to get a response from the Anthropic API with image support."""
    if len(images) > MAX_IMAGES:
        # XXX TODO handle this
        raise Exception(f"{len(images)=} > {MAX_IMAGES=}. Use a different adapter.")
    payload = create_payload(
        prompt,
        system_prompt,
        images,
        max_tokens=max_tokens,
    )
    # pprint(f"payload=\n{payload}")  # Log payload for debugging
    result = get_completion(payload)
    pprint(f"result=\n{result}")  # Log result for debugging
    return result
