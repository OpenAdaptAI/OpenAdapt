import importlib.metadata
import subprocess
import sys

from nicegui import ui
from packaging import version
import requests


def main() -> None:
    api_url = "https://api.github.com/repos/OpenAdaptAI/OpenAdapt/releases"

    response = requests.get(f"{api_url}?per_page=30&page=1")
    releases = response.json()
    if not releases:
        print("No releases found")
        sys.exit(1)

    latest_release = releases[0]["name"]
    curr_version = importlib.metadata.version("openadapt")

    if version.parse(curr_version) < version.parse(latest_release):
        try:
            if sys.platform.startswith("win"):
                subprocess.call("powershell -File scripts/update_app.ps1", shell=True)
            else:
                subprocess.check_call(["./scripts/update_app.sh"])

            ui.notify("OpenAdapt has been updated")
        except subprocess.CalledProcessError as e:
            print(f"Error updating the app: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
