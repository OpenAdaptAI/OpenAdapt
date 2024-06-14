"""Plot downloads over time."""

import requests
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
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
            ignored_names |= set(
                [
                    asset["name"]
                    for asset in release["assets"]
                    if not asset["name"].endswith(".zip")
                ]
            )
            total_downloads = sum(
                asset["download_count"]
                for asset in release["assets"]
                if asset["name"].endswith(".zip")
            )
            download_data.setdefault(release_date, 0)
            download_data[release_date] += total_downloads
        page += 1  # Increment page number for the next API request
    print(f"ignored_names=\n{pformat(ignored_names)}")
    return download_data


import matplotlib.dates as mdates
from datetime import datetime, timedelta  # Import timedelta along with datetime


def plot_downloads(data: dict) -> None:
    """Plots number of downloads and cumulative downloads over time using matplotlib.

    Includes total cumulative in the title and annotates a specific event date with
    styled text.

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

    # Annotation for the release download button addition
    event_date = datetime(2024, 5, 9, 2, 46)  # Year, Month, Day, Hour, Minute
    plt.axvline(x=event_date, color="g", linestyle=":", label="Download Buttons Added")
    plt.annotate(
        "Download Buttons Added at\nwww.openadapt.ai",
        xy=(event_date, plt.ylim()[0] + 100),
        xytext=(
            event_date - timedelta(days=10),
            plt.ylim()[1] * 0.85,
        ),  # Shift left by 10 days
        horizontalalignment="center",
        fontsize=10,
        bbox=dict(
            boxstyle="round,pad=0.5", edgecolor="green", facecolor="#ffffcc", alpha=0.9
        ),
    )

    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plt.title(
        "github.com/OpenAdaptAI/OpenAdapt"
        "\nRelease Downloads Over Time"
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
