"""Get the version of the package."""

import importlib.metadata
import requests
import json

APP_VERSION_URL = "https://api.github.com/repos/OpenAdaptAI/OpenAdapt/releases?per_page=10&page=1"

def get_version() -> str:
    """Get the version of the package."""
    return importlib.metadata.version("openadapt")

def get_latest_version() -> str:
    response = requests.get(APP_VERSION_URL, stream=True)
    data = json.loads(response.text)
    return data[0]['tag_name'].replace('v', '')

if __name__ == "__main__":
    print(get_version())
