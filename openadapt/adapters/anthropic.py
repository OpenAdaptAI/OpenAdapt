"""Adapter for Anthropic API with vision support."""

from pprint import pprint
import base64
import json
import mimetypes
import os
import requests
import sys

from openadapt import cache, config
import anthropic


MAX_TOKENS = 4096


@cache.cache()
def create_payload(
    prompt: str,
    system_prompt: str | None = None,
    base64_images: list[tuple[str, str]] | None = None,
    model="claude-3-opus-20240229",
    max_tokens=None,
):
    """Creates the payload for the Anthropic API request with image support."""
    messages = []

    user_message_content = []

    max_tokens = max_tokens or MAX_TOKENS
    # max_tokens: 16384 > 4096, which is the maximum allowed number of output tokens for claude-3-opus-20240229
    if max_tokens > MAX_TOKENS:
        max_tokens = MAX_TOKENS

    # Add base64 encoded images to the user message content
    if base64_images:
        for image_data in base64_images:
            # Extract media type and base64 data
            media_type, base64_str = image_data.split(';base64,', 1)
            media_type = media_type.split(':')[-1]  # Remove 'data:' prefix
            
            user_message_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64_str,
                },
            })

    # Add text prompt
    user_message_content.append({
        "type": "text",
        "text": prompt,
    })

    # Construct user message
    messages.append({
        "role": "user",
        "content": user_message_content,
    })

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

client = anthropic.Anthropic(
    api_key=config.ANTHROPIC_API_KEY
)

@cache.cache()
def get_completion(payload):
    """Sends a request to the Anthropic API and returns the response."""
    response = client.messages.create(**payload)
    # Message(id='msg_01L55ai2A9q92687mmjMSch3', content=[ContentBlock(text='{"action": [{"name": "press", "key_name": "cmd", "canonical_key_name": "cmd"}, {"name": "press", "key_name": "space", "canonical_key_vk": "49"}, {"name": "release", "key_name": "space", "canonical_key_vk": "49"}, {"name": "release", "key_name": "cmd", "canonical_key_name": "cmd"}]}', type='text')], model='claude-3-opus-20240229', role='assistant', stop_reason='end_turn', stop_sequence=None, type='message', usage=Usage(input_tokens=4379, output_tokens=109))
    texts = [
        content_block.text
        for content_block in response.content
    ]
    return "\n".join(texts)


def prompt(
    prompt: str,
    system_prompt: str | None = None,
    base64_images: list[tuple[str, str]] | None = None,
    max_tokens: int | None = None,
):
    """Public method to get a response from the Anthropic API with image support."""
    payload = create_payload(
        prompt,
        system_prompt,
        base64_images,
        max_tokens=max_tokens,
    )
    #pprint(f"payload=\n{payload}")  # Log payload for debugging
    result = get_completion(payload)
    pprint(f"result=\n{result}")  # Log result for debugging
    return result
