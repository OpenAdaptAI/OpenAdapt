#!/usr/bin/env python3
"""Generate demo images showing before/after PII scrubbing."""

from PIL import Image, ImageDraw, ImageFont
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


def create_pii_screenshot(width: int = 600, height: int = 400) -> Image.Image:
    """Create a synthetic screenshot containing PII."""
    img = Image.new("RGB", (width, height), color=(240, 240, 240))
    draw = ImageDraw.Draw(img)

    # Try to use a nice font, fall back to default
    try:
        font_large = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 24)
        font_medium = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 18)
        font_small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
    except (OSError, IOError):
        font_large = ImageFont.load_default()
        font_medium = font_large
        font_small = font_large

    # Header
    draw.rectangle([0, 0, width, 50], fill=(51, 51, 51))
    draw.text((20, 12), "User Profile - John Smith", fill="white", font=font_large)

    # Form fields with PII
    y = 80

    # Email field
    draw.text((30, y), "Email:", fill="black", font=font_medium)
    draw.rectangle([30, y + 25, width - 30, y + 55], outline="gray", width=1)
    draw.text((40, y + 30), "john.smith@example.com", fill="black", font=font_medium)
    y += 80

    # Phone field
    draw.text((30, y), "Phone:", fill="black", font=font_medium)
    draw.rectangle([30, y + 25, width - 30, y + 55], outline="gray", width=1)
    draw.text((40, y + 30), "555-123-4567", fill="black", font=font_medium)
    y += 80

    # SSN field
    draw.text((30, y), "SSN:", fill="black", font=font_medium)
    draw.rectangle([30, y + 25, width - 30, y + 55], outline="gray", width=1)
    draw.text((40, y + 30), "923-45-6789", fill="black", font=font_medium)
    y += 80

    # Credit card field
    draw.text((30, y), "Card:", fill="black", font=font_medium)
    draw.rectangle([30, y + 25, width - 30, y + 55], outline="gray", width=1)
    draw.text((40, y + 30), "4532-1234-5678-9012", fill="black", font=font_medium)

    return img


def main():
    """Generate demo images."""
    from openadapt_privacy.providers.presidio import PresidioScrubbingProvider

    assets_dir = Path(__file__).parent.parent / "assets"
    assets_dir.mkdir(exist_ok=True)

    # Create original image
    print("Creating original image with PII...")
    original = create_pii_screenshot()
    original_path = assets_dir / "screenshot_original.png"
    original.save(original_path)
    print(f"Saved: {original_path}")

    # Scrub the image
    print("Scrubbing PII from image...")
    scrubber = PresidioScrubbingProvider()
    scrubbed = scrubber.scrub_image(original)
    scrubbed_path = assets_dir / "screenshot_scrubbed.png"
    scrubbed.save(scrubbed_path)
    print(f"Saved: {scrubbed_path}")

    print("\nDone! Images saved to assets/")


if __name__ == "__main__":
    main()
