"""Adapter for GPT4-V API."""

from pprint import pformat
import base64
import json
import mimetypes
import os
import requests
import sys

from loguru import logger

from openadapt import cache, config

MAX_TOKENS = 4096

def create_payload(
    prompt: str,
    system_prompt: str | None = None,
    base64_images: list[str] | None = None,
    model="gpt-4-vision-preview",
    detail="high",
    max_tokens=None,
):
    max_tokens = max_tokens or MAX_TOKENS
	# max_tokens is too large: 16384. This model supports at most 4096 completion tokens, whereas you provided 16384.
    if max_tokens > MAX_TOKENS:
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
        messages[0]["content"].append({
            "type": "image_url",
            "image_url": {
                "url": base64_image,
                "detail": detail,
            }
        })

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
def get_completion(payload):
    """Sends a request to the OpenAI API and prints the response."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.OPENAI_API_KEY}"
    }
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers=headers,
        json=payload,
    )
    #return response.json()
    result = response.json()
    #result = adapter.get_completion(payload)
    logger.info(f"result=\n{pformat(result)}")
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
):
    payload = create_payload(
        prompt,
        system_prompt,
        base64_images,
        max_tokens=max_tokens,
    )
    logger.info(f"payload=\n{pformat(payload)}")
    result = get_completion(payload)
    logger.info(f"result=\n{pformat(result)}")
    return result
