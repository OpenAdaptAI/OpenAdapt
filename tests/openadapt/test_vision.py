"""Tests for vision.py"""


import pytest
from PIL import Image
import numpy as np

from openadapt import vision


@pytest.fixture
def synthetic_images():
    """Generate synthetic images for testing."""
    base_data = np.random.rand(100, 100) * 255  # Base image array
    images = []
    
    # Create images that are identical
    for _ in range(3):
        img = Image.fromarray((base_data).astype(np.uint8))
        images.append(img)
    
    # Create images that are slightly different
    for i in range(3):
        perturbed_data = base_data + np.random.rand(100, 100) * 10
        img = Image.fromarray((perturbed_data).astype(np.uint8))
        images.append(img)

    return images


def test_similar_images(synthetic_images):
    """Test that the SSIM threshold correctly identifies similar images."""
    expected_groups = [list(range(3))]  # Expecting only the identical images to form a group
    result_groups, result_matrix = vision.get_similar_image_idxs(synthetic_images, 0.95)
    
    # Sorting sublists and the main list to ensure order in assertions does not matter
    result_sorted = [sorted(group) for group in result_groups]
    result_sorted.sort()
    expected_groups.sort()

    # Check that the grouped indices match the expected groups
    assert result_sorted == expected_groups, (
        f"Expected groups: {expected_groups}, but got: {result_sorted}"
    )

    # Verify symmetry and range of values
    num_images = len(synthetic_images)
    assert all(result_matrix[i][i] == 1.0 for i in range(num_images)), (
        result_matrix, "Diagonal values should be 1.0"
    )
    assert all(
        0 <= result_matrix[i][j] <= 1.0
        for i in range(num_images)
        for j in range(num_images)
    ), (
        result_matrix, "SSIM values should be between 0 and 1"
    )

    # Check that all values in the matrix above the threshold are properly identified
    for i in range(num_images):
        for j in range(i + 1, num_images):
            if result_matrix[i][j] >= 0.95:
                assert any(i in group and j in group for group in result_sorted), (
                    result_sorted, result_matrix,
                    f"Images {i} and {j} should be grouped (SSIM={result_matrix[i][j]})"
                )


if __name__ == "__main__":
    pytest.main()
