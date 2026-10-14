# Azure Container Registry (ACR) Migration Guide

This guide explains how to migrate from Docker Hub to Azure Container Registry (ACR) for faster and more reliable container image pulls in Azure ML evaluations.

## Benefits

- **Faster image pulls**: ACR is in the same Azure region as your compute instances
- **Reduced network costs**: No egress charges for pulling from ACR to Azure ML
- **Improved reliability**: No Docker Hub rate limiting
- **Time savings**: 5-10 minutes faster provisioning per worker

## Cost Impact

- **ACR Storage**: ~$0.10/month for the WinArena image (~15GB)
- **Network savings**: ~$0.50 per full evaluation (154 tasks) from reduced pull times
- **ROI**: Pays for itself after 2-3 evaluations

## Setup Instructions

### 1. Create Azure Container Registry

```bash
# Set your resource group and location
RESOURCE_GROUP="openadapt-agents"
LOCATION="eastus"
ACR_NAME="openadaptevals"  # Must be globally unique

# Create container registry (Basic tier is sufficient)
az acr create \
  --name $ACR_NAME \
  --resource-group $RESOURCE_GROUP \
  --location $LOCATION \
  --sku Basic \
  --admin-enabled true

# Get login server
ACR_LOGIN_SERVER=$(az acr show --name $ACR_NAME --query loginServer --output tsv)
echo "ACR Login Server: $ACR_LOGIN_SERVER"
```

### 2. Import WinArena Image

```bash
# Import image from Docker Hub to ACR
az acr import \
  --name $ACR_NAME \
  --source docker.io/windowsarena/winarena:latest \
  --image winarena:latest \
  --force

# Verify import
az acr repository show --name $ACR_NAME --repository winarena
```

### 3. Configure Azure ML Access

Azure ML can authenticate to ACR automatically if they're in the same subscription:

```bash
# Get ACR resource ID
ACR_ID=$(az acr show --name $ACR_NAME --query id --output tsv)

# Grant AcrPull role to Azure ML workspace identity
WORKSPACE_NAME="openadapt-ml"
WORKSPACE_IDENTITY=$(az ml workspace show \
  --name $WORKSPACE_NAME \
  --resource-group $RESOURCE_GROUP \
  --query identity.principal_id \
  --output tsv)

az role assignment create \
  --assignee $WORKSPACE_IDENTITY \
  --role AcrPull \
  --scope $ACR_ID
```

### 4. Update AzureConfig

Update your code or environment variables to use the ACR image:

```python
# Option 1: In code
from openadapt_evals.benchmarks.azure import AzureConfig

config = AzureConfig(
    subscription_id="your-subscription-id",
    resource_group="openadapt-agents",
    workspace_name="openadapt-ml",
    docker_image="openadaptevals.azurecr.io/winarena:latest",  # Use ACR image
)
```

```bash
# Option 2: Environment variable
export AZURE_DOCKER_IMAGE="openadaptevals.azurecr.io/winarena:latest"
```

### 5. Verify Setup

Test with a small evaluation to ensure the ACR image works:

```bash
# Run small test evaluation
uv run python -m openadapt_evals.benchmarks.cli azure \
  --workers 1 \
  --task-ids notepad_1 \
  --waa-path /path/to/WAA
```

Check the Azure ML job logs to verify the image pulls from ACR:

```
Pulling image: openadaptevals.azurecr.io/winarena:latest
Image pull completed in 45 seconds
```

## Automated Setup Script

Use the provided helper script to automate the entire setup:

```bash
# Run the setup script
cd /path/to/openadapt-evals
./scripts/setup_acr.sh \
  --acr-name openadaptevals \
  --resource-group openadapt-agents \
  --workspace openadapt-ml
```

## Performance Comparison

| Metric | Docker Hub | Azure Container Registry |
|--------|------------|--------------------------|
| Image pull time | 8-12 minutes | 1-2 minutes |
| Provisioning time per worker | 10-15 minutes | 3-5 minutes |
| Full evaluation (10 workers) | 15-20 minutes | 5-8 minutes |
| Network cost per evaluation | $0.50 | $0.00 |

## Updating the Image

To update the WinArena image in ACR:

```bash
# Re-import latest version from Docker Hub
az acr import \
  --name $ACR_NAME \
  --source docker.io/windowsarena/winarena:latest \
  --image winarena:latest \
  --force

# Or build and push custom image
docker build -t openadaptevals.azurecr.io/winarena:custom .
az acr login --name $ACR_NAME
docker push openadaptevals.azurecr.io/winarena:custom
```

## Troubleshooting

### Image Pull Fails with Authentication Error

```bash
# Check if workspace has AcrPull permissions
az role assignment list --assignee $WORKSPACE_IDENTITY --scope $ACR_ID

# Re-grant permissions if missing
az role assignment create \
  --assignee $WORKSPACE_IDENTITY \
  --role AcrPull \
  --scope $ACR_ID
```

### Image Not Found

```bash
# List all images in ACR
az acr repository list --name $ACR_NAME --output table

# Check tags for winarena
az acr repository show-tags --name $ACR_NAME --repository winarena --output table
```

### Slow Image Pulls Despite Using ACR

- **Check region**: ACR should be in same region as Azure ML workspace
- **Check tier**: Basic tier has bandwidth limits; upgrade to Standard if needed
- **Check network**: Ensure Azure ML compute can reach ACR (same VNet or public access)

```bash
# Check ACR region
az acr show --name $ACR_NAME --query location --output tsv

# Check workspace region
az ml workspace show \
  --name $WORKSPACE_NAME \
  --resource-group $RESOURCE_GROUP \
  --query location --output tsv
```

## Cost Optimization with ACR

Combined with tiered VMs and spot instances, ACR provides additional savings:

| Configuration | Cost per Full Eval (154 tasks) | Time Savings |
|---------------|--------------------------------|--------------|
| Baseline (Docker Hub, D4_v3) | $7.68 | - |
| + ACR (faster pulls) | $6.50 | 10-15 minutes |
| + Tiered VMs | $4.20 | Same |
| + Spot Instances | $2.80 | Same |
| + All Combined | $2.50 | 10-15 minutes |

**Total Savings: 67% cost reduction + faster results**

## References

- [Azure Container Registry Documentation](https://learn.microsoft.com/en-us/azure/container-registry/)
- [Azure ML Environment Documentation](https://learn.microsoft.com/en-us/azure/machine-learning/how-to-use-environments)
- [ACR Authentication with Azure ML](https://learn.microsoft.com/en-us/azure/machine-learning/how-to-access-resources-from-endpoints-managed-identities)
