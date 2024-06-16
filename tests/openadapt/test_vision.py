"""Tests for vision.py."""

import pytest
from PIL import Image
import numpy as np

from openadapt import vision


@pytest.fixture
def identical_images() -> list[Image.Image]:
    """Generate synthetic identical RGB images for testing."""
    base_data = np.random.rand(100, 100, 3) * 255  # Base image array for RGB
    images = [Image.fromarray(base_data.astype(np.uint8), "RGB") for _ in range(3)]
    return images


@pytest.fixture
def perturbed_images(identical_images: list[Image.Image]) -> list[Image.Image]:
    """Generate synthetic RGB images slightly perturbed from the identical ones."""
    perturbed_images = []
    for img in identical_images:
        # Perturb the image data slightly
        perturbed_data = np.array(img) + np.random.rand(100, 100, 3) * 50
        # Ensure values remain in valid range
        perturbed_data = np.clip(perturbed_data, 0, 255)
        # Scale width by +/- 10%
        scale_factor_width = 1 + (np.random.rand() - 0.5) / 5
        # Scale height by +/- 10%
        scale_factor_height = 1 + (np.random.rand() - 0.5) / 5
        new_size = (
            int(img.size[0] * scale_factor_width),
            int(img.size[1] * scale_factor_height),
        )
        perturbed_img = Image.fromarray(perturbed_data.astype(np.uint8), "RGB").resize(
            new_size, Image.LANCZOS
        )
        perturbed_images.append(perturbed_img)
    return perturbed_images


def test_similar_images(
    identical_images: list[Image.Image],
    perturbed_images: list[Image.Image],
) -> None:
    """Test that the SSIM threshold correctly identifies similar images."""
    MIN_SSIM = 0.99
    MIN_SIZE_SIM = 0.9

    images = identical_images + perturbed_images
    # Expecting only the identical images to form a group
    expected_groups = [list(range(len(identical_images)))]
    result_groups, _, result_ssim_matrix, result_size_matrix = (
        vision.get_similar_image_idxs(
            images,
            MIN_SSIM,
            MIN_SIZE_SIM,
            short_circuit_ssim=False,
        )
    )

    # Sorting sublists and the main list to ensure order in assertions does not matter
    result_sorted = [sorted(group) for group in result_groups]
    result_sorted.sort()
    expected_groups.sort()

    # Check that the grouped indices match the expected groups
    assert (
        result_sorted == expected_groups
    ), f"Expected groups: {expected_groups}, but got: {result_sorted}"

    # Verify symmetry and range of values in SSIM and size matrices
    num_images = len(images)
    assert all(
        result_ssim_matrix[i][i] == 1.0 and result_size_matrix[i][i] == 1.0
        for i in range(num_images)
    ), (
        "Diagonal values should be 1.0: "
        f"SSIM Matrix: {result_ssim_matrix}, "
        f"Size Matrix: {result_size_matrix}"
    )
    assert all(
        0 <= result_ssim_matrix[i][j] <= 1.0 and 0 <= result_size_matrix[i][j] <= 1.0
        for i in range(num_images)
        for j in range(num_images)
    ), (
        "SSIM and size values should be between 0 and 1: "
        f"SSIM Matrix: {result_ssim_matrix}, Size Matrix: {result_size_matrix}"
    )

    # Check that all values in the matrix above the threshold are properly identified
    for i in range(num_images):
        for j in range(i + 1, num_images):
            if (
                result_ssim_matrix[i][j] >= MIN_SSIM
                and result_size_matrix[i][j] >= MIN_SIZE_SIM
            ):
                assert any(i in group and j in group for group in result_sorted), (
                    f"Images {i} and {j} should be grouped: "
                    f"SSIM={result_ssim_matrix[i][j]}, Size={result_size_matrix[i][j]})"
                )


def test_size_similarity(
    identical_images: list[Image.Image],
    perturbed_images: list[Image.Image],
) -> None:
    """Test the size similarity function for accuracy."""
    # Identical images should have perfect size similarity
    for i in range(len(identical_images) - 1):
        for j in range(i + 1, len(identical_images)):
            assert (
                vision.get_size_similarity(identical_images[i], identical_images[j])
                == 1.0
            ), "Identical images should have size similarity of 1.0"

    # For perturbed images, ensure the similarity is correctly calculated
    for img1 in identical_images:
        for img2 in perturbed_images:
            size_sim = vision.get_size_similarity(img1, img2)
            assert (
                0 <= size_sim <= 1
            ), f"Size similarity should be between 0 and 1, got {size_sim}"


if __name__ == "__main__":
    pytest.main()
