"""Plot downloads over time."""

import requests
import matplotlib.pyplot as plt
from datetime import datetime
from pprint import pformat

import numpy as np


def fetch_download_data(api_url: str) -> dict:
    """Fetches download data from GitHub API and returns it as a dictionary.

    Supports pagination.

    Args:
        api_url (str): The URL of the GitHub API endpoint for releases.

    Returns:
        dict: A dictionary with dates as keys and download counts as values.
    """
    download_data = {}
    ignored_names = set()
    page = 1
    while True:
        response = requests.get(f"{api_url}?per_page=30&page={page}")
        releases = response.json()
        if not releases:  # Break the loop if no more releases are returned
            break
        for release in releases:
            release_date = release["published_at"][:10]
            print(
                release["name"],
                list(
                    (asset["name"], asset["download_count"])
                    for asset in release["assets"]
                ),
            )
            ignored_names |= set([
                asset["name"] for asset in release["assets"]
                if not asset["name"].endswith(".zip")
            ])
            total_downloads = sum(
                asset["download_count"] for asset in release["assets"]
                if asset["name"].endswith(".zip")
            )
            download_data[release_date] = total_downloads
        page += 1  # Increment page number for the next API request
    print(f"ignored_names=\n{pformat(ignored_names)}")
    return download_data


def plot_downloads(data: dict) -> None:
    """Plots number of downloads and cumulative downloads over time using matplotlib.

    Includes total cumulative in the title.

    Args:
        data (dict): A dictionary with dates as keys and download counts as values.
    """
    dates = [datetime.strptime(date, "%Y-%m-%d") for date in sorted(data.keys())]
    downloads = [data[date.strftime("%Y-%m-%d")] for date in dates]
    cumulative_downloads = np.cumsum(downloads)
    total_cumulative_downloads = (
        cumulative_downloads[-1] if cumulative_downloads.size > 0 else 0
    )

    plt.figure(figsize=(12, 6))
    plt.plot(
        dates, downloads, marker="o", linestyle="-", color="b", label="Daily Downloads"
    )
    plt.plot(
        dates,
        cumulative_downloads,
        marker=None,
        linestyle="--",
        color="r",
        label="Cumulative Downloads",
    )
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    plt.title(
        "Downloads Over Time"
        f"\n(Total Cumulative: {total_cumulative_downloads}) "
        f"\n{current_time}"
    )
    plt.xlabel("Date")
    plt.ylabel("Number of Downloads")
    plt.grid(True)
    plt.xticks(rotation=45)
    plt.legend()
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    api_url = "https://api.github.com/repos/OpenAdaptAI/OpenAdapt/releases"
    download_data = fetch_download_data(api_url)
    plot_downloads(download_data)
