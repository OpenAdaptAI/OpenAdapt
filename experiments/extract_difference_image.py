from PIL import Image
import numpy as np
import cv2
from skimage.metrics import structural_similarity as ssim

def extract_difference_image(
    new_image: Image.Image,
    old_image: Image.Image,
    tolerance: float = 0.05,
) -> Image.Image:
    """Extract the portion of the new image that is different from the old image."""
    new_image_np = np.array(new_image.convert('L'))
    old_image_np = np.array(old_image.convert('L'))

    # Compute the SSIM between the two images
    score, diff = ssim(new_image_np, old_image_np, full=True)
    diff = (diff * 255).astype("uint8")

    # Threshold the difference image to get the regions that are different
    thresh = cv2.threshold(diff, 255 * (1 - tolerance), 255, cv2.THRESH_BINARY_INV)[1]

    # Find contours of the different regions
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Create a mask of the differences
    mask = np.zeros_like(new_image_np)
    cv2.drawContours(mask, contours, -1, (255), thickness=cv2.FILLED)

    # Apply the mask to the new image to extract the different regions
    diff_image_np = cv2.bitwise_and(np.array(new_image), np.array(new_image), mask=mask)

    return Image.fromarray(diff_image_np)

# Example usage:
# new_image = Image.open('path_to_new_image')
# old_image = Image.open('path_to_old_image')
# difference_image = extract_difference_image(new_image, old_image, tolerance=0.05)
# difference_image.show()

new_image = Image.open('./winCalNew.png')
old_image = Image.open('./winCalOld.png')
difference_image = extract_difference_image(new_image, old_image, tolerance=0.05)
difference_image.show()
if __name__ == "__main__":
    main()