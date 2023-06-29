"""
Use named entity recognition to extract entities from text description (e.g. Spacy)
Download SVGs from https://thenounproject.com/
Ask large language model to arrange
Generate combined SVG
Run through e.g. Stable Diffusion and/or ask large language model to generate animation code
"""

import os

from dotenv import load_dotenv
from loguru import logger

load_dotenv()


def main():
    key = os.environ["NOUN_PROJECT_KEY"]
    secret = os.environ["NOUN_PROJECT_SECRET"]
    logger.info(f"{key=} {secret=}")


if __name__ == "__main__":
    main()
