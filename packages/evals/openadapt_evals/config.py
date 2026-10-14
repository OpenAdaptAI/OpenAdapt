from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env file.

    Priority order for configuration values:
    1. Environment variables
    2. .env file
    3. Default values (None for API keys)
    """

    # VLM API Keys
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    google_api_key: str | None = None

    # Azure credentials (for WAA benchmark on Azure)
    # These are used by DefaultAzureCredential for Service Principal auth
    azure_client_id: str | None = None
    azure_client_secret: str | None = None
    azure_tenant_id: str | None = None

    # Azure ML workspace config
    azure_subscription_id: str | None = None
    azure_ml_resource_group: str | None = None
    azure_ml_workspace_name: str | None = None

    # Azure resource group for VM operations (used by benchmarks CLI)
    azure_resource_group: str = "openadapt-agents"

    # Azure VM settings (optional overrides)
    azure_vm_size: str = "Standard_D8ds_v5"
    azure_docker_image: str = "waa-auto:latest"

    # Azure Container Registry (for pre-built Docker images)
    acr_name: str = "openadaptacr"
    acr_login_server: str = "openadaptacr.azurecr.io"

    # Azure Storage for async inference queue
    azure_storage_connection_string: str | None = None
    azure_inference_queue_name: str = "inference-jobs"
    azure_checkpoints_container: str = "checkpoints"
    azure_comparisons_container: str = "comparisons"

    # WAA VM connection (used by WAAConnection for SSH tunnel)
    waa_host: str | None = None  # VM IP address
    waa_key: str | None = None  # SSH key path (e.g., ~/.ssh/waa_key)
    waa_user: str = "azureuser"  # SSH username

    # Multi-cloud settings
    cloud_provider: str = "azure"  # "azure" or "aws"
    aws_region: str = "us-east-1"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",  # ignore extra env vars
    }


settings = Settings()
