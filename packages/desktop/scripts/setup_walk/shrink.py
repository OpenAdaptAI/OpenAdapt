"""Shrink the walk's screenshots for the repository: 256-colour PNG, same pixels size."""

import sys
from pathlib import Path

from PIL import Image

for folder in sys.argv[1:]:
    for path in sorted(Path(folder).glob("*.png")):
        with Image.open(path) as image:
            small = image.convert("RGB").quantize(
                colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
            )
            small.save(path, optimize=True)
