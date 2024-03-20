import fire
from gradio_client import Client
from loguru import logger
from PIL import Image
import tempfile
import typing


# Function to save PIL.Image to a temporary file and return the file path
def save_image_to_temp_file(img: Image.Image) -> str:
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    img.save(temp_file, format="PNG")
    temp_file.close()  # Important to close the file so others can access it
    logger.info(f"Image saved to temporary file: {temp_file.name}")
    return temp_file.name


def predict(server_url: str = "http://44.222.184.95:6092/",
            background: typing.Union[str, Image.Image] = "C:/Users/x/Desktop/calc_Screenshot.png",
            granularity: float = 2.7, 
            segmentation_mode: str = "Automatic",
            mask_alpha: float = 0.8,
            mark_mode: str = "Number",
            annotation_mode: list = ["Mask"],
            api_name: str = "/inference"):
    """
    Makes a prediction using the Gradio client with the provided IP address and other parameters.

    Args:
        server_url (str): The URL of the SoM Gradio server.
        background (Union[str, PIL.Image.Image]): URL to the background image or a PIL image object.
        granularity (float): Granularity value for the operation.
        segmentation_mode (str): Mode of segmentation, either 'Automatic' or 'Interactive'.
        mask_alpha (float): Alpha value for the mask.
        mark_mode (str): Mark mode, either 'Number' or 'Alphabet'.
        annotation_mode (list): List of annotation modes.
        api_name (str): API endpoint name for inference.
    """

    try:
        # Load PIL.Image from memory or create it
        img = Image.open(background)
        # Save the image to a temporary file and get the file path
        img_temp_path = save_image_to_temp_file(img)
        logger.info(f"Background image path: {img_temp_path}")
    except (FileNotFoundError, OSError):
        # If it fails, assume it's a URL
        logger.info("Background URL:", background)

    client = Client(server_url)
    result = client.predict(
        {
            "background": img_temp_path,
        },
        granularity,
        segmentation_mode,
        mask_alpha,
        mark_mode,
        annotation_mode,
        api_name=api_name
    )
    logger.info(result)


if __name__ == "__main__":
        fire.Fire(predict)
