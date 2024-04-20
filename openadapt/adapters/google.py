"""Adapter for Google Gemini.

See https://ai.google.dev/tutorials/python_quickstart for documentation.
"""

from pprint import pprint

from PIL import Image
import fire
import google.generativeai as genai

from openadapt import cache, utils
from openadapt.config import config

MAX_TOKENS = 2**20  # 1048576
MODEL_NAME = [
    "gemini-pro-vision",
    "models/gemini-1.5-pro-latest",
][-1]
# https://cloud.google.com/vertex-ai/generative-ai/docs/multimodal/send-multimodal-prompts
MAX_IMAGES = {
    "gemini-pro-vision": 16,
    "models/gemini-1.5-pro-latest": 3000,
}[MODEL_NAME]


@cache.cache()
def prompt(
    prompt: str,
    system_prompt: str | None = None,
    base64_images: list[str] | None = None,
    # max_tokens: int | None = None,
    model_name: str = MODEL_NAME,
) -> str:
    """Public method to get a response from the Google API with image support."""
    full_prompt = "\n\n###\n\n".join([s for s in (system_prompt, prompt) if s])
    # HACK
    full_prompt += "\nWhen responding in JSON, you MUST use double quotes around keys."

    # TODO: modify API across all adapters to accept PIL.Image
    images = (
        [utils.utf82image(base64_image) for base64_image in base64_images]
        if base64_images
        else []
    )

    genai.configure(api_key=config.GOOGLE_API_KEY)
    model = genai.GenerativeModel(model_name)
    response = model.generate_content([full_prompt] + images)
    response.resolve()
    pprint(f"response=\n{response}")  # Log response for debugging
    return response.text


def main(text: str, image_path: str | None = None) -> None:
    """Prompt Google Gemini with text and a path to an image."""
    if image_path:
        with Image.open(image_path) as img:
            # Convert image to RGB if it's RGBA (to remove alpha channel)
            if img.mode in ("RGBA", "LA") or (
                img.mode == "P" and "transparency" in img.info
            ):
                img = img.convert("RGB")
            base64_image = utils.image2utf8(img)
    else:
        base64_image = None

    base64_images = [base64_image] if base64_image else None
    output = prompt(text, base64_images=base64_images)
    print(output)


if __name__ == "__main__":
    fire.Fire(main)
