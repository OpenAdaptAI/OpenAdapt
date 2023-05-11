import base64
from io import BytesIO
from PIL import Image
from puterbot.scrub_image import scrub_image


def test_scrub_image():
    # Load the image from file
    image_path = 'test_image.jpeg'
    with open(image_path, 'rb') as f:
        image_data = f.read()

    # Encode the image data as base64 and scrub it
    encoded_data = base64.b64encode(image_data).decode('utf-8')
    scrubbed_data = scrub_image(encoded_data)

    # Decode the scrubbed image data and save it as a new file
    decoded_data = base64.b64decode(scrubbed_data)
    scrubbed_image = Image.open(BytesIO(decoded_data))
    scrubbed_image_path = 'scrubbed_test_image.jpeg'
    scrubbed_image.save(scrubbed_image_path)
