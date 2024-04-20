"""Adapter for segmenting images via Set-of-Mark server.

See https://github.com/microsoft/SoM for server implementation.
"""

import os
import tempfile

from loguru import logger
from PIL import Image
import fire
import gradio_client

from openadapt.config import config


def save_image_to_temp_file(img: Image.Image) -> str:
    """Save PIL.Image to a temporary file and return the file path.

    Args:
        img: the PIL Image to save

    Returns:
        Name of temporary file containing saved image.
    """
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    img.save(temp_file, format="PNG")
    temp_file.close()  # Important to close the file so others can access it
    logger.info(f"Image saved to temporary file: {temp_file.name}")

    return temp_file.name


def predict(
    file_path: str = None,
    server_url: str = config.SOM_SERVER_URL,
    granularity: float = 2.7,
    segmentation_mode: str = "Automatic",
    mask_alpha: float = 0.8,
    mark_mode: str = "Number",
    annotation_mode: list = ["Mask"],
    api_name: str = "/inference",
) -> str:
    """Make a prediction using the Gradio client with the provided IP address.

    Args:
        server_url (str): The URL of the SoM Gradio server.
        file_path (str): File path of temp image (.png) copy of a PIL image object.
        granularity (float): Granularity value for the operation.
        segmentation_mode (str): Mode of segmentation ('Automatic' or 'Interactive').
        mask_alpha (float): Alpha value for the mask.
        mark_mode (str): Mark mode, either 'Number' or 'Alphabet'.
        annotation_mode (list): List of annotation modes.
        api_name (str): API endpoint name for inference.

    Returns:
        Path to segmented image.
    """
    assert server_url, server_url
    assert server_url.startswith("http"), server_url
    client = gradio_client.Client(server_url)
    result = client.predict(
        {
            "background": gradio_client.file(file_path),
        },
        granularity,
        segmentation_mode,
        mask_alpha,
        mark_mode,
        annotation_mode,
        api_name=api_name,
    )
    logger.info(result)

    return result


def fetch_segmented_image(image: Image.Image) -> Image.Image:
    """Process an image using PIL.Image.Image object and predict using Gradio client.

    Args:
        image: The PIL Image to segment.

    Returns:
        The segmented PIL Image.
    """
    raise NotImplementedError(
        "SoM server currently compresses the segmented image, "
        "resulting in many more colors than masks."
    )
    img_temp_path = save_image_to_temp_file(image)  # Save the image to a temp file
    segmented_image_path = predict(file_path=img_temp_path)  # Perform prediction
    os.remove(img_temp_path)  # Delete the temp file after prediction
    image = Image.open(segmented_image_path)
    os.remove(segmented_image_path)
    return image


if __name__ == "__main__":
    fire.Fire(predict)
