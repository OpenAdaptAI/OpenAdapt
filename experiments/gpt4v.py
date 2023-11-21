"""Example usage of GPT4-V API.

Usage:

    OPENAI_API_KEY=<your_api_key> python3 gpt4v.py \
        [<path/to/image1.png>] [<path/to/image2.jpg>] [...] "text prompt"

Example:

    OPENAI_API_KEY=xxx python3 gpt4v.py photo.png "What's in this photo?"
"""

from pprint import pprint
import base64
import json
import mimetypes
import os
import requests
import sys


api_key = os.getenv("OPENAI_API_KEY")


def encode_image(image_path: str):
    """Encodes an image to base64 and determines the correct MIME type."""
    mime_type, _ = mimetypes.guess_type(image_path)
    if mime_type is None:
        raise ValueError(f"Cannot determine MIME type for {image_path}")

    with open(image_path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
        return f"data:{mime_type};base64,{encoded_string}"


def create_payload(images: list[str], prompt: str, model="gpt-4-vision-preview", max_tokens=100, detail="high"):
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

    for image in images:
        base64_image = encode_image(image)
        messages[0]["content"].append({
            "type": "image_url",
            "image_url": {
                "url": base64_image,
                "detail": detail,
            }
        })

    return {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens
    }


def query_openai(payload):
    """Sends a request to the OpenAI API and prints the response."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
    return response.json()


def main():
    if len(sys.argv) < 3:
        print("Usage: python script.py [image1.jpg] [image2.png] ... \"Text Prompt\"")
        sys.exit(1)

    # All arguments except the last one are image paths
    image_paths = sys.argv[1:-1]

    # The last argument is the text prompt
    prompt = sys.argv[-1]

    payload = create_payload(image_paths, prompt)
    response = query_openai(payload)
    pprint(response)


if __name__ == "__main__":
    main()
