from openadapt import som, utils
from openadapt.db import crud

def test_get_marked_image():
    if 0:
        recording = crud.get_latest_recording()
        action_event = next(iter(recording.action_events))
        image = action_event.screenshot.image
    else:
        image_path = "./experiments/spreadsheet.png"
        from PIL import Image
        image = Image.open(image_path)

    marked_image = som.get_marked_image(image)
    utils.display_two_images(image, marked_image)


if __name__ == "__main__":
    test_get_marked_image()
