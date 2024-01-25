from io import BytesIO

import requests
from PIL import Image
from loguru import logger

from openadapt import config

MODEL_ID = "yolov8n.pt"

def fetch_segmented_image(input_image: Image) -> Image:
    # API URL, use actual MODEL_ID
    url = f"https://api.ultralytics.com/v1/predict/{MODEL_ID}"

    # Headers, use actual API_KEY
    headers = {"x-api-key": config.ULTRALYTICS_API_KEY}

    # Inference arguments (optional)
    data = {"size": 640, "confidence": 0.25, "iou": 0.45}

    # Convert PIL Image to bytes
    buffered = BytesIO()
    input_image.save(buffered, format="JPEG")
    image_bytes = buffered.getvalue()

    # Prepare files dictionary to send in the request
    files = {"image": ("image.jpg", image_bytes)}

    # Send request
    response = requests.post(url, headers=headers, files=files, data=data)
    response.raise_for_status()

    # Check response status
    if response.status_code == 200:
        response_json = response.json()
        # Assuming the API returns the image in bytes under 'segmented_image' key.
        # Adjust the key based on actual API response.
        image_bytes = response_json['segmented_image']
        # Convert bytes to Image
        segmented_image = Image.open(BytesIO(image_bytes))
        return segmented_image
    else:
        logger.error(f"Unable to fetch the image, status code: {response.status_code}")
        return None
