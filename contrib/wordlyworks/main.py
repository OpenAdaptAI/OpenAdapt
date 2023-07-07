"""
Use named entity recognition to extract entities from text description (e.g. Spacy)
Download SVGs from https://thenounproject.com/
Ask large language model to arrange
Generate combined SVG
Run through e.g. Stable Diffusion and/or ask large language model to generate animation code
"""

from pprint import pformat
from requests_oauthlib import OAuth1
import os
import requests
from contrib.wordlyworks import ner.ner

from dotenv import load_dotenv
from loguru import logger
import ner
#from TheNounProjectAPI import API


load_dotenv()


def main():
    key = os.environ["NOUN_PROJECT_KEY"]
    secret = os.environ["NOUN_PROJECT_SECRET"]
    logger.info(f"{key=} {secret=}")


    auth = OAuth1(key, secret)
    endpoint = "https://api.thenounproject.com/v2/icon/1"

    response = requests.get(endpoint, auth=auth)
    logger.info(f"response=\n{pformat(response.json())}")

    if 0:
        api = API(key=key, secret=secret)
        icons = api.get_icons_by_term("goat", public_domain_only=True, limit=2)
        for icon in icons:
            print("Icon's term:", icon.term)
            print("This icon's tags:", ", ".join(tag.slug for tag in icon.tags))
            print("Uploader's username:", icon.uploader.username)


if __name__ == "__main__":
    main()
