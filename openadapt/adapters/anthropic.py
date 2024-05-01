"""Adapter for Anthropic API with vision support."""

from pprint import pprint

from loguru import logger
import anthropic

from openadapt import cache
from openadapt.config import config

MAX_TOKENS = 4096
# from https://docs.anthropic.com/claude/docs/vision
MAX_IMAGES = 20
MODEL_NAME = "claude-3-opus-20240229"


@cache.cache()
def create_payload(
    prompt: str,
    system_prompt: str | None = None,
    base64_images: list[tuple[str, str]] | None = None,
    model: str = MODEL_NAME,
    max_tokens: int | None = None,
) -> dict:
    """Creates the payload for the Anthropic API request with image support."""
    messages = []

    user_message_content = []

    max_tokens = max_tokens or MAX_TOKENS
    if max_tokens > MAX_TOKENS:
        logger.warning(f"{max_tokens=} > {MAX_TOKENS=}")
        max_tokens = MAX_TOKENS

    # Add base64 encoded images to the user message content
    if base64_images:
        for image_data in base64_images:
            # Extract media type and base64 data
            media_type, base64_str = image_data.split(";base64,", 1)
            media_type = media_type.split(":")[-1]  # Remove 'data:' prefix

            user_message_content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64_str,
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
    }

    # Add system_prompt as a top-level parameter if provided
    if system_prompt:
        payload["system"] = system_prompt

    return payload


client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


@cache.cache()
def get_completion(payload: dict) -> str:
    """Sends a request to the Anthropic API and returns the response."""
    try:
        response = client.messages.create(**payload)
    except Exception as exc:
        logger.exception(exc)
        import ipdb

        ipdb.set_trace()
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
    base64_images: list[str] | None = None,
    max_tokens: int | None = None,
) -> str:
    """Public method to get a response from the Anthropic API with image support."""
    if len(base64_images) > MAX_IMAGES:
        # XXX TODO handle this
        raise Exception(
            f"{len(base64_images)=} > {MAX_IMAGES=}. Use a different adapter."
        )
    payload = create_payload(
        prompt,
        system_prompt,
        base64_images,
        max_tokens=max_tokens,
    )
    # pprint(f"payload=\n{payload}")  # Log payload for debugging
    result = get_completion(payload)
    pprint(f"result=\n{result}")  # Log result for debugging
    return result
