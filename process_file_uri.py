# Example script to illustrate how to make API calls to the Private AI Docker
# container to deidentify a text using the unique PII markers feature (default).
#
# To use this script, please start the Docker container locally, as per the
# instructions at https://private-ai.com/docs/installation.
#
# In order to use the API key issued by Private AI, you can run the script as
# `API_KEY=<your key here> python process_file_uri.py` or you can define a
# `.env` file which has the line`API_KEY=<your key here>`.

import logging
import os

from privateai_client import PAIClient
from privateai_client.objects import request_objects

file_dir = "./assets/"
client = PAIClient("http", "localhost", "8080")
for file_name in os.listdir(file_dir):
    filepath = os.path.join(file_dir, file_name)
    if not os.path.isfile(filepath):
        continue
    req_obj = request_objects.file_uri_obj(uri=filepath)
    # NOTE this method of file processing requires the container to have an the input and output directories mounted
    resp = client.process_files_uri(req_obj)
    if not resp.ok:
        logging.error(f"response for file {file_name} returned with {resp.status_code}")
