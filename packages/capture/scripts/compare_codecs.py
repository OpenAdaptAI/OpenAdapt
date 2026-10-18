#!/usr/bin/env python3
"""Compare H.264 vs H.265 video codecs for capture.

This script compares:
1. File sizes between H.264 (libx264) and H.265 (libx265)
2. Frame extraction accuracy (PSNR, mean diff)
3. Encoding/decoding performance

Usage:
    uv run python scripts/compare_codecs.py --duration 5

Note: Requires libx265 to be available in your ffmpeg/av installation.
"""

import argparse
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image


@dataclass
class CodecResult:
    """Results from testing a codec."""
    codec: str
    file_size_bytes: int
    encode_time_seconds: float
    decode_time_seconds: float
    mean_diff: float
    max_diff: float
    psnr: float
    num_frames: int


def generate_test_frames(num_frames: int, width: int = 1920, height: int = 1080) -> list[Image.Image]:
    """Generate test frames with realistic content variation.

    Creates frames that simulate screen capture with:
    - Text-like patterns
    - Gradients
    - Sharp edges
    - Some temporal consistency (slight changes between frames)
    """
    print(f"Generating {num_frames} test frames ({width}x{height})...")
    frames = []

    np.random.seed(42)  # Reproducible

    # Base pattern (simulates a desktop background)
    base = np.zeros((height, width, 3), dtype=np.uint8)

    # Add gradient background
    for y in range(height):
        base[y, :, 0] = int(30 + 20 * (y / height))  # Dark blue gradient
        base[y, :, 1] = int(40 + 30 * (y / height))
        base[y, :, 2] = int(60 + 40 * (y / height))

    # Add some "window" rectangles
    windows = [
        (100, 100, 800, 600, (40, 44, 52)),    # Dark window
        (850, 150, 1000, 500, (255, 255, 255)), # Light window
        (200, 650, 600, 350, (30, 30, 30)),     # Terminal-like
    ]

    for x, y, w, h, color in windows:
        base[y:y+h, x:x+w] = color

    for i in range(num_frames):
        frame = base.copy()

        # Add cursor movement (small red square)
        cursor_x = 100 + int(400 * np.sin(i * 0.1))
        cursor_y = 300 + int(200 * np.cos(i * 0.15))
        frame[cursor_y:cursor_y+20, cursor_x:cursor_x+15] = (255, 0, 0)

        # Add some "typing" in the terminal area (random noise to simulate text)
        text_y = 680 + (i % 10) * 20
        if text_y < 980:
            noise = np.random.randint(150, 255, (15, 300, 3), dtype=np.uint8)
            frame[text_y:text_y+15, 220:520] = noise

        # Add timestamp-like text area
        frame[50:70, 1700:1900] = np.random.randint(200, 255, (20, 200, 3), dtype=np.uint8)

        frames.append(Image.fromarray(frame))

        if (i + 1) % 50 == 0:
            print(f"  Generated {i + 1}/{num_frames} frames")

    return frames


def test_codec(
    frames: list[Image.Image],
    codec: str,
    output_dir: Path,
    crf: int = 0,
    pix_fmt: str = "yuv444p",
) -> CodecResult:
    """Test a codec and return results.

    Args:
        frames: List of PIL Images to encode.
        codec: Codec name (libx264 or libx265).
        output_dir: Directory to write video file.
        crf: Constant Rate Factor (0 = lossless).
        pix_fmt: Pixel format.

    Returns:
        CodecResult with metrics.
    """
    from openadapt_capture.comparison import compute_psnr
    from openadapt_capture.video import VideoWriter, extract_frames

    video_path = output_dir / f"test_{codec}.mp4"
    width, height = frames[0].size

    print(f"\nTesting {codec}...")
    print(f"  Settings: CRF={crf}, pix_fmt={pix_fmt}")

    # Encode
    print(f"  Encoding {len(frames)} frames...")
    encode_start = time.time()

    writer = VideoWriter(
        video_path,
        width=width,
        height=height,
        fps=24,
        codec=codec,
        pix_fmt=pix_fmt,
        crf=crf,
        preset="medium",  # Faster for testing
    )

    for i, frame in enumerate(frames):
        timestamp = i / 24.0  # 24 fps
        writer.write_frame(frame, timestamp)

    writer.close()
    encode_time = time.time() - encode_start

    # Get file size
    file_size = video_path.stat().st_size
    print(f"  File size: {file_size / 1024 / 1024:.2f} MB")
    print(f"  Encode time: {encode_time:.2f}s")

    # Decode and compare
    print("  Extracting frames for comparison...")
    decode_start = time.time()

    # Extract frames at same timestamps
    timestamps = [i / 24.0 for i in range(len(frames))]

    try:
        extracted = extract_frames(video_path, timestamps, tolerance=0.1)
    except ValueError as e:
        print(f"  Warning: Could not extract all frames: {e}")
        # Try with larger tolerance
        extracted = extract_frames(video_path, timestamps[:len(frames)//2], tolerance=0.5)
        frames = frames[:len(extracted)]

    decode_time = time.time() - decode_start
    print(f"  Decode time: {decode_time:.2f}s")

    # Compare frames
    print("  Computing accuracy metrics...")
    diffs = []
    max_diffs = []
    psnrs = []

    for orig, extr in zip(frames, extracted):
        # Ensure same size
        if orig.size != extr.size:
            extr = extr.resize(orig.size, Image.Resampling.LANCZOS)

        # Convert to arrays
        orig_arr = np.array(orig.convert("RGB"), dtype=np.float64)
        extr_arr = np.array(extr.convert("RGB"), dtype=np.float64)

        diff = np.abs(orig_arr - extr_arr)
        diffs.append(np.mean(diff))
        max_diffs.append(np.max(diff))
        psnrs.append(compute_psnr(np.array(orig.convert("RGB")), np.array(extr.convert("RGB"))))

    mean_diff = np.mean(diffs)
    max_diff = np.max(max_diffs)
    mean_psnr = np.mean([p for p in psnrs if p != float("inf")])

    print(f"  Mean pixel diff: {mean_diff:.4f}")
    print(f"  Max pixel diff: {max_diff:.4f}")
    print(f"  Mean PSNR: {mean_psnr:.2f} dB")

    return CodecResult(
        codec=codec,
        file_size_bytes=file_size,
        encode_time_seconds=encode_time,
        decode_time_seconds=decode_time,
        mean_diff=mean_diff,
        max_diff=max_diff,
        psnr=mean_psnr,
        num_frames=len(frames),
    )


def print_comparison(results: list[CodecResult]) -> None:
    """Print comparison table."""
    print("\n" + "=" * 70)
    print("CODEC COMPARISON RESULTS")
    print("=" * 70)

    # Header
    print(f"{'Codec':<12} {'Size (MB)':<12} {'Encode (s)':<12} {'Decode (s)':<12} {'Mean Diff':<12} {'PSNR (dB)':<12}")
    print("-" * 70)

    for r in results:
        size_mb = r.file_size_bytes / 1024 / 1024
        print(f"{r.codec:<12} {size_mb:<12.2f} {r.encode_time_seconds:<12.2f} {r.decode_time_seconds:<12.2f} {r.mean_diff:<12.4f} {r.psnr:<12.2f}")

    print("-" * 70)

    # Comparison
    if len(results) >= 2:
        h264 = next((r for r in results if "264" in r.codec), None)
        h265 = next((r for r in results if "265" in r.codec), None)

        if h264 and h265:
            size_reduction = (1 - h265.file_size_bytes / h264.file_size_bytes) * 100
            print("\nH.265 vs H.264:")
            print(f"  Size reduction: {size_reduction:.1f}%")
            print(f"  Quality difference (PSNR): {h265.psnr - h264.psnr:+.2f} dB")
            print(f"  Encode time ratio: {h265.encode_time_seconds / h264.encode_time_seconds:.2f}x")


def generate_comparison_plot(results: list[CodecResult], output_path: Path) -> None:
    """Generate comparison plot."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available, skipping plot generation")
        return

    fig, axes = plt.subplots(1, 3, figsize=(15, 5))

    codecs = [r.codec for r in results]

    # File size
    ax1 = axes[0]
    sizes = [r.file_size_bytes / 1024 / 1024 for r in results]
    bars1 = ax1.bar(codecs, sizes, color=['#3498db', '#e74c3c'])
    ax1.set_ylabel("File Size (MB)")
    ax1.set_title("File Size Comparison")
    for bar, size in zip(bars1, sizes):
        ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.1,
                 f'{size:.1f}', ha='center', va='bottom')

    # Encoding time
    ax2 = axes[1]
    times = [r.encode_time_seconds for r in results]
    bars2 = ax2.bar(codecs, times, color=['#3498db', '#e74c3c'])
    ax2.set_ylabel("Encode Time (s)")
    ax2.set_title("Encoding Speed")
    for bar, t in zip(bars2, times):
        ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.1,
                 f'{t:.1f}s', ha='center', va='bottom')

    # Quality (PSNR)
    ax3 = axes[2]
    psnrs = [r.psnr for r in results]
    bars3 = ax3.bar(codecs, psnrs, color=['#3498db', '#e74c3c'])
    ax3.set_ylabel("PSNR (dB)")
    ax3.set_title("Quality (higher = better)")
    ax3.set_ylim(min(psnrs) - 5, max(psnrs) + 5)
    for bar, p in zip(bars3, psnrs):
        ax3.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
                 f'{p:.1f}', ha='center', va='bottom')

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\nComparison plot saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Compare H.264 vs H.265 codecs")
    parser.add_argument(
        "--num-frames", "-n",
        type=int,
        default=100,
        help="Number of test frames (default: 100)",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=0,
        help="CRF value, 0=lossless (default: 0)",
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=str,
        default=None,
        help="Output directory for results",
    )
    parser.add_argument(
        "--keep-videos",
        action="store_true",
        help="Keep generated video files",
    )
    args = parser.parse_args()

    # Setup output directory
    if args.output_dir:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        output_dir = Path(tempfile.mkdtemp(prefix="codec_compare_"))
        cleanup = not args.keep_videos

    print("=" * 70)
    print("H.264 vs H.265 Codec Comparison")
    print("=" * 70)

    try:
        # Generate test frames
        frames = generate_test_frames(args.num_frames)

        # Test both codecs
        results = []

        # H.264
        try:
            result_h264 = test_codec(frames, "libx264", output_dir, crf=args.crf)
            results.append(result_h264)
        except Exception as e:
            print(f"H.264 test failed: {e}")

        # H.265
        try:
            result_h265 = test_codec(frames, "libx265", output_dir, crf=args.crf)
            results.append(result_h265)
        except Exception as e:
            print(f"H.265 test failed: {e}")
            print("Note: libx265 may not be available in your ffmpeg installation")

        if results:
            print_comparison(results)

            # Generate plot
            plot_path = Path(__file__).parent.parent / "docs" / "images" / "codec_comparison.png"
            plot_path.parent.mkdir(parents=True, exist_ok=True)
            generate_comparison_plot(results, plot_path)

    finally:
        if cleanup:
            print(f"\nCleaning up {output_dir}...")
            shutil.rmtree(output_dir, ignore_errors=True)
        else:
            print(f"\nVideo files saved in: {output_dir}")


if __name__ == "__main__":
    main()
