"""Plot release downloads over time."""

import requests
import matplotlib.pyplot as plt
from datetime import datetime
import numpy as np  # Import numpy for cumulative sum calculation


def fetch_download_data(api_url: str) -> dict:
    """Fetches download data from GitHub API and returns it as a dictionary.

    Args:
        api_url (str): The URL of the GitHub API endpoint for releases.

    Returns:
        dict: A dictionary with dates as keys and download counts as values.
    """
    response = requests.get(api_url)
    releases = response.json()

    download_data = {}
    for release in releases:
        release_date = release["published_at"][:10]
        total_downloads = sum(asset["download_count"] for asset in release["assets"])
        download_data[release_date] = total_downloads

    return download_data


def plot_downloads(data: dict) -> None:
    """Plots number of downloads and cumulative downloads over time using matplotlib.

    Args:
        data (dict): A dictionary with dates as keys and download counts as values.
    """
    dates = [datetime.strptime(date, "%Y-%m-%d") for date in sorted(data.keys())]
    downloads = [data[date.strftime("%Y-%m-%d")] for date in dates]
    cumulative_downloads = np.cumsum(downloads)

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
    plt.title("Downloads Over Time")
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
