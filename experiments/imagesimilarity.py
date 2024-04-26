import time

from matplotlib.offsetbox import OffsetImage, AnnotationBbox
from PIL import Image, ImageOps
from sklearn.manifold import MDS
import imagehash
import matplotlib.pyplot as plt
import numpy as np

from openadapt.db import crud


def calculate_dynamic_threshold(
    im1: Image.Image,
    im2: Image.Image,
    k: float = 1.0,
) -> float:
    """Calculate a dynamic threshold for image difference.

    Based on the standard deviation of the pixel differences.

    Args:
        im1 (Image.Image): The first image.
        im2 (Image.Image): The second image.
        k (float): The multiplier for the standard deviation to set the threshold.

    Returns:
        float: The dynamically calculated threshold.
    """
    # Convert images to numpy arrays
    arr1 = np.array(im1)
    arr2 = np.array(im2)

    # Calculate the absolute difference between the images
    diff = np.abs(arr1 - arr2)

    # Calculate mean and standard deviation of the differences
    mean_diff = np.mean(diff)
    std_diff = np.std(diff)

    # Calculate the threshold as mean plus k times the standard deviation
    threshold = mean_diff + k * std_diff

    return threshold


def thresholded_difference(im1: Image.Image, im2: Image.Image, k: float = 1.0) -> int:
    """
    Return the difference between two images by computing the number of pixels
    differing by a dynamically calculated threshold based on the standard deviation.

    Args:
        im1 (Image.Image): The first image.
        im2 (Image.Image): The second image.
        k (float): Multiplier for the standard deviation to set the dynamic threshold.

    Returns:
        int: The number of pixels differing by at least the dynamically calculated
        threshold.
    """
    common_size = (min(im1.width, im2.width), min(im1.height, im2.height))
    im1 = im1.resize(common_size)
    im2 = im2.resize(common_size)

    # Calculate the dynamic threshold
    difference_threshold = calculate_dynamic_threshold(im1, im2, k)

    # Convert images to numpy arrays
    arr1 = np.array(im1)
    arr2 = np.array(im2)

    # Calculate the absolute difference between the images
    diff = np.abs(arr1 - arr2)

    # Count pixels with a difference above the dynamically calculated threshold
    count = np.sum(diff >= difference_threshold)

    return count


def prepare_image(
    img: Image.Image,
    size: tuple[int, int] = (64, 64),
    border: int = 2,
    color: str = 'red',
) -> Image.Image:
    """
    Resize an image to a common size, add a border to it.

    Args:
        img (Image.Image): The original image to prepare.
        size (tuple[int, int]): The size to which the images should be resized.
        border (int): The width of the border around the image.
        color (str): The color of the border.

    Returns:
        Image.Image: The processed image.
    """
    # Resize image
    img = img.resize(size, Image.ANTIALIAS)

    # Add border to the image
    img_with_border = ImageOps.expand(img, border=border, fill=color)

    return img_with_border


def plot_images_with_mds(
    images: list[Image.Image],
    distance_matrix: np.ndarray,
    title: str,
):
    """
    Plot images on a scatter plot based on the provided distance matrix using MDS.

    Args:
        images (list[Image.Image]): list of images to plot.
        distance_matrix (np.ndarray): A distance matrix of image differences.
        title (str): Title of the plot.
    """
    # Prepare images by resizing and adding a border
    prepared_images = [prepare_image(img) for img in images]

    # Initialize MDS and fit the distance matrix to get the 2D embedding
    mds = MDS(n_components=2, dissimilarity='precomputed', random_state=0)
    positions = mds.fit_transform(distance_matrix)

    # Create a scatter plot with the MDS results
    fig, ax = plt.subplots(figsize=(12, 7))
    ax.scatter(positions[:, 0], positions[:, 1], alpha=0)

    # Define properties for the bounding box
    bbox_props = dict(boxstyle="round,pad=0.3", ec="b", lw=2, fc="white", alpha=0.7)

    # Loop through images and positions to create and place the annotations
    for img, (x, y) in zip(prepared_images, positions):
        im = OffsetImage(np.array(img), zoom=0.5)  # Adjust zoom level if necessary
        ab = AnnotationBbox(
            im, (x, y), xycoords='data', frameon=True, bboxprops=bbox_props
        )
        ax.add_artist(ab)

    # Remove the x and y ticks
    ax.set_xticks([])
    ax.set_yticks([])

    plt.title(title)
    plt.show()


def main():
    recording = crud.get_latest_recording()
    action_events = recording.processed_action_events
    images = [action_event.screenshot.cropped_image for action_event in action_events]

    similarity_funcs = {
        #"thresholded_difference": thresholded_difference,
        #"average_hash": lambda im1, im2: (
        #    imagehash.average_hash(im1) - imagehash.average_hash(im2)
        #),
        "dhash": lambda im1, im2: (
            imagehash.dhash(im1) - imagehash.dhash(im2)
        ),
        "phash": lambda im1, im2: (
            imagehash.phash(im1) - imagehash.phash(im2)
        ),
        #"crop_resistant_hash": lambda im1, im2: (
        #    imagehash.crop_resistant_hash(im1) - imagehash.crop_resistant_hash(im2)
        #),
        #"colorhash": lambda im1, im2: (
        #    imagehash.colorhash(im1) - imagehash.colorhash(im2)
        #),
        #"whash": lambda im1, im2: imagehash.whash(im1) - imagehash.whash(im2),
    }

    # Process each similarity function
    for func_name, func in similarity_funcs.items():
        # Create a matrix to store all pairwise distances
        n = len(images)
        distance_matrix = np.zeros((n, n))
        durations = []
        for i in range(n):
            for j in range(i + 1, n):
                start_time = time.time()
                distance = abs(func(images[i], images[j]))
                duration = time.time() - start_time
                durations.append(duration)
                distance_matrix[i, j] = distance
                distance_matrix[j, i] = distance
        mean_duration = sum(durations) / len(durations)
        plot_images_with_mds(
            images,
            distance_matrix,
            f"Image layout based on {func_name} ({mean_duration=:.4f}s)",
        )


if __name__ == "__main__":
    main()
