#!/usr/bin/env python3
"""
Create placeholder icons for the OpenAdapt Chrome extension.
Run this script to generate icon16.png, icon48.png, and icon128.png.
"""

import base64
import os

# Directory where this script is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Minimal valid PNG data (blue squares)
# These are placeholder icons - replace with actual branding later

# 16x16 PNG (blue)
ICON_16_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAP0lEQVQ4T2NkYGD4z8DA"
    "wMhACGZkYGBgZGRgYCQEMzIyMDAy/P/PyMDISAwGGUCMZpg2RkZGBmI0w7QxMjICADhQ"
    "A/9VqTZjAAAAAElFTkSuQmCC"
)

# 48x48 PNG (blue)
ICON_48_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAYAAABXAvmHAAAAS0lEQVRoge3PMQoAIAwE"
    "0dz/0lbBRhBBweJ3bJIsAAAAAAAAAAAAAAAAAAAAAAAAAIC/dZoD1tmBmZmZmZmZmZmZ"
    "mZmZmZmZmZn5awfwmAP/FhtDTwAAAABJRU5ErkJggg=="
)

# 128x128 PNG (blue)
ICON_128_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAYAAADDPmHLAAAAYklEQVR4nO3BAQEAAACC"
    "IP+vbkhAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADAB4wAAQE6AAAAAABJRU5ErkJg"
    "gg=="
)


def main():
    icons = [
        ("icon16.png", ICON_16_B64),
        ("icon48.png", ICON_48_B64),
        ("icon128.png", ICON_128_B64),
    ]

    for filename, b64_data in icons:
        filepath = os.path.join(SCRIPT_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(base64.b64decode(b64_data))
        print(f"Created {filepath}")


if __name__ == "__main__":
    main()
