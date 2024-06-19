#!/bin/bash

# Function to download assets from the latest release of a GitHub repository
download_latest_release_assets() {
    # Check if required utilities are installed
    if ! command -v jq &> /dev/null; then
        echo "jq is required but not installed. Please install jq to proceed."
        exit 1
    fi

    if ! command -v curl &> /dev/null; then
        echo "curl is required but not installed. Please install curl to proceed."
        exit 1
    fi

    # GitHub repository in the format 'owner/repo'
    REPO="OpenAdaptAI/OpenAdapt"

    # GitHub API URL to fetch the latest release
    API_URL="https://api.github.com/repos/$REPO/releases/latest"

    # Fetch the latest release information
    echo "Fetching latest release information for $REPO..."
    response=$(curl -s "$API_URL")

    # Check if the response contains an error
    if echo "$response" | jq -e '.message' &> /dev/null; then
        echo "Error: $(echo "$response" | jq -r '.message')"
        exit 1
    fi

    # Extract the release tag name and assets URL
    tag_name=$(echo "$response" | jq -r '.tag_name')
    assets_url=$(echo "$response" | jq -r '.assets_url')

    echo "Latest release: $tag_name"

    # Fetch the assets information
    assets=$(curl -s "$assets_url")

    # Download each asset
    echo "Downloading assets..."
    echo "$assets" | jq -r '.[].browser_download_url' | while read -r asset_url; do
        echo "Downloading $asset_url..."
        curl -LO "$asset_url"
    done

    echo "All assets downloaded successfully."
}

# Download the assets
download_latest_release_assets

# Remove older versions
rm -rf dist/OpenAdapt.zip
rm -rf dist/OpenAdapt.app.zip

rm -rf dist/OpenAdapt
rm -rf dist/OpenAdapt.app

# Rename the downloaded assets
mv OpenAdapt-$tag_name.zip OpenAdapt.zip
mv OpenAdapt-$tag_name.app.zip OpenAdapt.app.zip

# Move assets to dist folder
mv OpenAdapt.zip dist/
mv OpenAdapt.app.zip dist/

# Unzip the downloaded assets
unzip -o dist/OpenAdapt.zip
unzip -o dist/OpenAdapt.app.zip

# Move the unzipped app to the Applications folder
mv OpenAdapt.app dist/
mv OpenAdapt dist/

echo "Updated the OpenAdapt app successfully!"
