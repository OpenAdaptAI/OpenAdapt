import base64
import os

import requests

url = "https://api.private-ai.com/deid/v3/process/files/base64"

file_dir = "files/"
file_name = "sample_emr_1.png"
filepath = os.path.join(file_dir, file_name)
file_type = "image/png"

# Read from file
with open(filepath, "rb") as b64_file:
    file_data = base64.b64encode(b64_file.read())
    file_data = file_data.decode("ascii")


payload = {
    "file": {"data": file_data, "content_type": file_type},
    "entity_detection": {"accuracy": "high", "return_entity": True},
    "pdf_options": {"density": 150, "max_resolution": 2000},
    "audio_options": {"bleep_start_padding": 0, "bleep_end_padding": 0},
}

headers = {"Content-Type": "application/json", "X-API-KEY": "MRSLHS7HUX4G"}

response = requests.post(url, json=payload, headers=headers)
response = response.json()

# Write to file
with open(os.path.join(file_dir, f"redacted-{file_name}"), "wb") as redacted_file:
    processed_file = response.get("processed_file").encode("ascii")
    processed_file = base64.b64decode(processed_file, validate=True)
    redacted_file.write(processed_file)
