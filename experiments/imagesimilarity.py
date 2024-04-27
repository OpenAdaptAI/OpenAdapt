"""This module calculates image similarities using various methods."""

from typing import Callable
import time

from matplotlib.offsetbox import OffsetImage, AnnotationBbox
from PIL import Image, ImageOps
from skimage.metrics import structural_similarity as ssim
from sklearn.manifold import MDS
import imagehash
import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import numpy as np

from openadapt.db import crud


SHOW_SSIM = False


def calculate_ssim(im1: Image.Image, im2: Image.Image) -> float:
    """Calculate the Structural Similarity Index (SSIM) between two images.

    Args:
        im1 (Image.Image): The first image.
        im2 (Image.Image): The second image.

    Returns:
        float: The SSIM index between the two images.
    """
    # Calculate aspect ratios
    aspect_ratio1 = im1.width / im1.height
    aspect_ratio2 = im2.width / im2.height
    # Use the smaller image as the base for resizing to maintain the aspect ratio
    if aspect_ratio1 < aspect_ratio2:
        base_width = min(im1.width, im2.width)
        base_height = int(base_width / aspect_ratio1)
    else:
        base_height = min(im1.height, im2.height)
        base_width = int(base_height * aspect_ratio2)

    # Resize images to a common base while maintaining aspect ratio
    im1 = im1.resize((base_width, base_height), Image.LANCZOS)
    im2 = im2.resize((base_width, base_height), Image.LANCZOS)

    # Convert images to grayscale
    im1_gray = np.array(im1.convert("L"))
    im2_gray = np.array(im2.convert("L"))

    mssim, grad, S = ssim(
        im1_gray,
        im2_gray,
        data_range=im2_gray.max() - im2_gray.min(),
        gradient=True,
        full=True,
    )

    if SHOW_SSIM:
        # Normalize the gradient for visualization
        grad_normalized = (grad - grad.min()) / (grad.max() - grad.min())
        im_grad = Image.fromarray((grad_normalized * 255).astype(np.uint8))

        # Convert full SSIM image to uint8
        im_S = Image.fromarray((S * 255).astype(np.uint8))

        # Create a figure to display the images
        fig, axs = plt.subplots(1, 4, figsize=(20, 5))  # 1 row, 4 columns

        # Display each image in the subplot
        axs[0].imshow(im1, cmap="gray")
        axs[0].set_title("Image 1")
        axs[0].axis("off")

        axs[1].imshow(im2, cmap="gray")
        axs[1].set_title("Image 2")
        axs[1].axis("off")

        axs[2].imshow(im_grad, cmap="gray")
        axs[2].set_title("Gradient of SSIM")
        axs[2].axis("off")

        axs[3].imshow(im_S, cmap="gray")
        axs[3].set_title("SSIM Image")
        axs[3].axis("off")

        plt.show(block=False)

    return 1 - mssim


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
    """Return number of pixels differing by at least a dynamically calculated threshold.

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
    size: tuple[int, int] = (128, 128),
    border: int = 2,
    color: str = "red",
) -> Image.Image:
    """Resize an image to a common size, add a border to it.

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
    hash_func: Callable,
) -> None:
    """Plot images on a scatter plot based on the provided distance matrix.

    Args:
        images (list[Image.Image]): list of images to plot.
        distance_matrix (np.ndarray): A distance matrix of image differences.
        title (str): Title of the plot.
        hash_func (Callable): The hashing function to compute hash values.

    Returns:
        None
    """
    # Prepare images by resizing and adding a border
    prepared_images = [prepare_image(img) for img in images]

    # Compute hash values for each image
    hash_values = [str(hash_func(img)) if hash_func else "" for img in images]

    # Initialize MDS and fit the distance matrix to get the 2D embedding
    mds = MDS(n_components=2, dissimilarity="precomputed", random_state=0)
    positions = mds.fit_transform(distance_matrix)

    # Create a scatter plot with the MDS results
    fig, ax = plt.subplots(figsize=(15, 10))
    ax.scatter(positions[:, 0], positions[:, 1], alpha=0)

    # Define properties for the bounding box
    bbox_props = dict(boxstyle="round,pad=0.3", ec="b", lw=2, fc="white", alpha=0.7)

    # Loop through images, positions, and hash values to create annotations
    for img, hash_val, (x, y) in zip(prepared_images, hash_values, positions):
        im = OffsetImage(np.array(img), zoom=0.5)
        ab = AnnotationBbox(
            im,
            (x, y),
            xycoords="data",
            frameon=True,
            bboxprops=bbox_props,
        )
        ax.add_artist(ab)
        # Display the hash value beside the image
        ax.text(x, y - 0.05, hash_val, fontsize=9, ha="center")

    # Remove the x and y ticks
    ax.set_xticks([])
    ax.set_yticks([])

    plt.title(title)
    plt.show()


def display_distance_matrix_with_images(
    distance_matrix: np.ndarray,
    images: list[Image.Image],
    func_name: str,
    thumbnail_size: tuple[int, int] = (32, 32),
) -> None:
    """Display the distance matrix as an image with thumbnails along the top and left.

    Args:
        distance_matrix (np.ndarray): A square matrix with distance values.
        images (list[Image.Image]): list of images corresponding to matrix rows/cols.
        thumbnail_size (tuple[int, int]): Size to which thumbnails will be resized.

    Returns:
        None
    """
    # Number of images
    n = len(images)
    # Create a figure with subplots
    fig = plt.figure(figsize=(10, 10))
    # GridSpec layout for the thumbnails and the distance matrix
    gs = gridspec.GridSpec(n + 1, n + 1, figure=fig)

    # Place the distance matrix
    ax_matrix = fig.add_subplot(gs[1:, 1:])
    ax_matrix.imshow(distance_matrix, cmap="viridis")
    ax_matrix.set_xticks([])
    ax_matrix.set_yticks([])

    # Annotate each cell with the distance value
    for (i, j), val in np.ndenumerate(distance_matrix):
        ax_matrix.text(j, i, f"{val:.4f}", ha="center", va="center", color="white")

    # Resize images to thumbnails
    thumbnails = [img.resize(thumbnail_size, Image.ANTIALIAS) for img in images]

    # Plot images on the top row
    for i, img in enumerate(thumbnails):
        ax_img_top = fig.add_subplot(gs[0, i + 1])
        ax_img_top.imshow(np.array(img))
        ax_img_top.axis("off")  # Hide axes

    # Plot images on the left column
    for i, img in enumerate(thumbnails):
        ax_img_left = fig.add_subplot(gs[i + 1, 0])
        ax_img_left.imshow(np.array(img))
        ax_img_left.axis("off")  # Hide axes

    plt.suptitle(func_name)
    plt.show()


def main() -> None:
    """Main function to process images and display similarity metrics."""
    recording = crud.get_latest_recording()
    action_events = recording.processed_action_events
    images = [action_event.screenshot.cropped_image for action_event in action_events]

    similarity_funcs = {
        "ssim": calculate_ssim,
        "thresholded_difference": thresholded_difference,
        "average_hash": lambda im1, im2: (
            imagehash.average_hash(im1) - imagehash.average_hash(im2)
        ),
        "dhash": lambda im1, im2: (imagehash.dhash(im1) - imagehash.dhash(im2)),
        "phash": lambda im1, im2: (imagehash.phash(im1) - imagehash.phash(im2)),
        "crop_resistant_hash": lambda im1, im2: (
            imagehash.crop_resistant_hash(im1) - imagehash.crop_resistant_hash(im2)
        ),
        "colorhash": lambda im1, im2: (
            imagehash.colorhash(im1) - imagehash.colorhash(im2)
        ),
        "whash": lambda im1, im2: imagehash.whash(im1) - imagehash.whash(im2),
    }

    # Process each similarity function
    for func_name, func in similarity_funcs.items():
        hash_func = {
            "average_hash": imagehash.average_hash,
            "dhash": imagehash.dhash,
            "phash": imagehash.phash,
            "crop_resistant_hash": imagehash.crop_resistant_hash,
            "colorhash": imagehash.colorhash,
            "whash": imagehash.whash,
        }.get(func_name, None)

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
        print(f"{func_name=}")
        print(f"distance_matrix=\n{distance_matrix}")
        print(f"{mean_duration=}")
        display_distance_matrix_with_images(distance_matrix, images, func_name)
        plot_images_with_mds(
            images,
            distance_matrix,
            f"Image layout based on {func_name} ({mean_duration=:.4f}s)",
            hash_func,
        )


if __name__ == "__main__":
    main()
