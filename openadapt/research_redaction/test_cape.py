import os

import requests

from openadapt import config

resp = requests.post(
    "https://api.capeprivacy.com/v1/privacy/deidentify/text",
    headers={"Authorization": f"Bearer {config.CAPE_API_KEY}"},
    json={
        "content": (
            "There were three bids for the painting: David bid 30.000€, Larissa bid"
            " 35.000€, and Mark bid 37.000€."
        )
    },
)

print(resp.json())
