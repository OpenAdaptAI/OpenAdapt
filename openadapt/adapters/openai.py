"""Adapter for GPT4-V API.

https://platform.openai.com/docs/guides/vision
"""

from pprint import pformat

from loguru import logger
import requests

from openadapt import cache
from openadapt.config import config

MODEL_NAME = [
    "gpt-4-vision-preview",
    "gpt-4-turbo-2024-04-09",
][-1]
MAX_TOKENS = 4096
# TODO XXX undocumented
MAX_IMAGES = None


def create_payload(
    prompt: str,
    system_prompt: str | None = None,
    base64_images: list[str] | None = None,
    model: str = MODEL_NAME,
    detail: str = "high",  # "low" or "high"
    max_tokens: int | None = None,
) -> dict:
    """Create payload for prompting.

    Args:
        prompt: the prompt
        system_prompt: the system prompt
        base64_images: list of base64 encoded images
        model: name of OpenAI model
        detail: detail level of images, "low" or "high"
        max_tokens: maximum number of tokens

    Returns:
        dict containing prompt payload
    """
    max_tokens = max_tokens or MAX_TOKENS
    if max_tokens > MAX_TOKENS:
        logger.warning(f"{max_tokens=} {MAX_TOKENS=}")
        max_tokens = MAX_TOKENS

    """Creates the payload for the API request."""
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": prompt,
                },
            ],
        },
    ]

    base64_images = base64_images or []
    for base64_image in base64_images:
        messages[0]["content"].append(
            {
                "type": "image_url",
                "image_url": {
                    "url": base64_image,
                    "detail": detail,
                },
            }
        )

    if system_prompt:
        messages = [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": system_prompt,
                    }
                ],
            }
        ] + messages

    rval = {
        "model": model,
        "messages": messages,
    }
    if max_tokens:
        rval["max_tokens"] = max_tokens
    return rval


@cache.cache()
def get_response(payload: dict) -> requests.Response:
    """Sends a request to the OpenAI API and returns the response.

    Args:
        payload: dictionary returned by create_payload

    Returns:
        response from OpenAI API
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.OPENAI_API_KEY}",
    }
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers=headers,
        json=payload,
    )
    return response


def get_completion(payload: dict) -> str:
    """Sends a request to the OpenAI API and returns the first message.

    Args:
        pyalod: dictionary returned by create_payload

    Returns:
        string containing the first message from the response
    """
    response = get_response(payload)
    result = response.json()
    logger.info(f"result=\n{pformat(result)}")
    if "error" in result:
        error = result["error"]
        message = error["message"]
        # TODO: fail after maximum number of attempts
        if "retry your request" in message:
            return get_completion(payload)
        else:
            import ipdb

            ipdb.set_trace()
            # TODO: handle more errors
    choices = result["choices"]
    choice = choices[0]
    message = choice["message"]
    content = message["content"]
    return content


def prompt(
    prompt: str,
    system_prompt: str | None = None,
    base64_images: list[str] | None = None,
    max_tokens: int | None = None,
    detail: str = "high",
) -> str:
    """Get prompt completion from OpenAI.

    Args:
        prompt: the prompt
        system_prompt: the system prompt
        base64_images: list of base64 encoded images
        model: name of OpenAI model
        detail: detail level of images, "low" or "high"
        max_tokens: maximum number of tokens

    Returns:
        string containing the first message from the response
    """
    payload = create_payload(
        prompt,
        system_prompt,
        base64_images,
        max_tokens=max_tokens,
        detail=detail,
    )
    logger.info(f"payload=\n{pformat(payload)}")
    result = get_completion(payload)
    logger.info(f"result=\n{pformat(result)}")
    return result
