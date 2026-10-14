# Azure ML Long-Term Solution for Reliable WAA Evaluations

**Date**: 2026-01-18
**Status**: Comprehensive Architecture Review
**Priority**: P0 - Critical Infrastructure

---

## Executive Summary

The Azure ML evaluation infrastructure for Windows Agent Arena (WAA) benchmarks has experienced reliability issues, with jobs getting stuck in "Running" state without executing tasks. This document provides root cause analysis, architectural recommendations, and a prioritized implementation plan for long-term reliability.

**Key Recommendations:**
1. **Pre-provisioned compute clusters** instead of on-demand compute instances
2. **Health monitoring with automatic retries** and circuit breakers
3. **Alternative execution environments** (Azure Batch, VMs with SSH)
4. **Comprehensive observability** with Azure Monitor integration
5. **Cost optimization** through reserved capacity and spot instances

---

## Table of Contents

1. [Root Cause Analysis](#1-root-cause-analysis)
2. [Current Architecture Issues](#2-current-architecture-issues)
3. [Recommended Architecture](#3-recommended-architecture)
4. [Configuration Best Practices](#4-configuration-best-practices)
5. [Monitoring and Alerting Strategy](#5-monitoring-and-alerting-strategy)
6. [Cost Optimization](#6-cost-optimization)
7. [Implementation Plan](#7-implementation-plan)
8. [Alternative Approaches](#8-alternative-approaches)

---

## 1. Root Cause Analysis

### 1.1 Failed Job Details

**Job ID**: `waa-waa3718w0-1768743963-20a88242`
**Compute**: `waa3718w0` (Standard_D4_v3)
**Status**: Stuck in "Running" for 8+ hours, then cancelled
**Tasks Completed**: 0/13
**Docker Image**: `windowsarena/winarena:latest`

### 1.2 Root Causes Identified

#### Primary Cause: Container Startup Failure
The Docker container never started successfully on the compute instance, despite the instance reaching "Running" state.

**Evidence:**
- Only 3 log messages captured (all Azure ML initialization)
- No Docker container startup logs
- No WAA client execution logs
- Compute instance shows "Running" but job has no progress

**Likely Sub-Causes:**
1. **Docker Image Pull Issues**
   - Large image size (~5GB Windows container) causing timeout
   - Network connectivity issues between Azure ML and Docker Hub
   - Rate limiting on Docker Hub (especially for large images)

2. **Container Setup Task Failure**
   - Container setup can fail silently in Azure ML
   - Permission issues executing containerSetup task
   - Missing dependencies or incompatible base image

3. **Nested Virtualization Issues**
   - Windows containers require nested virtualization
   - Standard_D4_v3 supports nested virtualization BUT...
   - "Trusted Launch" security type (Azure default since 2024) may disable nested virtualization
   - Generation 1 vs Generation 2 VM compatibility issues

4. **Silent Failure Mode**
   - Azure ML doesn't always report container startup failures
   - Job stays in "Running" state indefinitely
   - No automatic timeout on container initialization

#### Secondary Causes

**Infrastructure Configuration:**
- On-demand compute instances have slow provisioning (3-5 minutes)
- No health checks between compute provisioning and job execution
- No retry logic for container startup failures
- No monitoring of Docker daemon or container health

**Observability Gaps:**
- Limited visibility into container lifecycle
- Log streaming only shows job logs, not infrastructure logs
- No metrics on Docker pull progress or container startup time
- No alerts for stuck jobs

**Resource Management:**
- Compute instances allocated but not properly utilized
- No cleanup of failed containers
- Potential disk space exhaustion from failed image pulls

### 1.3 Why It Wasn't Detected Earlier

1. **No baseline reliability data** - First large-scale Azure run
2. **No health checks** - Job submission succeeded, assumed running
3. **No timeout alerts** - Job can run indefinitely without progress
4. **No container startup monitoring** - Can't see Docker daemon status

---

## 2. Current Architecture Issues

### 2.1 Compute Instance Model

**Current Approach:**
```python
# On-demand compute instance creation
compute = ComputeInstance(
    name=f"waa{worker_id}",
    size="Standard_D4_v3",
    idle_time_before_shutdown_minutes=60,
)
client.compute.begin_create_or_update(compute).result()
```

**Problems:**
- ❌ 3-5 minute provisioning time per worker
- ❌ No health verification before job submission
- ❌ No retry logic for provisioning failures
- ❌ Single point of failure per worker
- ❌ Difficult to debug container startup issues
- ❌ "Trusted Launch" security type may break nested virtualization

### 2.2 Job Execution Model

**Current Approach:**
```python
job = ml_command(
    command=command,
    environment=Environment(image="windowsarena/winarena:latest"),
    compute=compute_name,
    limits=CommandJobLimits(timeout=14400),  # 4 hours
)
```

**Problems:**
- ❌ No container startup timeout (only job timeout)
- ❌ Docker image pulled on every execution
- ❌ No health check before/after container start
- ❌ Silent failures in container setup
- ❌ No progress monitoring during execution
- ❌ Limited observability into Docker daemon

### 2.3 Monitoring and Observability

**Current Approach:**
```python
# Poll job status every 30 seconds
process = subprocess.Popen(["az", "ml", "job", "stream", ...])
```

**Problems:**
- ❌ Only sees job logs, not infrastructure logs
- ❌ No metrics on container health
- ❌ No alerts for stuck jobs
- ❌ No visibility into Docker pull progress
- ❌ Cannot detect silent failures
- ❌ No historical performance data

### 2.4 Error Handling

**Current Approach:**
- Wait indefinitely for job completion
- Manual cancellation required
- No automatic retry

**Problems:**
- ❌ No circuit breaker for stuck jobs
- ❌ No automatic retry on transient failures
- ❌ No exponential backoff
- ❌ No failure classification (transient vs. permanent)

---

## 3. Recommended Architecture

### 3.1 Multi-Tier Reliability Model

```
┌─────────────────────────────────────────────────────────┐
│                   Control Plane                          │
│  (Orchestrator with Circuit Breaker + Health Checks)    │
└────────────┬───────────────────────────┬────────────────┘
             │                           │
    ┌────────▼─────────┐       ┌────────▼─────────┐
    │  Tier 1: Fast    │       │  Tier 2: Robust  │
    │  Pre-provisioned │       │  Azure Batch     │
    │  Compute Cluster │       │  (Fallback)      │
    └────────┬─────────┘       └────────┬─────────┘
             │                           │
    ┌────────▼──────────────────────────▼─────────┐
    │          Tier 3: Direct VM (Debug)          │
    │          SSH-based execution                │
    └─────────────────────────────────────────────┘
```

### 3.2 Tier 1: Pre-Provisioned Compute Cluster (Primary)

**Architecture:**
```python
from azure.ai.ml.entities import AmlCompute

# Create persistent compute cluster
compute = AmlCompute(
    name="waa-cluster",
    type="amlcompute",
    size="Standard_D4_v3",
    min_instances=0,        # Scale to zero when idle
    max_instances=10,       # Scale up as needed
    idle_time_before_scale_down=600,  # 10 minutes
    tier="Standard",        # NOT "Trusted Launch"
)
```

**Benefits:**
- ✅ Pre-validated compute nodes (no on-demand provisioning)
- ✅ Docker images pre-pulled and cached
- ✅ Auto-scaling based on workload
- ✅ Cost-effective (scale to zero when idle)
- ✅ Better reliability than on-demand instances
- ✅ Supports nested virtualization (Standard tier)

**Implementation:**
```python
class AzureWAAClusterOrchestrator:
    """Cluster-based orchestrator with health checks."""

    def __init__(self, config: AzureConfig):
        self.config = config
        self.ml_client = AzureMLClient(config)
        self.health_checker = ClusterHealthChecker(self.ml_client)

    def ensure_cluster_ready(self, num_nodes: int) -> bool:
        """Ensure cluster is provisioned and healthy."""
        # Create or get cluster
        cluster = self.ml_client.get_or_create_cluster(
            name="waa-cluster",
            min_instances=0,
            max_instances=num_nodes,
        )

        # Wait for cluster to reach min capacity
        self.health_checker.wait_for_nodes(num_nodes, timeout=600)

        # Verify Docker images are cached
        self.health_checker.verify_docker_images(["windowsarena/winarena:latest"])

        return True

    def submit_job_with_retry(
        self,
        command: str,
        max_retries: int = 3,
    ) -> str:
        """Submit job with exponential backoff retry."""
        for attempt in range(max_retries):
            try:
                job_name = self.ml_client.submit_job(
                    compute_name="waa-cluster",
                    command=command,
                    environment_variables={...},
                )

                # Health check: Verify container started within 5 minutes
                if self.health_checker.wait_for_container_start(job_name, timeout=300):
                    return job_name
                else:
                    raise ContainerStartupTimeout(f"Job {job_name} failed to start")

            except (ContainerStartupTimeout, ContainerSetupError) as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff
                    logger.warning(f"Retry {attempt+1}/{max_retries} after {wait_time}s: {e}")
                    time.sleep(wait_time)
                else:
                    raise
```

### 3.3 Tier 2: Azure Batch (Fallback)

**When to Use:**
- Azure ML cluster exhausted
- Persistent Azure ML issues
- Need lower-level control

**Architecture:**
```python
from azure.batch import BatchServiceClient
from azure.batch.models import (
    PoolAddParameter,
    TaskAddParameter,
    ContainerConfiguration,
)

class AzureBatchWAAOrchestrator:
    """Fallback to Azure Batch for higher reliability."""

    def create_pool(self, num_nodes: int):
        """Create Batch pool with pre-configured containers."""
        pool = PoolAddParameter(
            id="waa-pool",
            vm_size="Standard_D4_v3",
            target_dedicated_nodes=num_nodes,
            container_configuration=ContainerConfiguration(
                container_image_names=["windowsarena/winarena:latest"],
                type="docker",
            ),
            # CRITICAL: Use Standard VM generation for nested virtualization
            virtual_machine_configuration={
                "image_reference": {
                    "publisher": "Canonical",
                    "offer": "UbuntuServer",
                    "sku": "18.04-LTS",
                },
                "node_agent_sku_id": "batch.node.ubuntu 18.04",
                "container_configuration": container_config,
            },
        )

        self.batch_client.pool.add(pool)
```

**Benefits:**
- ✅ Lower-level control than Azure ML
- ✅ Better container lifecycle management
- ✅ More detailed error reporting
- ✅ Proven reliability for batch workloads

### 3.4 Tier 3: Direct VM with SSH (Debug/Development)

**When to Use:**
- Debugging container startup issues
- Development and testing
- Azure ML/Batch both failing

**Architecture:**
```python
class DirectVMOrchestrator:
    """SSH-based execution for debugging and development."""

    def run_task_on_vm(self, vm_ip: str, task_id: str) -> BenchmarkResult:
        """Execute task directly via SSH for debugging."""
        ssh_client = paramiko.SSHClient()
        ssh_client.connect(vm_ip, username="azureuser", key_filename="~/.ssh/id_rsa")

        # Execute with real-time logging
        stdin, stdout, stderr = ssh_client.exec_command(
            f"docker run --rm "
            f"-e TASK_ID={task_id} "
            f"windowsarena/winarena:latest "
            f"python /client/run.py --task_ids '{task_id}'"
        )

        # Stream logs in real-time
        for line in stdout:
            logger.info(f"[{task_id}] {line.strip()}")

        # Parse results
        exit_code = stdout.channel.recv_exit_status()
        return self.parse_result(exit_code, stdout, stderr)
```

### 3.5 Health Monitoring System

```python
class ClusterHealthChecker:
    """Comprehensive health checking for compute infrastructure."""

    def wait_for_container_start(
        self,
        job_name: str,
        timeout: int = 300,
    ) -> bool:
        """Wait for container to start, return False if timeout."""
        start_time = time.time()

        while time.time() - start_time < timeout:
            # Check for container startup logs
            logs = self.ml_client.get_job_logs(job_name)

            if "Container started" in logs or "Executing task" in logs:
                return True

            if "Container setup failed" in logs or "Docker pull failed" in logs:
                raise ContainerSetupError(f"Container startup failed: {logs}")

            time.sleep(10)

        return False

    def verify_docker_images(self, images: list[str]) -> bool:
        """Verify Docker images are cached on cluster nodes."""
        # Query cluster nodes for cached images
        for node in self.get_cluster_nodes():
            cached_images = self.get_node_docker_images(node)

            for image in images:
                if image not in cached_images:
                    logger.warning(f"Image {image} not cached on {node}")
                    return False

        return True

    def get_node_docker_images(self, node_id: str) -> list[str]:
        """Get list of Docker images on a cluster node."""
        # Use Azure ML diagnostic API to query node
        # This requires Azure ML SDK v2 diagnostics extension
        return self.ml_client.diagnostics.list_docker_images(node_id)
```

---

## 4. Configuration Best Practices

### 4.1 VM Configuration

**Critical: Ensure Nested Virtualization Support**

```python
@dataclass
class AzureConfig:
    vm_size: str = "Standard_D4_v3"

    # CRITICAL: Security type for nested virtualization
    vm_security_type: str = "Standard"  # NOT "TrustedLaunch"

    # VM generation (Gen2 supports nested virt better)
    vm_generation: int = 2

    # Enable nested virtualization explicitly
    enable_nested_virtualization: bool = True
```

**VM Size Selection:**

| VM Size | vCPUs | RAM | Nested Virt | Cost/Hour | Best For |
|---------|-------|-----|-------------|-----------|----------|
| Standard_D2_v3 | 2 | 8 GB | ✅ Yes | $0.096 | Development |
| Standard_D4_v3 | 4 | 16 GB | ✅ Yes | $0.192 | Production (current) |
| Standard_D8_v3 | 8 | 32 GB | ✅ Yes | $0.384 | High-load tasks |
| Standard_E2s_v3 | 2 | 16 GB | ✅ Yes | $0.126 | Memory-optimized |
| Standard_D4s_v5 | 4 | 16 GB | ✅ Yes | $0.208 | Latest generation |

**Recommendation:** Use `Standard_D4s_v5` for better reliability and performance (v5 series has better nested virtualization support).

### 4.2 Docker Image Configuration

**Option A: Pre-Pull Images to Azure Container Registry (Recommended)**

```python
# Step 1: Push image to ACR
# az acr import --name myacr --source windowsarena/winarena:latest \
#   --image winarena:latest

# Step 2: Configure environment
from azure.ai.ml.entities import Environment

env = Environment(
    name="waa-agent-env",
    image="myacr.azurecr.io/winarena:latest",  # ACR instead of Docker Hub
    description="Pre-pulled Windows Arena image",
)

# Step 3: Pre-build environment (caches image)
ml_client.environments.create_or_update(env)
```

**Benefits:**
- ✅ Faster image pulls (Azure network vs. Internet)
- ✅ No Docker Hub rate limits
- ✅ Better reliability (Azure internal network)
- ✅ Image versioning and governance
- ✅ Can scan for vulnerabilities

**Option B: Use Docker Hub with Retry Logic**

```python
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    retry=retry_if_exception_type(DockerPullError),
)
def create_environment_with_retry():
    """Create environment with retry on Docker pull failures."""
    env = Environment(
        image="windowsarena/winarena:latest",
        name="waa-agent-env",
    )

    try:
        ml_client.environments.create_or_update(env)
    except Exception as e:
        if "failed to pull image" in str(e).lower():
            raise DockerPullError(e)
        raise
```

### 4.3 Job Configuration

**Comprehensive Job Limits and Timeouts:**

```python
from azure.ai.ml.entities import CommandJobLimits

limits = CommandJobLimits(
    timeout=14400,  # 4 hours max runtime

    # NEW: Container startup timeout
    container_startup_timeout=600,  # 10 minutes to start

    # NEW: Progress monitoring
    communication_timeout=600,  # 10 minutes without logs = failure

    # Retry configuration
    max_retries=3,
    retry_backoff_multiplier=2,
)

job = ml_command(
    command=command,
    environment=env,
    compute=compute_name,
    limits=limits,
)
```

### 4.4 Environment Variables for Debugging

```python
environment_variables = {
    "WAA_TASK_IDS": task_ids_json,
    "WAA_MAX_STEPS": str(max_steps),

    # Debugging flags
    "DOCKER_VERBOSE": "1",
    "AZURE_ML_DEBUG": "1",
    "PYTHONUNBUFFERED": "1",  # Immediate log output

    # Monitoring
    "HEARTBEAT_INTERVAL": "30",  # Send heartbeat every 30s
    "LOG_LEVEL": "DEBUG",
}
```

---

## 5. Monitoring and Alerting Strategy

### 5.1 Multi-Layer Monitoring

```
┌─────────────────────────────────────────────────────┐
│  Layer 1: Infrastructure Monitoring (Azure Monitor) │
│  - VM health, CPU, memory, disk                     │
│  - Docker daemon status                             │
│  - Network connectivity                             │
└────────────┬────────────────────────────────────────┘
             │
┌────────────▼────────────────────────────────────────┐
│  Layer 2: Job Lifecycle Monitoring                  │
│  - Job submission → running → completion            │
│  - Container startup time                           │
│  - Task execution progress                          │
└────────────┬────────────────────────────────────────┘
             │
┌────────────▼────────────────────────────────────────┐
│  Layer 3: Application Monitoring (WAA tasks)        │
│  - Task success/failure rates                       │
│  - Step execution times                             │
│  - Action success rates                             │
└─────────────────────────────────────────────────────┘
```

### 5.2 Azure Monitor Integration

```python
from azure.monitor.query import MetricsQueryClient

class AzureMonitoringService:
    """Integrate Azure Monitor for infrastructure metrics."""

    def __init__(self, subscription_id: str):
        self.metrics_client = MetricsQueryClient(credential)
        self.subscription_id = subscription_id

    def get_compute_metrics(self, compute_name: str) -> dict:
        """Get CPU, memory, disk metrics for compute instance."""
        resource_id = (
            f"/subscriptions/{self.subscription_id}"
            f"/resourceGroups/{resource_group}"
            f"/providers/Microsoft.MachineLearningServices"
            f"/workspaces/{workspace}"
            f"/computes/{compute_name}"
        )

        metrics = self.metrics_client.query_resource(
            resource_id,
            metric_names=["CpuUtilization", "MemoryUtilization", "DiskUtilization"],
            timespan=timedelta(hours=1),
        )

        return {
            "cpu": metrics.metrics[0].timeseries[0].data[-1].average,
            "memory": metrics.metrics[1].timeseries[0].data[-1].average,
            "disk": metrics.metrics[2].timeseries[0].data[-1].average,
        }

    def check_job_health(self, job_name: str) -> bool:
        """Check if job is healthy (making progress)."""
        # Get job logs from last 5 minutes
        recent_logs = self.get_job_logs(job_name, since=timedelta(minutes=5))

        # Job is healthy if:
        # 1. Logs are being produced
        # 2. No error patterns
        # 3. Progress indicators present

        if not recent_logs:
            logger.warning(f"Job {job_name} has no logs in last 5 minutes")
            return False

        if any(pattern in recent_logs for pattern in ["Error", "Exception", "Failed"]):
            logger.error(f"Job {job_name} has errors in recent logs")
            return False

        return True
```

### 5.3 Alerting Rules

**Critical Alerts (Immediate Action Required):**

```python
alerts = [
    {
        "name": "job_stuck_no_progress",
        "condition": "No logs for 10 minutes AND job status = Running",
        "action": "Cancel job, retry on different node",
        "priority": "critical",
    },
    {
        "name": "container_startup_timeout",
        "condition": "Job running for >10 minutes with no container startup logs",
        "action": "Cancel job, investigate Docker image",
        "priority": "critical",
    },
    {
        "name": "compute_node_unhealthy",
        "condition": "CPU/Memory/Disk >95% for >5 minutes",
        "action": "Mark node unhealthy, drain and restart",
        "priority": "critical",
    },
]
```

**Warning Alerts (Monitor and Escalate):**

```python
warnings = [
    {
        "name": "slow_task_execution",
        "condition": "Task taking >2x average execution time",
        "action": "Log for investigation, don't cancel",
        "priority": "warning",
    },
    {
        "name": "high_retry_rate",
        "condition": ">20% of jobs requiring retries",
        "action": "Investigate infrastructure issues",
        "priority": "warning",
    },
    {
        "name": "docker_pull_slow",
        "condition": "Docker image pull taking >5 minutes",
        "action": "Consider switching to ACR",
        "priority": "warning",
    },
]
```

### 5.4 Real-Time Monitoring Dashboard

**Enhanced Live Monitoring:**

```python
class EnhancedLiveTracker:
    """Enhanced monitoring with infrastructure metrics."""

    def __init__(self, output_file: str):
        self.output_file = output_file
        self.monitor = AzureMonitoringService(subscription_id)

    def update_dashboard(self):
        """Update dashboard with infrastructure + job metrics."""
        data = {
            "timestamp": datetime.now().isoformat(),
            "status": self.get_overall_status(),
            "tasks": self.get_task_progress(),

            # NEW: Infrastructure metrics
            "infrastructure": {
                "compute_health": self.monitor.get_compute_metrics("waa-cluster"),
                "active_nodes": self.get_active_nodes(),
                "pending_jobs": self.get_pending_jobs(),
            },

            # NEW: Performance metrics
            "performance": {
                "avg_task_duration": self.calculate_avg_task_duration(),
                "task_success_rate": self.calculate_success_rate(),
                "retry_rate": self.calculate_retry_rate(),
            },

            # NEW: Cost tracking
            "cost": {
                "current_hourly_rate": self.calculate_current_cost(),
                "estimated_total": self.estimate_total_cost(),
            },
        }

        # Write to file for viewer
        with open(self.output_file, "w") as f:
            json.dump(data, f, indent=2)
```

---

## 6. Cost Optimization

### 6.1 Current Cost Baseline

**Single Job (13 tasks, 1 worker):**
- VM: Standard_D4_v3 @ $0.192/hour
- Expected runtime: 1-2 hours (13 tasks × 5-10 min/task)
- Expected cost: **$0.38 - $0.77**

**Failed Job Cost:**
- Runtime: 8 hours (stuck)
- Actual cost: **$1.54** (no useful output)

**Full Evaluation (154 tasks, 10 workers):**
- VMs: 10 × Standard_D4_v3 @ $0.192/hour
- Expected runtime: 2-4 hours
- Expected cost: **$3.84 - $7.68**

**If jobs frequently fail:**
- Average 2 retries per evaluation
- Cost: **$11.52 - $23.04** (3× baseline)

### 6.2 Optimization Strategies

#### Strategy 1: Right-Size VMs

**Current:** Standard_D4_v3 (4 vCPUs, 16 GB)
**Recommended:** Standard_D2_v3 for most tasks (2 vCPUs, 8 GB)

```python
# Task complexity analysis
task_profiles = {
    "simple": ["notepad_*", "settings_*"],  # 2 vCPU sufficient
    "medium": ["file_explorer_*", "chrome_*"],  # 2-4 vCPU
    "complex": ["libreoffice_*", "coding_*"],  # 4 vCPU
}

def select_vm_size(task_id: str) -> str:
    """Select VM size based on task complexity."""
    if any(task_id.startswith(prefix.replace("_*", "")) for prefix in task_profiles["simple"]):
        return "Standard_D2_v3"  # $0.096/hour (50% savings)
    elif any(task_id.startswith(prefix.replace("_*", "")) for prefix in task_profiles["complex"]):
        return "Standard_D4_v3"  # $0.192/hour
    else:
        return "Standard_D2_v3"  # Default to smaller
```

**Savings:** ~40-50% on VM costs

#### Strategy 2: Use Spot Instances

```python
compute = AmlCompute(
    name="waa-cluster-spot",
    size="Standard_D4_v3",
    tier="LowPriority",  # Spot instances
    max_instances=10,
    eviction_policy="Deallocate",
)
```

**Benefits:**
- ✅ 60-90% cost savings vs. standard instances
- ⚠️ Can be evicted (need retry logic)
- ✅ Good for batch workloads with retries

**Recommendation:** Use spot for development/testing, standard for production

#### Strategy 3: Reserved Capacity

For regular evaluations (weekly/monthly):

```python
# Purchase reserved instance (1 or 3 year commitment)
# Azure Portal → Reservations → Machine Learning
```

**Savings:**
- 1-year: ~25-30% discount
- 3-year: ~40-50% discount

**Use case:** Running evaluations weekly or more frequently

#### Strategy 4: Scale to Zero When Idle

```python
compute = AmlCompute(
    name="waa-cluster",
    size="Standard_D4_v3",
    min_instances=0,  # Scale to zero when idle
    max_instances=10,
    idle_time_before_scale_down=300,  # 5 minutes (faster than default 10)
)
```

**Savings:** Zero cost when not running evaluations

#### Strategy 5: Parallel Execution Optimization

**Current:** 10 workers × 2-4 hours = 20-40 VM-hours
**Optimized:** Dynamic worker allocation based on queue depth

```python
def calculate_optimal_workers(num_tasks: int, target_duration_hours: float = 2) -> int:
    """Calculate optimal number of workers for target duration."""
    avg_task_duration_minutes = 8  # Historical average
    total_task_minutes = num_tasks * avg_task_duration_minutes
    target_minutes = target_duration_hours * 60

    # Optimal workers = total work / target duration
    optimal_workers = math.ceil(total_task_minutes / target_minutes)

    # Cap at quota limit
    return min(optimal_workers, 10)

# Example: 154 tasks × 8 min = 1232 min
# Target 2 hours (120 min) → 1232/120 = 10.3 workers
# Use 10 workers (at quota limit)
```

### 6.3 Cost Monitoring

```python
class CostTracker:
    """Track and report costs in real-time."""

    def __init__(self, vm_size: str = "Standard_D4_v3"):
        self.hourly_rate = self.get_vm_hourly_rate(vm_size)
        self.start_time = time.time()

    def get_current_cost(self) -> float:
        """Calculate current cost based on runtime."""
        hours = (time.time() - self.start_time) / 3600
        return hours * self.hourly_rate

    def estimate_remaining_cost(self, tasks_completed: int, total_tasks: int) -> float:
        """Estimate remaining cost based on progress."""
        if tasks_completed == 0:
            return float('inf')  # Can't estimate

        avg_time_per_task = (time.time() - self.start_time) / tasks_completed
        remaining_tasks = total_tasks - tasks_completed
        estimated_hours = (remaining_tasks * avg_time_per_task) / 3600

        return estimated_hours * self.hourly_rate

    def get_cost_summary(self) -> dict:
        """Get comprehensive cost summary."""
        return {
            "current_cost_usd": round(self.get_current_cost(), 2),
            "estimated_total_usd": round(
                self.get_current_cost() + self.estimate_remaining_cost(...), 2
            ),
            "cost_per_task_usd": round(
                self.get_current_cost() / max(tasks_completed, 1), 3
            ),
        }
```

### 6.4 Cost Optimization Recommendations

**Short-term (Immediate):**
1. ✅ Scale to zero when idle (idle_time_before_scale_down=300)
2. ✅ Right-size VMs (use D2_v3 for simple tasks)
3. ✅ Set aggressive timeouts (cancel stuck jobs early)

**Medium-term (1-2 months):**
4. ✅ Use spot instances for development (60-90% savings)
5. ✅ Pre-pull images to ACR (faster startup = less idle time)
6. ✅ Implement health checks (avoid wasted compute on stuck jobs)

**Long-term (3+ months):**
7. ✅ Purchase reserved capacity if running weekly (25-50% savings)
8. ✅ Optimize task execution (reduce avg task duration)
9. ✅ Use Azure Batch for better cost efficiency

**Expected Savings:**
- Baseline: $7.68 per full evaluation (154 tasks, 10 workers, 4 hours)
- With optimizations: **$2.50 - $4.00** (50-67% reduction)

---

## 7. Implementation Plan

### Phase 1: Immediate Fixes (Week 1) - **P0**

**Goal:** Get evaluations running reliably

**Tasks:**

1. **Fix Nested Virtualization Issue** [2 hours]
   - Update `azure.py` to specify `vm_security_type="Standard"` (not TrustedLaunch)
   - Update `azure.py` to use `Standard_D4s_v5` (better nested virt support)
   - Test with single-task evaluation

2. **Add Container Startup Health Check** [4 hours]
   ```python
   class ClusterHealthChecker:
       def wait_for_container_start(self, job_name: str, timeout: int = 300):
           # Poll for container startup logs
           # Raise ContainerStartupTimeout if timeout exceeded
   ```

3. **Implement Job Retry Logic** [3 hours]
   ```python
   @retry(stop=stop_after_attempt(3), wait=wait_exponential(...))
   def submit_job_with_retry(command: str):
       # Submit job
       # Verify container starts
       # Retry on transient failures
   ```

4. **Add Stuck Job Detection** [2 hours]
   ```python
   def monitor_job_progress(job_name: str):
       # If no logs for 10 minutes → cancel and retry
   ```

**Deliverables:**
- ✅ Updated `azure.py` with VM configuration fixes
- ✅ `health_checker.py` module with container startup verification
- ✅ Retry logic integrated into `AzureWAAOrchestrator`
- ✅ Stuck job detection in monitoring loop

**Success Criteria:**
- Single-task evaluation completes successfully
- 10-task evaluation completes successfully
- No jobs stuck for >15 minutes

**Estimated Effort:** 11 hours (1-2 days)

### Phase 2: Observability and Monitoring (Week 2) - **P1**

**Goal:** Gain visibility into infrastructure and job health

**Tasks:**

1. **Integrate Azure Monitor** [6 hours]
   - Collect VM metrics (CPU, memory, disk)
   - Track job lifecycle events
   - Set up alerts for stuck jobs

2. **Enhanced Live Dashboard** [4 hours]
   - Add infrastructure health metrics
   - Add cost tracking
   - Add performance metrics (avg task duration, success rate)

3. **Alerting System** [4 hours]
   - Critical alerts (stuck jobs, unhealthy nodes)
   - Warning alerts (slow execution, high retry rate)
   - Email/Slack integration

**Deliverables:**
- ✅ `monitoring.py` with Azure Monitor integration
- ✅ Enhanced `LiveEvaluationTracker` with infrastructure metrics
- ✅ Alerting system with critical/warning rules
- ✅ Updated viewer HTML with new metrics

**Success Criteria:**
- Real-time infrastructure metrics visible in dashboard
- Alerts trigger within 5 minutes of issues
- Cost tracking accurate within 5%

**Estimated Effort:** 14 hours (2-3 days)

### Phase 3: Cluster-Based Architecture (Week 3-4) - **P1**

**Goal:** Move from on-demand instances to pre-provisioned cluster

**Tasks:**

1. **Implement Cluster Orchestrator** [8 hours]
   ```python
   class AzureWAAClusterOrchestrator:
       def ensure_cluster_ready(self, num_nodes: int):
           # Create or get cluster
           # Wait for nodes to be ready
           # Verify Docker images cached
   ```

2. **Cluster Health Management** [6 hours]
   - Node health checks
   - Docker image pre-pulling
   - Auto-scaling configuration

3. **Migration from Compute Instances** [4 hours]
   - Update CLI to use cluster orchestrator
   - Backward compatibility with old approach
   - Migration guide documentation

4. **Testing and Validation** [6 hours]
   - Test with 1, 10, 20, 40 tasks
   - Measure reliability improvement
   - Measure cost impact

**Deliverables:**
- ✅ `azure_cluster.py` with new orchestrator
- ✅ Cluster health management system
- ✅ Updated CLI with cluster support
- ✅ Migration documentation

**Success Criteria:**
- 95%+ job success rate (vs. current <50%)
- <5 minute cluster startup time
- Container startup within 2 minutes (vs. never starting)

**Estimated Effort:** 24 hours (1 week)

### Phase 4: Cost Optimization (Week 5) - **P2**

**Goal:** Reduce evaluation costs by 50%

**Tasks:**

1. **Right-Size VMs** [3 hours]
   - Implement task complexity-based VM selection
   - Test simple tasks on D2_v3

2. **Spot Instance Support** [4 hours]
   - Add spot instance configuration option
   - Implement eviction handling (retry logic)
   - Test with development workloads

3. **ACR Image Migration** [4 hours]
   - Push `windowsarena/winarena:latest` to ACR
   - Update environment to use ACR
   - Measure pull time improvement

4. **Cost Tracking Dashboard** [3 hours]
   - Real-time cost tracking
   - Cost per task metrics
   - Budget alerts

**Deliverables:**
- ✅ Dynamic VM sizing based on task complexity
- ✅ Spot instance support (opt-in)
- ✅ ACR-based Docker images
- ✅ Cost tracking in dashboard

**Success Criteria:**
- 40-50% cost reduction vs. baseline
- No reliability degradation
- <2 minute image pull time (vs. 5-10 minutes from Docker Hub)

**Estimated Effort:** 14 hours (2-3 days)

### Phase 5: Alternative Execution Environments (Week 6) - **P2**

**Goal:** Provide fallback options for Azure ML failures

**Tasks:**

1. **Azure Batch Orchestrator** [10 hours]
   - Implement `AzureBatchWAAOrchestrator`
   - Pool creation and management
   - Task submission and monitoring

2. **Direct VM Orchestrator** [6 hours]
   - Implement SSH-based execution
   - Real-time log streaming
   - Debugging tools

3. **Multi-Tier Fallback System** [4 hours]
   - Try Azure ML cluster (Tier 1)
   - Fall back to Azure Batch (Tier 2)
   - Fall back to direct VM (Tier 3)

**Deliverables:**
- ✅ `azure_batch.py` with Batch orchestrator
- ✅ `direct_vm.py` with SSH orchestrator
- ✅ Multi-tier fallback logic in main orchestrator

**Success Criteria:**
- Can complete evaluations even if Azure ML is down
- <10% performance overhead from fallback
- Transparent failover (user doesn't need to intervene)

**Estimated Effort:** 20 hours (1 week)

### Phase 6: Long-Term Improvements (Ongoing) - **P3**

**Tasks:**

1. **Performance Optimization** [Ongoing]
   - Reduce avg task duration (optimize agent prompts)
   - Parallel task execution within single job
   - Optimize Docker image size

2. **Advanced Monitoring** [4 hours]
   - Application performance monitoring (APM)
   - Distributed tracing for multi-worker jobs
   - Historical performance analytics

3. **Self-Healing Infrastructure** [8 hours]
   - Auto-restart failed nodes
   - Auto-rebalance tasks
   - Predictive failure detection

**Estimated Effort:** 12+ hours (ongoing)

---

## 8. Alternative Approaches

### 8.1 Azure Batch (Primary Alternative)

**Comparison to Azure ML:**

| Feature | Azure ML | Azure Batch |
|---------|----------|-------------|
| Ease of use | ✅ High (ML-focused) | ⚠️ Medium (lower-level) |
| Container support | ✅ Excellent | ✅ Excellent |
| Nested virtualization | ⚠️ Limited (TrustedLaunch issues) | ✅ Better control |
| Debugging | ⚠️ Limited logs | ✅ Detailed logs |
| Cost | ⚠️ Higher (managed service) | ✅ Lower (raw compute) |
| Reliability | ⚠️ Medium (current experience) | ✅ High (proven for batch) |

**When to Use Azure Batch:**
- Azure ML reliability issues persist
- Need lower-level control over compute
- Cost optimization is critical

**Implementation Complexity:** Medium (2-3 days)

### 8.2 Direct VM Pool with SSH

**Architecture:**
```python
class DirectVMPoolOrchestrator:
    """Manage pool of VMs with SSH-based task execution."""

    def __init__(self, num_vms: int = 10):
        self.vms = []
        for i in range(num_vms):
            vm = self.create_vm(f"waa-vm-{i}")
            self.vms.append(vm)

    def distribute_tasks(self, tasks: list[BenchmarkTask]):
        """Distribute tasks across VMs via SSH."""
        task_queue = queue.Queue()
        for task in tasks:
            task_queue.put(task)

        with ThreadPoolExecutor(max_workers=len(self.vms)) as executor:
            futures = [
                executor.submit(self.run_tasks_on_vm, vm, task_queue)
                for vm in self.vms
            ]

            for future in as_completed(futures):
                results = future.result()
                yield from results
```

**Pros:**
- ✅ Full control over execution environment
- ✅ Easy debugging (can SSH in)
- ✅ No Azure ML complexity
- ✅ Can use any VM size/configuration

**Cons:**
- ❌ Manual VM lifecycle management
- ❌ No auto-scaling
- ❌ More infrastructure code to maintain
- ❌ SSH key management

**When to Use:**
- Development and debugging
- Azure ML and Batch both failing
- Need maximum control

**Implementation Complexity:** Medium (3-4 days)

### 8.3 Kubernetes (AKS)

**Architecture:**
```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: waa-evaluation
spec:
  parallelism: 10
  completions: 154
  template:
    spec:
      containers:
      - name: waa-agent
        image: windowsarena/winarena:latest
        command: ["python", "/client/run.py"]
        env:
        - name: TASK_ID
          valueFrom:
            fieldRef:
              fieldPath: metadata.name
```

**Pros:**
- ✅ Industry-standard orchestration
- ✅ Excellent auto-scaling
- ✅ Rich ecosystem (monitoring, logging, etc.)
- ✅ Multi-cloud portability

**Cons:**
- ❌ High complexity (Kubernetes learning curve)
- ❌ Windows container support limited on AKS
- ❌ Nested virtualization challenges
- ❌ Significant infrastructure setup

**When to Use:**
- Already using Kubernetes
- Multi-cloud strategy
- Need advanced orchestration features

**Implementation Complexity:** High (2-3 weeks)

**Recommendation:** Not recommended unless already invested in Kubernetes

### 8.4 GitHub Actions Self-Hosted Runners

**Architecture:**
```yaml
name: WAA Evaluation
on: workflow_dispatch

jobs:
  evaluate:
    runs-on: [self-hosted, windows, waa]
    strategy:
      matrix:
        task_id: ${{ fromJson(inputs.task_ids) }}
    steps:
      - name: Run WAA Task
        run: |
          docker run windowsarena/winarena:latest \
            python /client/run.py --task_ids "${{ matrix.task_id }}"
```

**Pros:**
- ✅ Simple workflow definition
- ✅ Built-in parallelism (matrix strategy)
- ✅ Good for CI/CD integration
- ✅ Free if using self-hosted runners

**Cons:**
- ❌ Self-hosted runner management complexity
- ❌ Windows runner setup challenges
- ❌ Limited control over execution environment
- ❌ Primarily designed for CI/CD, not batch compute

**When to Use:**
- CI/CD integration needed
- Small-scale evaluations (<20 tasks)
- Already using GitHub Actions

**Implementation Complexity:** Medium (3-5 days including runner setup)

---

## Summary and Recommendations

### Critical Issues Identified

1. **Nested Virtualization:** "TrustedLaunch" security type disables nested virtualization
2. **Container Startup:** No health checks, jobs can get stuck indefinitely
3. **Observability:** Limited visibility into infrastructure and container lifecycle
4. **Retry Logic:** No automatic retry on transient failures
5. **Cost Management:** No monitoring or optimization

### Recommended Architecture

**Primary Path:**
1. **Immediate:** Fix nested virtualization (Standard security type, D4s_v5 VMs)
2. **Week 1:** Add health checks and retry logic
3. **Week 2:** Implement monitoring and alerting
4. **Week 3-4:** Migrate to pre-provisioned compute cluster
5. **Week 5+:** Cost optimization and alternative execution environments

**Fallback Path:**
- Azure Batch (if Azure ML continues to have issues)
- Direct VM pool (for development and debugging)

### Expected Outcomes

**Reliability:**
- Current: <50% job success rate (jobs getting stuck)
- Target: >95% job success rate

**Performance:**
- Current: 8+ hours for 13 tasks (stuck, cancelled)
- Target: 2-4 hours for 154 tasks

**Cost:**
- Current: $1.54 for failed 13-task job
- Target: $3-5 for successful 154-task evaluation (50%+ savings)

**Observability:**
- Current: Minimal (only job logs, no metrics)
- Target: Comprehensive (infrastructure + job + application metrics)

### Next Steps

1. **Immediate (Today):**
   - Update `azure.py` with VM security type fix
   - Test single-task evaluation
   - Validate nested virtualization works

2. **This Week:**
   - Implement health checks
   - Add retry logic
   - Run 10-task validation

3. **Next Week:**
   - Set up monitoring and alerting
   - Run 20-50 task evaluation
   - Measure baseline performance

4. **Following Weeks:**
   - Migrate to cluster architecture
   - Implement cost optimizations
   - Build fallback execution paths

---

## Sources

- [Create Azure burstable VM with nested virtualization - Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/4372831/create-azure-burstable-vm-with-nested-virtualizati)
- [How do I know what size Azure VM supports nested virtualization? - Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/813416/how-do-i-know-what-size-azure-vm-supports-nested-v)
- [Azure D4s v3 VM Virtualization not enabled? WSL unable to run correctly - Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/1195862/azure-d4s-v3-vm-virtualization-not-enabled-wsl-una)
- [Azure Machine Learning Compute Instance Stuck in Starting State - Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/1666026/azure-machine-learning-compute-instance-stuck-in-s)
- [AzureML Compute Job failed to start - GitHub Issue #1389](https://github.com/Azure/MachineLearningNotebooks/issues/1389)
- [Azure Machine Learning Job stuck on 'starting' when creating an environment - Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/1179332/azure-machine-learning-job-stuck-on-starting-when)
- [Azure ML jobs running on VM getting stuck in running state - Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/2106264/azure-ml-jobs-running-on-vm-getting-stuck-in-runni)
- [Troubleshooting ML pipelines - Azure Machine Learning](https://learn.microsoft.com/en-us/azure/machine-learning/how-to-debug-pipelines?view=azureml-api-1)

---

**Document Version:** 1.0
**Last Updated:** 2026-01-18
**Author:** Claude Code Analysis
**Status:** Ready for Review and Implementation
