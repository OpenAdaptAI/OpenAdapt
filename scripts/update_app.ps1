function Invoke-LatestReleaseDownload {
    # Check if required utilities are installed
    if (-not (Get-Command jq -ErrorAction SilentlyContinue)) {
        Write-Host "jq is required but not installed. Please install jq to proceed."
        exit 1
    }

    if (-not (Get-Command curl -ErrorAction SilentlyContinue)) {
        Write-Host "curl is required but not installed. Please install curl to proceed."
        exit 1
    }

    # GitHub repository in the format 'owner/repo'
    $repo = "OpenAdaptAI/OpenAdapt"

    # GitHub API URL to fetch the latest release
    $apiUrl = "https://api.github.com/repos/$repo/releases/latest"

    # Fetch the latest release information
    Write-Host "Fetching latest release information for $repo..."
    $response = Invoke-RestMethod -Uri $apiUrl

    # Check if the response contains an error
    if ($response.message) {
        Write-Host "Error: $($response.message)"
        exit 1
    }

    # Extract the release tag name and assets URL
    $tagName = $response.tag_name
    $assetsUrl = $response.assets_url

    Write-Host "Latest release: $tagName"

    # Fetch the assets information
    $assets = Invoke-RestMethod -Uri $assetsUrl

    # Download each asset
    Write-Host "Downloading assets..."
    foreach ($asset in $assets) {
        $assetUrl = $asset.browser_download_url
        Write-Host "Downloading $assetUrl..."
        & Invoke-WebRequest -o $(Split-Path -Leaf $assetUrl) $assetUrl
    }

    Write-Host "All assets downloaded successfully."

    # Remove older versions
    Remove-Item -Recurse -Force dist/OpenAdapt.zip, dist/OpenAdapt.app.zip
    Remove-Item -Recurse -Force dist/OpenAdapt, dist/OpenAdapt.app

    # Rename the downloaded assets
    Rename-Item "OpenAdapt-$tagName.zip" "OpenAdapt.zip"
    Rename-Item "OpenAdapt-$tagName.app.zip" "OpenAdapt.app.zip"

    # Move assets to dist folder
    Move-Item "OpenAdapt.zip" "dist/"
    Move-Item "OpenAdapt.app.zip" "dist/"

    # Unzip the downloaded assets
    Expand-Archive -Force -Path "dist/OpenAdapt.zip" -DestinationPath "dist/"
    Expand-Archive -Force -Path "dist/OpenAdapt.app.zip" -DestinationPath "dist/"
}

# Download the assets
Invoke-LatestReleaseDownload

Write-Host "Updated the OpenAdapt app successfully!"
