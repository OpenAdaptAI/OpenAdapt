"""Video vs image frame comparison utilities.

Compare extracted video frames against original captured images to verify
frame extraction accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from PIL import Image


@dataclass
class FrameComparison:
    """Result of comparing an extracted frame to the original image."""

    timestamp: float
    mean_diff: float  # Mean absolute pixel difference (0-255 scale)
    max_diff: float  # Maximum absolute pixel difference
    psnr: float  # Peak Signal-to-Noise Ratio (higher = more similar)
    ssim: float | None = None  # Structural Similarity Index (optional)
    diff_image: "Image.Image | None" = None  # Visualization of differences


@dataclass
class ComparisonReport:
    """Summary of frame comparison results."""

    comparisons: list[FrameComparison] = field(default_factory=list)
    video_path: Path | None = None

    @property
    def mean_diff_overall(self) -> float:
        """Overall mean pixel difference across all frames."""
        if not self.comparisons:
            return 0.0
        return sum(c.mean_diff for c in self.comparisons) / len(self.comparisons)

    @property
    def max_diff_overall(self) -> float:
        """Maximum pixel difference across all frames."""
        if not self.comparisons:
            return 0.0
        return max(c.max_diff for c in self.comparisons)

    @property
    def mean_psnr(self) -> float:
        """Mean PSNR across all frames."""
        if not self.comparisons:
            return 0.0
        return sum(c.psnr for c in self.comparisons) / len(self.comparisons)

    @property
    def is_lossless(self) -> bool:
        """Check if extraction appears lossless (very small differences)."""
        # Allow for minimal codec artifacts (< 1 on 0-255 scale)
        return self.mean_diff_overall < 1.0

    def summary(self) -> dict:
        """Get summary statistics."""
        return {
            "num_frames": len(self.comparisons),
            "mean_diff": self.mean_diff_overall,
            "max_diff": self.max_diff_overall,
            "mean_psnr": self.mean_psnr,
            "is_lossless": self.is_lossless,
        }


def compute_psnr(img1: np.ndarray, img2: np.ndarray) -> float:
    """Compute Peak Signal-to-Noise Ratio between two images.

    Args:
        img1: First image as numpy array.
        img2: Second image as numpy array.

    Returns:
        PSNR value in dB. Higher = more similar. Infinity = identical.
    """
    mse = np.mean((img1.astype(np.float64) - img2.astype(np.float64)) ** 2)
    if mse == 0:
        return float("inf")
    max_pixel = 255.0
    return 20 * np.log10(max_pixel / np.sqrt(mse))


def compare_frames(
    original: "Image.Image",
    extracted: "Image.Image",
    timestamp: float,
    compute_diff_image: bool = False,
) -> FrameComparison:
    """Compare an original image to an extracted video frame.

    Args:
        original: Original captured image.
        extracted: Frame extracted from video.
        timestamp: Timestamp of the frame.
        compute_diff_image: Whether to generate a difference visualization.

    Returns:
        FrameComparison with similarity metrics.
    """
    from PIL import Image

    # Ensure same size
    if original.size != extracted.size:
        extracted = extracted.resize(original.size, Image.Resampling.LANCZOS)

    # Convert to RGB if needed
    if original.mode != "RGB":
        original = original.convert("RGB")
    if extracted.mode != "RGB":
        extracted = extracted.convert("RGB")

    # Convert to numpy arrays
    orig_arr = np.array(original, dtype=np.float64)
    extr_arr = np.array(extracted, dtype=np.float64)

    # Compute differences
    diff = np.abs(orig_arr - extr_arr)
    mean_diff = float(np.mean(diff))
    max_diff = float(np.max(diff))
    psnr = compute_psnr(np.array(original), np.array(extracted))

    # Generate diff image if requested
    diff_image = None
    if compute_diff_image:
        # Amplify differences for visibility (scale to 0-255)
        diff_scaled = np.clip(diff * 10, 0, 255).astype(np.uint8)
        diff_image = Image.fromarray(diff_scaled)

    return FrameComparison(
        timestamp=timestamp,
        mean_diff=mean_diff,
        max_diff=max_diff,
        psnr=psnr,
        diff_image=diff_image,
    )


def compare_video_to_images(
    video_path: str | Path,
    images: list[tuple[float, "Image.Image"]],
    tolerance: float = 0.1,
    compute_diff_images: bool = False,
) -> ComparisonReport:
    """Compare extracted video frames to original images.

    Args:
        video_path: Path to the video file.
        images: List of (timestamp, image) tuples.
        tolerance: Tolerance for frame extraction timestamp matching.
        compute_diff_images: Whether to generate difference visualizations.

    Returns:
        ComparisonReport with frame-by-frame comparisons.
    """
    from openadapt_capture.video import extract_frames

    video_path = Path(video_path)

    # Extract timestamps
    timestamps = [ts for ts, _ in images]

    # Extract frames from video
    try:
        extracted_frames = extract_frames(video_path, timestamps, tolerance=tolerance)
    except ValueError as e:
        # Some frames couldn't be extracted
        raise ValueError(f"Frame extraction failed: {e}")

    # Compare each pair
    report = ComparisonReport(video_path=video_path)
    for (timestamp, original), extracted in zip(images, extracted_frames):
        comparison = compare_frames(
            original=original,
            extracted=extracted,
            timestamp=timestamp,
            compute_diff_image=compute_diff_images,
        )
        report.comparisons.append(comparison)

    return report


def plot_comparison(
    report: ComparisonReport,
    output_path: str | Path | None = None,
    show: bool = False,
) -> "Image.Image | None":
    """Plot comparison metrics over time.

    Args:
        report: ComparisonReport to visualize.
        output_path: Path to save the plot image.
        show: Whether to display interactively.

    Returns:
        PIL Image if neither output_path nor show, else None.
    """
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        raise ImportError(
            "matplotlib is required for plotting. Install with: uv add matplotlib"
        )

    if not report.comparisons:
        return None

    timestamps = [c.timestamp for c in report.comparisons]
    mean_diffs = [c.mean_diff for c in report.comparisons]
    max_diffs = [c.max_diff for c in report.comparisons]
    psnrs = [c.psnr for c in report.comparisons]

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

    # Plot differences
    ax1 = axes[0]
    ax1.plot(timestamps, mean_diffs, "b-", label="Mean Diff", alpha=0.7)
    ax1.plot(timestamps, max_diffs, "r-", label="Max Diff", alpha=0.7)
    ax1.set_ylabel("Pixel Difference (0-255)")
    ax1.set_title("Video vs Image Frame Comparison")
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    # Plot PSNR
    ax2 = axes[1]
    # Filter out infinite PSNR for plotting
    finite_psnrs = [(t, p) for t, p in zip(timestamps, psnrs) if p != float("inf")]
    if finite_psnrs:
        ts, ps = zip(*finite_psnrs)
        ax2.plot(ts, ps, "g-", label="PSNR (dB)", alpha=0.7)
    ax2.set_xlabel("Timestamp (s)")
    ax2.set_ylabel("PSNR (dB)")
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    # Add summary text
    summary = report.summary()
    summary_text = (
        f"Frames: {summary['num_frames']} | "
        f"Mean Diff: {summary['mean_diff']:.2f} | "
        f"Max Diff: {summary['max_diff']:.2f} | "
        f"Mean PSNR: {summary['mean_psnr']:.1f} dB | "
        f"Lossless: {summary['is_lossless']}"
    )
    fig.suptitle(summary_text, fontsize=10)

    plt.tight_layout()

    if output_path:
        plt.savefig(output_path, dpi=150, bbox_inches="tight")
        plt.close()
        return None
    elif show:
        plt.show()
        plt.close()
        return None
    else:
        from io import BytesIO

        from PIL import Image

        buf = BytesIO()
        plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
        plt.close()
        buf.seek(0)
        return Image.open(buf)
