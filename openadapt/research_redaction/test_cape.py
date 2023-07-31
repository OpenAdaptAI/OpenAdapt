import os

import requests

from openadapt import config

with open("./files/sample_llc_2.pdf", "rb") as f:
    resp = requests.post(
        "https://api.capeprivacy.com/v1/privacy/deidentify/file",
        headers={"Authorization": f"Bearer {config.CAPE_API_KEY}"},
        files={"file": f},
    )

print(resp.json())

# import os

# import requests

# from openadapt import config

# with open("./files/sample_emr_1.png", "rb") as f:
#     resp = requests.post(
#         "https://api.capeprivacy.com/v1/privacy/deidentify/file",
#         headers={"Authorization": f"Bearer {config.CAPE_API_KEY}"},
#         files={"file": f.read()},
#     )

# print(resp.json())
