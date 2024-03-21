import os
import fire
from gradio_client import Client
from loguru import logger
from PIL import Image
import tempfile
from openadapt import config


# Function to save PIL.Image to a temporary file and return the file path
def save_image_to_temp_file(img: Image.Image) -> str:
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    img.save(temp_file, format="PNG")
    temp_file.close()  # Important to close the file so others can access it
    logger.info(f"Image saved to temporary file: {temp_file.name}")

    return temp_file.name


def predict(server_url: str = config.SOM_SERVER_URL,
            file_path: str = None,
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
        file_path (str): File path of temp image (.png) copy of a PIL image object.
        granularity (float): Granularity value for the operation.
        segmentation_mode (str): Mode of segmentation, either 'Automatic' or 'Interactive'.
        mask_alpha (float): Alpha value for the mask.
        mark_mode (str): Mark mode, either 'Number' or 'Alphabet'.
        annotation_mode (list): List of annotation modes.
        api_name (str): API endpoint name for inference.
    """

    client = Client(server_url)
    result = client.predict(
        {
            "background": file_path,
        },
        granularity,
        segmentation_mode,
        mask_alpha,
        mark_mode,
        annotation_mode,
        api_name=api_name
    )
    logger.info(result)

    return result


def predict_for_image(image: Image.Image):
    """
    Process an image directly using PIL.Image.Image object and predict using the Gradio client.

    Args:
        image (PIL.Image.Image): A PIL image object.
    """
    img_temp_path = save_image_to_temp_file(image)  # Save the image to a temp file
    segmented_image = predict(file_path=img_temp_path)  # Perform prediction
    os.remove(img_temp_path)  # Delete the temp file after prediction
    return segmented_image


if __name__ == "__main__":
    image = Image.open("C:/Users/x/Desktop/calc_Screenshot.png")
    fire.Fire(predict_for_image(image))
