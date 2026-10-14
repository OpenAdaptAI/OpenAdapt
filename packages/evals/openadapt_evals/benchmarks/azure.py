"""Azure deployment automation for WAA benchmark.

This module provides Azure VM orchestration for running Windows Agent Arena
at scale across multiple parallel VMs.

Requirements:
    - azure-ai-ml
    - azure-identity
    - Azure subscription with ML workspace

Example:
    from openadapt_evals.benchmarks.azure import AzureWAAOrchestrator, AzureConfig

    config = AzureConfig(
        subscription_id="your-subscription-id",
        resource_group="agents",
        workspace_name="agents_ml",
    )
    orchestrator = AzureWAAOrchestrator(config, waa_repo_path="/path/to/WAA")

    # Run evaluation on 40 parallel VMs
    results = orchestrator.run_evaluation(
        agent=my_agent,
        num_workers=40,
        task_ids=None,  # All tasks
    )
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from openadapt_evals.adapters import BenchmarkResult, BenchmarkTask
from openadapt_evals.agents import BenchmarkAgent

logger = logging.getLogger(__name__)


# VM Tier Configuration for Cost Optimization
VM_TIERS = {
    "simple": "Standard_D2_v3",   # 2 vCPUs, 8 GB RAM - Notepad, File Explorer, basic apps
    "medium": "Standard_D4_v3",   # 4 vCPUs, 16 GB RAM - Chrome, Office, email
    "complex": "Standard_D8_v3",  # 8 vCPUs, 32 GB RAM - Coding, multi-app workflows
}

# Hourly costs for VM tiers (East US pricing, regular instances)
VM_TIER_COSTS = {
    "simple": 0.096,   # $0.096/hour
    "medium": 0.192,   # $0.192/hour
    "complex": 0.384,  # $0.384/hour
}

# How many consecutive poll failures for one worker before the run is failed
# rather than polled forever. A worker we cannot reach has produced NO results,
# which is not the same as having produced zero-score ones.
_MAX_WORKER_POLL_ERRORS = 10

# Spot instance hourly costs (approximately 70-80% discount)
VM_TIER_SPOT_COSTS = {
    "simple": 0.024,   # ~$0.024/hour (75% discount)
    "medium": 0.048,   # ~$0.048/hour (75% discount)
    "complex": 0.096,  # ~$0.096/hour (75% discount)
}


def classify_task_complexity(task: BenchmarkTask) -> str:
    """Classify task complexity to select appropriate VM tier.

    Classification priority: complex > medium > simple > default(medium)

    Args:
        task: The benchmark task to classify.

    Returns:
        VM tier name: "simple", "medium", or "complex"
    """
    task_id = task.task_id.lower()
    instruction = task.instruction.lower()
    domain = (task.domain or "").lower()

    # Complex tasks: Coding, debugging, multi-app workflows, data analysis
    complex_indicators = [
        "code", "debug", "compile", "ide", "visual studio",
        "git", "terminal", "powershell", "cmd",
        "excel formula", "pivot table", "macro",
        "multiple applications", "switch between",
        "data analysis", "chart", "graph",
        "multitasking",
    ]

    # Medium tasks: Browser, Office apps, email
    medium_indicators = [
        "browser", "chrome", "edge", "firefox",
        "word", "excel", "powerpoint", "office",
        "email", "outlook", "calendar",
        "pdf", "acrobat",
    ]

    # Simple tasks: Notepad, File Explorer, basic Windows operations
    # Note: Check these AFTER medium to avoid "open" matching browser tasks
    simple_indicators = [
        "notepad", "file explorer", "file_explorer", "calculator", "paint",
    ]

    # Simple domains take precedence for direct domain matching
    simple_domains = {"notepad", "calculator", "paint", "file_explorer"}

    # Check for complex indicators first
    for indicator in complex_indicators:
        if indicator in task_id or indicator in instruction or indicator in domain:
            return "complex"

    # Check for medium indicators (browsers, office apps are more complex than notepad)
    for indicator in medium_indicators:
        if indicator in task_id or indicator in instruction or indicator in domain:
            return "medium"

    # Check for simple domains (direct match)
    if domain in simple_domains:
        return "simple"

    # Check for simple indicators in task_id or instruction
    for indicator in simple_indicators:
        if indicator in task_id or indicator in instruction:
            return "simple"

    # Default to medium for unknown tasks
    return "medium"


@dataclass
class AzureJobLogParser:
    """Parses Azure ML job logs to extract task progress.

    Looks for patterns like:
    - "Task 1/10: {task_id}"
    - "Step {step_idx}: {action_type}"
    - "Task {task_id}: SUCCESS/FAIL"
    - Error messages
    """

    # Regex patterns for log parsing
    TASK_START_PATTERN = re.compile(r"Task (\d+)/(\d+):\s+(\S+)")
    STEP_PATTERN = re.compile(r"Step (\d+):\s+(\w+)")
    TASK_RESULT_PATTERN = re.compile(r"Task (\S+):\s+(SUCCESS|FAIL)")
    ERROR_PATTERN = re.compile(r"ERROR|Error|error|Exception|Traceback")

    def __init__(self):
        self.current_task: str | None = None
        self.current_task_idx: int = 0
        self.total_tasks: int = 0
        self.current_step: int = 0
        self.errors: list[str] = []

    def parse_line(self, line: str) -> dict[str, Any] | None:
        """Parse a log line and return extracted information.

        Args:
            line: Log line to parse.

        Returns:
            Dict with parsed data or None if no match.
        """
        # Check for task start
        match = self.TASK_START_PATTERN.search(line)
        if match:
            self.current_task_idx = int(match.group(1))
            self.total_tasks = int(match.group(2))
            self.current_task = match.group(3)
            self.current_step = 0
            return {
                "type": "task_start",
                "task_idx": self.current_task_idx,
                "total_tasks": self.total_tasks,
                "task_id": self.current_task,
            }

        # Check for step
        match = self.STEP_PATTERN.search(line)
        if match:
            self.current_step = int(match.group(1))
            action_type = match.group(2)
            return {
                "type": "step",
                "step_idx": self.current_step,
                "action_type": action_type,
                "task_id": self.current_task,
            }

        # Check for task result
        match = self.TASK_RESULT_PATTERN.search(line)
        if match:
            task_id = match.group(1)
            result = match.group(2)
            return {
                "type": "task_result",
                "task_id": task_id,
                "success": result == "SUCCESS",
            }

        # Check for errors
        if self.ERROR_PATTERN.search(line):
            self.errors.append(line)
            return {
                "type": "error",
                "message": line,
            }

        return None


@dataclass
class AzureConfig:
    """Azure configuration for WAA deployment.

    Attributes:
        subscription_id: Azure subscription ID.
        resource_group: Resource group containing ML workspace.
        workspace_name: Azure ML workspace name.
        vm_size: VM size for compute instances (must support nested virtualization).
        vm_security_type: VM security type (Standard or TrustedLaunch). Use Standard for nested virt.
        enable_nested_virtualization: Whether to enable nested virtualization (default: True).
        idle_timeout_minutes: Auto-shutdown after idle (minutes).
        docker_image: Docker image for agent container.
        storage_account: Storage account for results (auto-detected if None).
        use_managed_identity: Whether to use managed identity for auth.
        managed_identity_name: Name of managed identity (if using).
        enable_tiered_vms: Whether to auto-select VM size based on task complexity (default: False).
        use_spot_instances: Whether to use spot instances for cost savings (default: False).
        max_spot_price: Maximum hourly price for spot instances (default: 0.5).
        spot_eviction_policy: What to do when spot instance is evicted (Deallocate or Delete).
        environment: Deployment environment (production or development).
    """

    subscription_id: str
    resource_group: str
    workspace_name: str
    vm_size: str = "Standard_D4s_v5"  # Better nested virt support than v3
    vm_security_type: str = "Standard"  # NOT TrustedLaunch (disables nested virt)
    enable_nested_virtualization: bool = True
    idle_timeout_minutes: int = 60
    docker_image: str = "windowsarena/winarena:latest"  # Public Docker Hub image
    storage_account: str | None = None
    use_managed_identity: bool = False
    managed_identity_name: str | None = None
    # Cost optimization features
    enable_tiered_vms: bool = False  # Auto-select VM size based on task complexity
    use_spot_instances: bool = False  # Use spot instances for 70-80% cost savings
    max_spot_price: float = 0.5  # Maximum hourly price for spot instances
    spot_eviction_policy: str = "Deallocate"  # Deallocate or Delete
    environment: str = "production"  # production or development

    @classmethod
    def from_env(cls) -> AzureConfig:
        """Create config from environment variables.

        Required env vars:
            AZURE_SUBSCRIPTION_ID
            AZURE_ML_RESOURCE_GROUP
            AZURE_ML_WORKSPACE_NAME

        Optional env vars:
            AZURE_VM_SIZE (default: Standard_D4s_v5)
            AZURE_VM_SECURITY_TYPE (default: Standard)
            AZURE_DOCKER_IMAGE (default: windowsarena/winarena:latest)
            AZURE_ENABLE_TIERED_VMS (default: false) - Auto-select VM size by task complexity
            AZURE_USE_SPOT_INSTANCES (default: false) - Use spot instances for cost savings
            AZURE_MAX_SPOT_PRICE (default: 0.5) - Maximum hourly price for spot instances
            AZURE_ENVIRONMENT (default: production) - Set to 'development' to enable spot by default

        Authentication (one of):
            - AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID (service principal)
            - Azure CLI login (`az login`)
            - Managed Identity (when running on Azure)

        Raises:
            ValueError: If required settings are not configured.
        """
        subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")
        resource_group = os.getenv("AZURE_ML_RESOURCE_GROUP")
        workspace_name = os.getenv("AZURE_ML_WORKSPACE_NAME")

        # Validate required settings
        if not subscription_id:
            raise ValueError(
                "AZURE_SUBSCRIPTION_ID not set. "
                "Set it in environment or .env file."
            )
        if not resource_group:
            raise ValueError(
                "AZURE_ML_RESOURCE_GROUP not set. "
                "Set it in environment or .env file."
            )
        if not workspace_name:
            raise ValueError(
                "AZURE_ML_WORKSPACE_NAME not set. "
                "Set it in environment or .env file."
            )

        # Cost optimization settings
        environment = os.getenv("AZURE_ENVIRONMENT", "production")
        enable_tiered_vms = os.getenv("AZURE_ENABLE_TIERED_VMS", "false").lower() == "true"
        use_spot_instances = os.getenv("AZURE_USE_SPOT_INSTANCES",
                                       "true" if environment == "development" else "false").lower() == "true"

        return cls(
            subscription_id=subscription_id,
            resource_group=resource_group,
            workspace_name=workspace_name,
            vm_size=os.getenv("AZURE_VM_SIZE", "Standard_D4s_v5"),
            vm_security_type=os.getenv("AZURE_VM_SECURITY_TYPE", "Standard"),
            docker_image=os.getenv(
                "AZURE_DOCKER_IMAGE",
                "windowsarena/winarena:latest"
            ),
            enable_tiered_vms=enable_tiered_vms,
            use_spot_instances=use_spot_instances,
            max_spot_price=float(os.getenv("AZURE_MAX_SPOT_PRICE", "0.5")),
            environment=environment,
        )

    @classmethod
    def from_json(cls, path: str | Path) -> AzureConfig:
        """Load config from JSON file."""
        with open(path) as f:
            data = json.load(f)
        return cls(**data)

    def to_json(self, path: str | Path) -> None:
        """Save config to JSON file."""
        with open(path, "w") as f:
            json.dump(self.__dict__, f, indent=2)


@dataclass
class WorkerState:
    """State of a single worker VM."""

    worker_id: int
    compute_name: str
    status: str = "pending"  # pending, running, completed, failed
    assigned_tasks: list[str] = field(default_factory=list)
    completed_tasks: list[str] = field(default_factory=list)
    results: list[BenchmarkResult] = field(default_factory=list)
    error: str | None = None
    start_time: float | None = None
    end_time: float | None = None
    # Cost tracking
    vm_tier: str = "medium"  # simple, medium, or complex
    vm_size: str = "Standard_D4s_v5"  # Actual VM size used
    is_spot: bool = False  # Whether spot instance was used
    hourly_cost: float = 0.192  # Actual hourly cost
    total_cost: float = 0.0  # Total cost for this worker


@dataclass
class EvaluationRun:
    """State of an evaluation run across multiple workers."""

    run_id: str
    experiment_name: str
    num_workers: int
    total_tasks: int
    workers: list[WorkerState] = field(default_factory=list)
    status: str = "pending"  # pending, running, completed, failed
    start_time: float | None = None
    end_time: float | None = None
    total_cost: float = 0.0  # Total cost for entire evaluation

    def to_dict(self) -> dict:
        """Serialize to dict for JSON storage."""
        # Calculate total cost
        total_cost = sum(w.total_cost for w in self.workers)

        return {
            "run_id": self.run_id,
            "experiment_name": self.experiment_name,
            "num_workers": self.num_workers,
            "total_tasks": self.total_tasks,
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "total_cost": total_cost,
            "cost_per_task": total_cost / self.total_tasks if self.total_tasks > 0 else 0,
            "workers": [
                {
                    "worker_id": w.worker_id,
                    "compute_name": w.compute_name,
                    "status": w.status,
                    "assigned_tasks": w.assigned_tasks,
                    "completed_tasks": w.completed_tasks,
                    "error": w.error,
                    "vm_tier": w.vm_tier,
                    "vm_size": w.vm_size,
                    "is_spot": w.is_spot,
                    "hourly_cost": w.hourly_cost,
                    "total_cost": w.total_cost,
                }
                for w in self.workers
            ],
        }


class AzureMLClient:
    """Wrapper around Azure ML SDK for compute management.

    This provides a simplified interface for creating and managing
    Azure ML compute instances for WAA evaluation.
    """

    def __init__(self, config: AzureConfig):
        self.config = config
        self._client = None
        self._ensure_sdk_available()

    def _ensure_sdk_available(self) -> None:
        """Check that Azure SDK is available."""
        try:
            from azure.ai.ml import MLClient
            from azure.identity import (
                ClientSecretCredential,
                DefaultAzureCredential,
            )

            self._MLClient = MLClient
            self._DefaultAzureCredential = DefaultAzureCredential
            self._ClientSecretCredential = ClientSecretCredential
        except ImportError as e:
            raise ImportError(
                "Azure ML SDK not installed. Install with: "
                "pip install azure-ai-ml azure-identity"
            ) from e

    @property
    def client(self):
        """Lazy-load ML client.

        Uses service principal credentials if configured in env,
        otherwise falls back to DefaultAzureCredential (CLI login, managed identity, etc.)
        """
        if self._client is None:
            credential = self._get_credential()
            self._client = self._MLClient(
                credential=credential,
                subscription_id=self.config.subscription_id,
                resource_group_name=self.config.resource_group,
                workspace_name=self.config.workspace_name,
            )
            logger.info(f"Connected to Azure ML workspace: {self.config.workspace_name}")
        return self._client

    def _get_credential(self):
        """Get Azure credential, preferring service principal if configured."""
        client_id = os.getenv("AZURE_CLIENT_ID")
        client_secret = os.getenv("AZURE_CLIENT_SECRET")
        tenant_id = os.getenv("AZURE_TENANT_ID")

        # Use service principal if credentials are configured
        if all([client_id, client_secret, tenant_id]):
            logger.info("Using service principal authentication")
            return self._ClientSecretCredential(
                tenant_id=tenant_id,
                client_id=client_id,
                client_secret=client_secret,
            )

        # Fall back to DefaultAzureCredential (CLI login, managed identity, etc.)
        logger.info(
            "Using DefaultAzureCredential (ensure you're logged in with 'az login' "
            "or have service principal credentials in env)"
        )
        return self._DefaultAzureCredential()

    def create_compute_instance(
        self,
        name: str,
        startup_script: str | None = None,
        vm_size: str | None = None,
        use_spot: bool | None = None,
    ) -> str:
        """Create a compute instance.

        Args:
            name: Compute instance name.
            startup_script: Optional startup script content (not yet implemented).
            vm_size: Override VM size (uses config.vm_size if None).
            use_spot: Override spot instance setting (uses config.use_spot_instances if None).

        Returns:
            Compute instance name.
        """
        from azure.ai.ml.entities import ComputeInstance

        # Check if already exists
        try:
            existing = self.client.compute.get(name)
            if existing:
                logger.info(f"Compute instance {name} already exists")
                return name
        except Exception:
            pass  # Doesn't exist, create it

        # Determine VM size and spot settings
        vm_size = vm_size or self.config.vm_size
        use_spot = use_spot if use_spot is not None else self.config.use_spot_instances

        # CRITICAL: Use Standard security type for nested virtualization
        # TrustedLaunch (Azure default since 2024) disables nested virtualization
        compute = ComputeInstance(
            name=name,
            size=vm_size,
            idle_time_before_shutdown_minutes=self.config.idle_timeout_minutes,
        )

        # Configure spot instance if enabled
        if use_spot:
            # Note: Azure ML Compute Instances don't directly support spot instances
            # in the same way as VMs. For now, we log this and use regular instances.
            # Full spot support would require using Azure ML Compute Clusters instead.
            logger.warning(
                f"Spot instances requested but not supported for ComputeInstance. "
                f"Using regular instance for {name}. "
                f"Consider using Azure ML Compute Clusters for spot instance support."
            )
            # Future enhancement: Switch to AmlCompute cluster with low priority nodes
            # from azure.ai.ml.entities import AmlCompute
            # compute = AmlCompute(
            #     name=name,
            #     size=vm_size,
            #     min_instances=0,
            #     max_instances=1,
            #     tier="LowPriority",  # Spot instance equivalent
            # )

        # Note: VM security type configuration may vary by Azure ML SDK version
        # The vm_security_type parameter controls whether nested virtualization is enabled
        # For Azure ML SDK v2, this is typically set through additional_properties or
        # by ensuring we use Standard tier VMs (not TrustedLaunch)
        # The config.vm_security_type and config.enable_nested_virtualization are
        # available for future SDK updates or custom deployment templates

        # Add managed identity if configured
        if self.config.use_managed_identity and self.config.managed_identity_name:
            identity_id = (
                f"/subscriptions/{self.config.subscription_id}"
                f"/resourceGroups/{self.config.resource_group}"
                f"/providers/Microsoft.ManagedIdentity"
                f"/userAssignedIdentities/{self.config.managed_identity_name}"
            )
            compute.identity = {"type": "UserAssigned", "user_assigned_identities": [identity_id]}

        spot_indicator = " (spot)" if use_spot else ""
        print(f"      Creating VM: {name} ({vm_size}{spot_indicator})...", end="", flush=True)
        self.client.compute.begin_create_or_update(compute).result()
        print(" done")

        return name

    def delete_compute_instance(self, name: str) -> None:
        """Delete a compute instance.

        Args:
            name: Compute instance name.
        """
        try:
            logger.info(f"Deleting compute instance: {name}")
            self.client.compute.begin_delete(name).result()
            logger.info(f"Compute instance {name} deleted")
        except Exception as e:
            logger.warning(f"Failed to delete compute instance {name}: {e}")

    def list_compute_instances(self, prefix: str | None = None) -> list[dict]:
        """List compute instances.

        Args:
            prefix: Optional name prefix filter.

        Returns:
            List of dicts with compute instance info (name, state, created_on).
        """
        computes = self.client.compute.list()
        instances = []
        for c in computes:
            if c.type == "ComputeInstance":
                if prefix is None or c.name.startswith(prefix):
                    instances.append({
                        "name": c.name,
                        "state": c.state,
                        "created_on": c.created_on if hasattr(c, "created_on") else None,
                    })
        return instances

    def get_compute_status(self, name: str) -> str:
        """Get compute instance status.

        Args:
            name: Compute instance name.

        Returns:
            Status string (Running, Stopped, etc.)
        """
        compute = self.client.compute.get(name)
        return compute.state

    def submit_job(
        self,
        compute_name: str,
        command: str,
        environment_variables: dict[str, str] | None = None,
        display_name: str | None = None,
        timeout_hours: float = 4.0,
    ) -> str:
        """Submit a job to a compute instance.

        Args:
            compute_name: Target compute instance.
            command: Command to run.
            environment_variables: Environment variables.
            display_name: Job display name.
            timeout_hours: Maximum job duration in hours (default: 4). The job
                will be automatically canceled after this duration.

        Returns:
            Job name/ID.
        """
        from azure.ai.ml import command as ml_command
        from azure.ai.ml.entities import CommandJobLimits, Environment

        # Create environment with Docker image
        env = Environment(
            image=self.config.docker_image,
            name="waa-agent-env",
        )

        import uuid
        timestamp = int(time.time())
        unique_id = str(uuid.uuid4())[:8]
        job_name = f"waa-{compute_name}-{timestamp}-{unique_id}"

        # Convert hours to seconds for Azure ML timeout
        timeout_seconds = int(timeout_hours * 3600)

        job = ml_command(
            command=command,
            environment=env,
            compute=compute_name,
            name=job_name,
            display_name=display_name or f"waa-job-{compute_name}",
            environment_variables=environment_variables or {},
            limits=CommandJobLimits(timeout=timeout_seconds),
        )

        submitted = self.client.jobs.create_or_update(job)
        logger.info(f"Job submitted: {submitted.name} (timeout: {timeout_hours}h)")
        return submitted.name

    def wait_for_job(self, job_name: str, timeout_seconds: int = 3600) -> dict:
        """Wait for a job to complete.

        Args:
            job_name: Job name/ID.
            timeout_seconds: Maximum wait time.

        Returns:
            Job result dict.
        """
        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            job = self.client.jobs.get(job_name)
            if job.status in ["Completed", "Failed", "Canceled"]:
                return {
                    "status": job.status,
                    "outputs": job.outputs if hasattr(job, "outputs") else {},
                }
            time.sleep(10)

        raise TimeoutError(f"Job {job_name} did not complete within {timeout_seconds}s")

    def stream_job_logs(
        self,
        job_name: str,
        on_log_line: Callable[[str], None] | None = None,
    ) -> subprocess.Popen:
        """Stream Azure ML job logs in real-time via az ml job stream.

        Args:
            job_name: Job name/ID.
            on_log_line: Optional callback for each log line.

        Returns:
            Subprocess handle (caller should call .wait() or .terminate()).
        """
        cmd = [
            "az",
            "ml",
            "job",
            "stream",
            "--name",
            job_name,
            "--workspace-name",
            self.config.workspace_name,
            "--resource-group",
            self.config.resource_group,
        ]

        logger.info(f"Starting log stream for job: {job_name}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # Line buffered
        )

        # Start background thread to read logs
        def _read_logs():
            try:
                for line in process.stdout:
                    line = line.rstrip()
                    if on_log_line:
                        on_log_line(line)
                    logger.debug(f"[Azure ML] {line}")
            except Exception as e:
                logger.error(f"Error reading job logs: {e}")

        thread = threading.Thread(target=_read_logs, daemon=True)
        thread.start()

        return process


class AzureWAAOrchestrator:
    """Orchestrates WAA evaluation across multiple Azure VMs.

    This class manages the full lifecycle of a distributed WAA evaluation:
    1. Provisions Azure ML compute instances
    2. Distributes tasks across workers
    3. Monitors progress and collects results
    4. Cleans up resources

    Example:
        config = AzureConfig.from_env()
        orchestrator = AzureWAAOrchestrator(config, waa_repo_path="/path/to/WAA")

        results = orchestrator.run_evaluation(
            agent=my_agent,
            num_workers=40,
        )
        print(f"Success rate: {sum(r.success for r in results) / len(results):.1%}")
    """

    def __init__(
        self,
        config: AzureConfig,
        waa_repo_path: str | Path,
        experiment_name: str = "waa-eval",
    ):
        """Initialize orchestrator.

        Args:
            config: Azure configuration.
            waa_repo_path: Path to WAA repository.
            experiment_name: Name prefix for this evaluation.
        """
        self.config = config
        self.waa_repo_path = Path(waa_repo_path)
        self.experiment_name = experiment_name
        self.ml_client = AzureMLClient(config)
        self._current_run: EvaluationRun | None = None
        self._cleanup_registered = False
        self._interrupted = False

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful cleanup on interruption."""
        if self._cleanup_registered:
            return

        def signal_handler(sig, frame):
            logger.warning("\n⚠️  Interrupted! Cleaning up compute instances...")
            self._interrupted = True
            if self._current_run and self._current_run.workers:
                self._cleanup_workers(self._current_run.workers)
            sys.exit(1)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        self._cleanup_registered = True
        logger.info("Signal handlers registered for graceful cleanup")

    def cleanup_stale_instances(self, prefix: str = "waa", dry_run: bool = False) -> int:
        """Delete compute instances from previous runs.

        This prevents quota exhaustion from stale instances that weren't
        properly cleaned up after failures or interruptions.

        Args:
            prefix: Name prefix filter (default: "waa").
            dry_run: If True, only list instances without deleting.

        Returns:
            Number of instances cleaned up (or found if dry_run=True).
        """
        logger.info(f"Scanning for stale compute instances with prefix '{prefix}'...")
        instances = self.ml_client.list_compute_instances(prefix=prefix)

        if not instances:
            logger.info("No stale instances found.")
            return 0

        logger.info(f"Found {len(instances)} stale instance(s):")
        for inst in instances:
            state = inst.get("state", "unknown")
            created = inst.get("created_on", "unknown")
            logger.info(f"  - {inst['name']}: {state} (created: {created})")

        if dry_run:
            logger.info("Dry-run mode: no instances deleted.")
            return len(instances)

        # Delete all stale instances in parallel
        logger.info(f"Deleting {len(instances)} stale instance(s)...")
        with ThreadPoolExecutor(max_workers=min(len(instances), 10)) as executor:
            futures = [
                executor.submit(self.ml_client.delete_compute_instance, inst["name"])
                for inst in instances
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logger.warning(f"Cleanup error: {e}")

        logger.info(f"Cleanup complete: {len(instances)} instance(s) deleted.")
        return len(instances)

    def run_evaluation(
        self,
        agent: BenchmarkAgent,
        num_workers: int = 10,
        task_ids: list[str] | None = None,
        max_steps_per_task: int = 15,
        on_worker_complete: Callable[[WorkerState], None] | None = None,
        cleanup_on_complete: bool = True,
        cleanup_stale_on_start: bool = True,
        timeout_hours: float = 4.0,
    ) -> list[BenchmarkResult]:
        """Run evaluation across multiple Azure VMs.

        Args:
            agent: Agent to evaluate (must be serializable or API-based).
            num_workers: Number of parallel VMs.
            task_ids: Specific tasks to run (None = all 154 tasks).
            max_steps_per_task: Maximum steps per task.
            on_worker_complete: Callback when a worker finishes.
            cleanup_on_complete: Whether to delete VMs after completion.
            cleanup_stale_on_start: Whether to cleanup stale instances before starting.
            timeout_hours: Maximum job duration in hours (default: 4). Jobs are
                auto-canceled after this duration to prevent runaway costs.

        Returns:
            List of BenchmarkResult for all tasks.
        """
        # Setup signal handlers for graceful cleanup on Ctrl+C
        self._setup_signal_handlers()

        # Cleanup stale instances from previous runs to prevent quota exhaustion
        if cleanup_stale_on_start:
            print("[0/5] Cleaning up stale compute instances from previous runs...")
            stale_count = self.cleanup_stale_instances(prefix="waa")
            if stale_count > 0:
                print(f"      Cleaned up {stale_count} stale instance(s).")
            else:
                print("      No stale instances found.")

        # Load tasks
        from openadapt_evals.adapters.waa import WAAAdapter

        adapter = WAAAdapter(waa_repo_path=self.waa_repo_path)
        if task_ids:
            tasks = [adapter.load_task(tid) for tid in task_ids]
        else:
            tasks = adapter.list_tasks()

        print(f"[1/5] Loaded {len(tasks)} tasks for {num_workers} worker(s)")

        # Create evaluation run
        run_id = f"{self.experiment_name}-{int(time.time())}"
        self._current_run = EvaluationRun(
            run_id=run_id,
            experiment_name=self.experiment_name,
            num_workers=num_workers,
            total_tasks=len(tasks),
            status="running",
            start_time=time.time(),
        )

        # Distribute tasks across workers
        task_batches = self._distribute_tasks(tasks, num_workers)

        # Create workers with cost tracking
        workers = []
        short_id = str(int(time.time()))[-4:]
        for i, batch in enumerate(task_batches):
            # Determine VM tier based on tasks if tiered VMs are enabled
            if self.config.enable_tiered_vms and batch:
                # Classify all tasks in batch and use highest complexity
                complexities = [classify_task_complexity(t) for t in batch]
                if "complex" in complexities:
                    vm_tier = "complex"
                elif "medium" in complexities:
                    vm_tier = "medium"
                else:
                    vm_tier = "simple"
                vm_size = VM_TIERS[vm_tier]
            else:
                # Use default VM size from config
                vm_tier = "medium"
                vm_size = self.config.vm_size

            # Determine cost
            is_spot = self.config.use_spot_instances
            if is_spot:
                hourly_cost = VM_TIER_SPOT_COSTS.get(vm_tier, 0.048)
            else:
                hourly_cost = VM_TIER_COSTS.get(vm_tier, 0.192)

            worker = WorkerState(
                worker_id=i,
                compute_name=f"waa{short_id}w{i}",
                assigned_tasks=[t.task_id for t in batch],
                vm_tier=vm_tier,
                vm_size=vm_size,
                is_spot=is_spot,
                hourly_cost=hourly_cost,
            )
            workers.append(worker)
        self._current_run.workers = workers

        try:
            # Provision VMs in parallel
            print(f"[2/5] Provisioning {num_workers} Azure VM(s)... (this takes 3-5 minutes)")
            self._provision_workers(workers)
            print("      VM(s) ready")

            # Submit jobs to workers
            print("[3/5] Submitting evaluation jobs...")
            self._submit_worker_jobs(workers, task_batches, agent, max_steps_per_task, timeout_hours)
            print("      Jobs submitted")

            # Wait for completion and collect results
            print("[4/5] Waiting for workers to complete...")
            results = self._wait_and_collect_results(workers, on_worker_complete)

            self._current_run.status = "completed"
            self._current_run.end_time = time.time()

            return results

        except KeyboardInterrupt:
            logger.warning("Evaluation interrupted by user")
            self._current_run.status = "interrupted"
            raise

        except Exception as e:
            logger.error(f"Evaluation failed: {e}")
            self._current_run.status = "failed"
            raise

        finally:
            # ALWAYS cleanup, even on error or interruption
            if cleanup_on_complete:
                print("[5/5] Cleaning up compute instances...")
                self._cleanup_workers(workers)
                print("      Cleanup complete.")

    def _distribute_tasks(
        self, tasks: list[BenchmarkTask], num_workers: int
    ) -> list[list[BenchmarkTask]]:
        """Distribute tasks evenly across workers."""
        batches: list[list[BenchmarkTask]] = [[] for _ in range(num_workers)]
        for i, task in enumerate(tasks):
            batches[i % num_workers].append(task)
        return batches

    def _provision_workers(self, workers: list[WorkerState]) -> None:
        """Provision all worker VMs in parallel with cost-optimized sizing."""
        with ThreadPoolExecutor(max_workers=len(workers)) as executor:
            futures = {
                executor.submit(
                    self.ml_client.create_compute_instance,
                    worker.compute_name,
                    None,  # startup_script
                    worker.vm_size,  # VM size based on task complexity
                    worker.is_spot,  # Spot instance setting
                ): worker
                for worker in workers
            }

            for future in as_completed(futures):
                worker = futures[future]
                try:
                    future.result()
                    worker.status = "provisioned"
                    logger.info(
                        f"Worker {worker.worker_id} provisioned: {worker.vm_size} "
                        f"({'spot' if worker.is_spot else 'regular'}) "
                        f"${worker.hourly_cost:.3f}/hr"
                    )
                except Exception as e:
                    worker.status = "failed"
                    worker.error = str(e)
                    logger.error(f"Failed to provision worker {worker.worker_id}: {e}")

    def _submit_worker_jobs(
        self,
        workers: list[WorkerState],
        task_batches: list[list[BenchmarkTask]],
        agent: BenchmarkAgent,
        max_steps: int,
        timeout_hours: float = 4.0,
    ) -> None:
        """Submit evaluation jobs to workers.

        Args:
            workers: List of worker states.
            task_batches: Task batches for each worker.
            agent: Agent to run.
            max_steps: Maximum steps per task.
            timeout_hours: Maximum job duration in hours.
        """
        for worker, tasks in zip(workers, task_batches):
            if worker.status == "failed":
                continue

            try:
                # Serialize task IDs for this worker
                task_ids = [t.task_id for t in tasks]
                task_ids_json = json.dumps(task_ids)

                # Build command
                command = self._build_worker_command(task_ids_json, max_steps, agent)

                # Submit job with timeout
                self.ml_client.submit_job(
                    compute_name=worker.compute_name,
                    command=command,
                    environment_variables={
                        "WAA_TASK_IDS": task_ids_json,
                        "WAA_MAX_STEPS": str(max_steps),
                    },
                    display_name=f"waa-worker-{worker.worker_id}",
                    timeout_hours=timeout_hours,
                )
                worker.status = "running"
                worker.start_time = time.time()

            except Exception as e:
                worker.status = "failed"
                worker.error = str(e)
                logger.error(f"Failed to submit job for worker {worker.worker_id}: {e}")

    def _build_worker_command(
        self,
        task_ids_json: str,
        max_steps: int,
        agent: BenchmarkAgent,
    ) -> str:
        """Build the command to run on a worker VM.

        Args:
            task_ids_json: JSON string of task IDs for this worker.
            max_steps: Maximum steps per task.
            agent: Agent to run (TODO: serialize agent config for remote execution).
        """
        # TODO: Serialize agent config and pass to remote worker
        # For now, workers use a default agent configuration
        _ = agent  # Reserved for agent serialization
        # WAA Docker image has client at /client (see Dockerfile-WinArena)
        # The run.py script is at /client/run.py (not a module, so use python run.py)
        return f"""
        cd /client && \
        python run.py \
            --task_ids '{task_ids_json}' \
            --max_steps {max_steps} \
            --output_dir /outputs
        """
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=60),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    def _submit_job_with_retry(
        self,
        compute_name: str,
        command: str,
        environment_variables: dict[str, str],
        display_name: str,
        timeout_hours: float = 4.0,
    ) -> str:
        """Submit job with retry logic and health checking.

        This method wraps the job submission with:
        1. Automatic retry on transient failures (3 attempts)
        2. Exponential backoff between retries
        3. Container startup health check

        Args:
            compute_name: Target compute instance.
            command: Command to run.
            environment_variables: Environment variables for the job.
            display_name: Job display name.
            timeout_hours: Maximum job duration in hours.

        Returns:
            Job name/ID.

        Raises:
            ContainerStartupTimeout: If container fails to start after retries.
            Exception: If job submission fails after all retries.
        """
        from openadapt_evals.benchmarks.health_checker import (
            ContainerHealthChecker,
            ContainerStartupTimeout,
        )

        # Submit the job
        job_name = self.ml_client.submit_job(
            compute_name=compute_name,
            command=command,
            environment_variables=environment_variables,
            display_name=display_name,
            timeout_hours=timeout_hours,
        )

        # Initialize health checker
        health_checker = ContainerHealthChecker(self.ml_client)

        # Wait for container to start (5-10 minute timeout)
        logger.info(f"Waiting for container to start (job: {job_name})...")
        container_started = health_checker.wait_for_container_start(
            job_name=job_name,
            timeout_seconds=600,  # 10 minutes
        )

        if not container_started:
            # Cancel the stuck job
            logger.warning(f"Container failed to start, canceling job {job_name}")
            try:
                self.ml_client.client.jobs.cancel(job_name)
            except Exception as e:
                logger.warning(f"Failed to cancel stuck job: {e}")

            raise ContainerStartupTimeout(
                f"Container failed to start for job {job_name} within 10 minutes"
            )

        logger.info(f"Container started successfully for job {job_name}")
        return job_name

    def _wait_and_collect_results(
        self,
        workers: list[WorkerState],
        on_worker_complete: Callable[[WorkerState], None] | None,
    ) -> list[BenchmarkResult]:
        """Wait for all workers and collect results."""
        all_results: list[BenchmarkResult] = []
        consecutive_errors: dict[int, int] = {}

        # Poll workers for completion
        pending_workers = [w for w in workers if w.status == "running"]

        while pending_workers:
            for worker in pending_workers[:]:
                try:
                    status = self.ml_client.get_compute_status(worker.compute_name)

                    # Check if job completed (simplified - real impl would check job status)
                    if status in ["Stopped", "Deallocated"]:
                        worker.status = "completed"
                        worker.end_time = time.time()

                        # Fetch results from blob storage
                        results = self._fetch_worker_results(worker)
                        worker.results = results
                        all_results.extend(results)

                        if on_worker_complete:
                            on_worker_complete(worker)

                        pending_workers.remove(worker)
                        logger.info(
                            f"Worker {worker.worker_id} completed: "
                            f"{len(results)} results"
                        )

                except NotImplementedError:
                    # Never swallow a stub: it would poll forever, and the
                    # alternative (returning what it has) is a partial result
                    # set reported as a complete run.
                    raise

                except Exception as e:
                    logger.warning(f"Error checking worker {worker.worker_id}: {e}")
                    consecutive_errors[worker.worker_id] = (
                        consecutive_errors.get(worker.worker_id, 0) + 1
                    )
                    if consecutive_errors[worker.worker_id] >= _MAX_WORKER_POLL_ERRORS:
                        # A worker we can no longer reach has NOT produced zero
                        # results; it has produced none. Fail the run instead of
                        # polling it forever or dropping it silently from the
                        # denominator.
                        worker.status = "failed"
                        worker.error = str(e)
                        pending_workers.remove(worker)
                        raise RuntimeError(
                            f"Worker {worker.worker_id} "
                            f"({worker.compute_name}) could not be polled "
                            f"{_MAX_WORKER_POLL_ERRORS} times in a row; its "
                            f"{len(worker.assigned_tasks)} tasks were not "
                            f"scored. Last error: {e}"
                        ) from e
                else:
                    consecutive_errors[worker.worker_id] = 0

            if pending_workers:
                time.sleep(30)

        return all_results

    def _fetch_worker_results(self, worker: WorkerState) -> list[BenchmarkResult]:
        """Fetch results from a worker's output storage.

        Not implemented. This used to synthesize one
        ``BenchmarkResult(success=False, score=0.0)`` per assigned task, with
        no ``error``/``error_type``/``reason``, which meant a distributed run
        that never touched a VM produced a complete, legitimate-looking
        ``Success rate: 0.0%`` that ``compute_metrics`` could not even filter
        out as infrastructure. A stub must refuse to produce numbers -- see
        ``harness/external.py``, which raises for exactly this reason.

        Raises:
            NotImplementedError: Always, until blob-storage retrieval lands.
        """
        raise NotImplementedError(
            "AzureWAAOrchestrator cannot retrieve worker results yet: "
            "downloading them from blob storage is unimplemented. Refusing to "
            f"fabricate {len(worker.assigned_tasks)} zero-score results for "
            f"worker {worker.worker_id}, which would be published as a real "
            "0% benchmark run."
        )

    def _cleanup_workers(self, workers: list[WorkerState]) -> None:
        """Delete all worker VMs."""
        logger.info("Cleaning up worker VMs...")
        with ThreadPoolExecutor(max_workers=len(workers)) as executor:
            futures = [
                executor.submit(self.ml_client.delete_compute_instance, w.compute_name)
                for w in workers
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logger.warning(f"Cleanup error: {e}")

    def get_run_status(self) -> dict | None:
        """Get current run status."""
        if self._current_run is None:
            return None
        return self._current_run.to_dict()

    def cancel_run(self) -> None:
        """Cancel the current run and cleanup resources."""
        if self._current_run is None:
            return

        logger.info("Canceling evaluation run...")
        self._cleanup_workers(self._current_run.workers)
        self._current_run.status = "canceled"
        self._current_run.end_time = time.time()

    def monitor_job(
        self,
        job_name: str,
        live_tracking_file: str = "benchmark_live.json",
    ) -> None:
        """Monitor an existing Azure ML job with live tracking.

        This connects to a running job and streams its logs, updating
        the live tracking file in real-time for viewer integration.

        Args:
            job_name: Azure ML job name to monitor.
            live_tracking_file: Path to write live tracking data.
        """
        from openadapt_evals.benchmarks.live_tracker import LiveEvaluationTracker

        # Initialize live tracker
        tracker = LiveEvaluationTracker(
            output_file=live_tracking_file,
            total_tasks=0,  # Will be updated from logs
        )

        # Initialize log parser
        parser = AzureJobLogParser()

        # Create a mock task for current progress
        current_task = None

        def on_log_line(line: str):
            nonlocal current_task

            # Parse the log line
            parsed = parser.parse_line(line)

            if parsed is None:
                return

            # Handle different event types
            if parsed["type"] == "task_start":
                # Update total tasks if we learned it
                if parsed["total_tasks"] > tracker.total_tasks:
                    tracker.total_tasks = parsed["total_tasks"]

                # Start tracking this task
                from openadapt_evals.adapters import BenchmarkTask

                current_task = BenchmarkTask(
                    task_id=parsed["task_id"],
                    instruction=f"Azure ML Task {parsed['task_id']}",
                    domain="azure",
                )
                tracker.start_task(current_task)
                logger.info(
                    f"Task {parsed['task_idx']}/{parsed['total_tasks']}: {parsed['task_id']}"
                )

            elif parsed["type"] == "step" and current_task:
                # Record step
                from openadapt_evals.adapters import BenchmarkAction, BenchmarkObservation

                obs = BenchmarkObservation(screenshot=None)
                action = BenchmarkAction(type=parsed["action_type"].lower())

                tracker.record_step(
                    step_idx=parsed["step_idx"],
                    observation=obs,
                    action=action,
                    reasoning=None,
                )
                logger.info(f"  Step {parsed['step_idx']}: {parsed['action_type']}")

            elif parsed["type"] == "task_result" and current_task:
                # Finish tracking this task
                from openadapt_evals.adapters import BenchmarkResult

                result = BenchmarkResult(
                    task_id=parsed["task_id"],
                    success=parsed["success"],
                    score=1.0 if parsed["success"] else 0.0,
                    num_steps=parser.current_step,
                )
                tracker.finish_task(result)
                status = "SUCCESS" if parsed["success"] else "FAIL"
                logger.info(f"Task {parsed['task_id']}: {status}")
                current_task = None

            elif parsed["type"] == "error":
                logger.warning(f"Error in job: {parsed['message']}")

        # Start streaming logs
        logger.info(f"Monitoring Azure ML job: {job_name}")
        logger.info(f"Live tracking file: {live_tracking_file}")

        stream_process = self.ml_client.stream_job_logs(
            job_name=job_name,
            on_log_line=on_log_line,
        )

        try:
            # Wait for job to complete or user interrupt
            stream_process.wait()
            logger.info("Job monitoring complete")
            tracker.finish()
        except KeyboardInterrupt:
            logger.info("Monitoring interrupted by user")
            stream_process.terminate()
            tracker.finish()
        except Exception as e:
            logger.error(f"Error monitoring job: {e}")
            stream_process.terminate()
            tracker.finish()
            raise


def estimate_cost(
    num_tasks: int = 154,
    num_workers: int = 1,
    avg_task_duration_minutes: float = 1.0,
    vm_hourly_cost: float = 0.19,  # Standard_D4_v3 in East US (free trial compatible)
) -> dict:
    """Estimate Azure costs for a WAA evaluation run.

    Args:
        num_tasks: Number of tasks to run.
        num_workers: Number of parallel VMs (default: 1 for free trial).
        avg_task_duration_minutes: Average time per task.
        vm_hourly_cost: Hourly cost per VM (D4_v3 = $0.19/hr, D8_v3 = $0.38/hr).

    Returns:
        Dict with cost estimates.
    """
    tasks_per_worker = num_tasks / num_workers
    total_minutes = tasks_per_worker * avg_task_duration_minutes
    total_hours = total_minutes / 60

    # Add overhead for provisioning/cleanup
    overhead_hours = 0.25  # ~15 minutes

    vm_hours = (total_hours + overhead_hours) * num_workers
    total_cost = vm_hours * vm_hourly_cost

    return {
        "num_tasks": num_tasks,
        "num_workers": num_workers,
        "tasks_per_worker": tasks_per_worker,
        "estimated_duration_minutes": total_minutes + (overhead_hours * 60),
        "total_vm_hours": vm_hours,
        "estimated_cost_usd": total_cost,
        "cost_per_task_usd": total_cost / num_tasks,
    }
