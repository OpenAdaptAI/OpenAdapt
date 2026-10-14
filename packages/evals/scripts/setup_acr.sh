#!/usr/bin/env bash
# Setup Azure Container Registry for openadapt-evals
#
# Usage:
#   ./scripts/setup_acr.sh --acr-name <name> --resource-group <rg> --workspace <workspace>
#
# Example:
#   ./scripts/setup_acr.sh \
#     --acr-name openadaptevals \
#     --resource-group openadapt-agents \
#     --workspace openadapt-ml

set -euo pipefail

# Default values
ACR_NAME=""
RESOURCE_GROUP=""
WORKSPACE_NAME=""
LOCATION="eastus"
IMAGE_SOURCE="docker.io/windowsarena/winarena:latest"
IMAGE_TAG="latest"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --acr-name)
            ACR_NAME="$2"
            shift 2
            ;;
        --resource-group)
            RESOURCE_GROUP="$2"
            shift 2
            ;;
        --workspace)
            WORKSPACE_NAME="$2"
            shift 2
            ;;
        --location)
            LOCATION="$2"
            shift 2
            ;;
        --image-source)
            IMAGE_SOURCE="$2"
            shift 2
            ;;
        --image-tag)
            IMAGE_TAG="$2"
            shift 2
            ;;
        --help)
            echo "Usage: $0 --acr-name <name> --resource-group <rg> --workspace <workspace> [options]"
            echo ""
            echo "Required:"
            echo "  --acr-name <name>           Name for Azure Container Registry (must be globally unique)"
            echo "  --resource-group <rg>       Resource group name"
            echo "  --workspace <workspace>     Azure ML workspace name"
            echo ""
            echo "Optional:"
            echo "  --location <location>       Azure region (default: eastus)"
            echo "  --image-source <source>     Source image to import (default: docker.io/windowsarena/winarena:latest)"
            echo "  --image-tag <tag>           Tag for imported image (default: latest)"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Validate required arguments
if [[ -z "$ACR_NAME" ]]; then
    echo "Error: --acr-name is required"
    exit 1
fi

if [[ -z "$RESOURCE_GROUP" ]]; then
    echo "Error: --resource-group is required"
    exit 1
fi

if [[ -z "$WORKSPACE_NAME" ]]; then
    echo "Error: --workspace is required"
    exit 1
fi

echo "=========================================="
echo "Azure Container Registry Setup"
echo "=========================================="
echo "ACR Name:          $ACR_NAME"
echo "Resource Group:    $RESOURCE_GROUP"
echo "Workspace:         $WORKSPACE_NAME"
echo "Location:          $LOCATION"
echo "Source Image:      $IMAGE_SOURCE"
echo "Target Tag:        $IMAGE_TAG"
echo "=========================================="
echo ""

# Step 1: Create ACR
echo "[1/5] Creating Azure Container Registry..."
if az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
    echo "      ACR '$ACR_NAME' already exists, skipping creation"
else
    az acr create \
        --name "$ACR_NAME" \
        --resource-group "$RESOURCE_GROUP" \
        --location "$LOCATION" \
        --sku Basic \
        --admin-enabled true
    echo "      ACR created successfully"
fi

# Get ACR details
ACR_LOGIN_SERVER=$(az acr show --name "$ACR_NAME" --query loginServer --output tsv)
ACR_ID=$(az acr show --name "$ACR_NAME" --query id --output tsv)

echo "      Login Server: $ACR_LOGIN_SERVER"
echo ""

# Step 2: Import WinArena image
echo "[2/5] Importing WinArena image from Docker Hub..."
echo "      This may take 5-10 minutes depending on network speed..."
az acr import \
    --name "$ACR_NAME" \
    --source "$IMAGE_SOURCE" \
    --image "winarena:$IMAGE_TAG" \
    --force

echo "      Image imported successfully"
echo ""

# Step 3: Verify import
echo "[3/5] Verifying image import..."
IMAGE_SIZE=$(az acr repository show \
    --name "$ACR_NAME" \
    --repository winarena \
    --query "lastUpdateTime" \
    --output tsv)

if [[ -n "$IMAGE_SIZE" ]]; then
    echo "      Image verified: winarena:$IMAGE_TAG"
    echo "      Last updated: $IMAGE_SIZE"
else
    echo "      Warning: Could not verify image"
fi
echo ""

# Step 4: Configure workspace permissions
echo "[4/5] Configuring Azure ML workspace permissions..."
WORKSPACE_IDENTITY=$(az ml workspace show \
    --name "$WORKSPACE_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query identity.principalId \
    --output tsv 2>/dev/null || echo "")

if [[ -z "$WORKSPACE_IDENTITY" ]]; then
    echo "      Warning: Could not get workspace identity"
    echo "      You may need to manually grant AcrPull permissions"
else
    # Check if role assignment already exists
    EXISTING_ROLE=$(az role assignment list \
        --assignee "$WORKSPACE_IDENTITY" \
        --scope "$ACR_ID" \
        --role AcrPull \
        --query "[0].id" \
        --output tsv 2>/dev/null || echo "")

    if [[ -n "$EXISTING_ROLE" ]]; then
        echo "      AcrPull role already assigned to workspace"
    else
        az role assignment create \
            --assignee "$WORKSPACE_IDENTITY" \
            --role AcrPull \
            --scope "$ACR_ID" \
            --output none

        echo "      AcrPull role assigned to workspace"
    fi
fi
echo ""

# Step 5: Display configuration
echo "[5/5] Setup complete!"
echo ""
echo "=========================================="
echo "Configuration Summary"
echo "=========================================="
echo ""
echo "ACR Details:"
echo "  Name:         $ACR_NAME"
echo "  Login Server: $ACR_LOGIN_SERVER"
echo "  Image:        $ACR_LOGIN_SERVER/winarena:$IMAGE_TAG"
echo ""
echo "Next Steps:"
echo ""
echo "1. Update your AzureConfig to use the ACR image:"
echo ""
echo "   # In Python code:"
echo "   config = AzureConfig("
echo "       docker_image=\"$ACR_LOGIN_SERVER/winarena:$IMAGE_TAG\","
echo "       ..."
echo "   )"
echo ""
echo "   # Or set environment variable:"
echo "   export AZURE_DOCKER_IMAGE=\"$ACR_LOGIN_SERVER/winarena:$IMAGE_TAG\""
echo ""
echo "2. Test with a small evaluation:"
echo ""
echo "   uv run python -m openadapt_evals.benchmarks.cli azure \\"
echo "     --workers 1 \\"
echo "     --task-ids notepad_1 \\"
echo "     --waa-path /path/to/WAA"
echo ""
echo "3. Monitor image pull time in Azure ML logs"
echo "   (should be 1-2 minutes instead of 8-12 minutes)"
echo ""
echo "=========================================="
