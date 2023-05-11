"""Module for scrubbing image data"""
import base64
from PIL import Image, ImageFilter
from io import BytesIO
from puterbot.scrub import scrub


def scrub_image(image_data):
    # Decode the base64-encoded image data
    decoded_data = base64.b64decode(image_data)

    # Load the image using PIL
    image = Image.open(BytesIO(decoded_data))

    # Apply blur filter to sensitive regions of the image
    blurred_image = image.filter(ImageFilter.BLUR)

    # Convert the image back to PNG format and encode as base64
    buffer = BytesIO()
    blurred_image.save(buffer, format="PNG")
    encoded_data = base64.b64encode(buffer.getvalue()).decode("utf-8")

    # Pass the encoded image data to the scrub function
    return scrub(encoded_data)
