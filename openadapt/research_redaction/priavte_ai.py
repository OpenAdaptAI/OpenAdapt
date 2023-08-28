import base64
import os

from loguru import logger
from privateai_client import PAIClient
from privateai_client.objects import request_objects

from openadapt import config

file_dir = "files"
file_name = "sample_llc_1.pdf"
filepath = os.path.join(file_dir, file_name)
file_type = "application/pdf"  # eg. application/pdf
client = PAIClient(url="http://localhost:8080")

# Read from file
with open(filepath, "rb") as b64_file:
    file_data = base64.b64encode(b64_file.read())
    file_data = file_data.decode("ascii")

# Make the request
file_obj = request_objects.file_obj(data=file_data, content_type=file_type)
request_obj = request_objects.file_base64_obj(file=file_obj)
resp = client.process_files_base64(request_object=request_obj)
if not resp.ok:
    logger.warning(f"response for file {file_name} returned with {resp.status_code}")

# Write to file
with open(os.path.join(file_dir, f"redacted-{file_name}"), "wb") as redacted_file:
    processed_file = resp.processed_file.encode("ascii")
    processed_file = base64.b64decode(processed_file, validate=True)
    redacted_file.write(processed_file)
