#!/usr/bin/env python3
"""Generate placeholder icons for OpenAdapt Tray."""

from pathlib import Path
from PIL import Image, ImageDraw

# Icon definitions: (name, color)
ICONS = [
    ("idle", "#4A90D9"),       # Blue
    ("recording", "#D0021B"),  # Red
    ("training", "#7B68EE"),   # Purple
    ("error", "#D0021B"),      # Red
]

# Output directory
OUTPUT_DIR = Path(__file__).parent.parent / "assets" / "icons"


def create_icon(name: str, color: str, size: int = 64) -> Image.Image:
    """Create a simple circular icon.

    Args:
        name: Icon name (unused but useful for logging).
        color: Hex color string.
        size: Icon size in pixels.

    Returns:
        PIL Image.
    """
    # Create transparent image
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Parse hex color
    if color.startswith("#"):
        color = color[1:]
    r = int(color[0:2], 16)
    g = int(color[2:4], 16)
    b = int(color[4:6], 16)

    # Draw filled circle with slight padding
    padding = size // 8
    draw.ellipse(
        [padding, padding, size - padding, size - padding],
        fill=(r, g, b, 255),
    )

    return image


def create_ico_file(images: list, output_path: Path) -> None:
    """Create a Windows .ico file with multiple sizes.

    Args:
        images: List of PIL Images at different sizes.
        output_path: Path to save the .ico file.
    """
    # ico format expects the images in the list
    images[0].save(
        output_path,
        format="ICO",
        sizes=[(img.width, img.height) for img in images],
        append_images=images[1:] if len(images) > 1 else None,
    )


def main():
    """Generate all placeholder icons."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Generating icons in {OUTPUT_DIR}")

    for name, color in ICONS:
        # Generate standard size (64x64)
        icon = create_icon(name, color, 64)
        icon.save(OUTPUT_DIR / f"{name}.png")
        print(f"  Created {name}.png")

        # Generate retina size (128x128 for @2x)
        icon_2x = create_icon(name, color, 128)
        icon_2x.save(OUTPUT_DIR / f"{name}@2x.png")
        print(f"  Created {name}@2x.png")

    # Generate Windows .ico file (using idle icon)
    ico_sizes = [16, 32, 48, 64, 128, 256]
    ico_images = [create_icon("idle", "#4A90D9", size) for size in ico_sizes]

    ico_path = OUTPUT_DIR.parent / "logo.ico"
    create_ico_file(ico_images, ico_path)
    print(f"  Created logo.ico")

    print("Done!")


if __name__ == "__main__":
    main()
