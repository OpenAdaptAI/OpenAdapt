"""Infrastructure components for VM management and monitoring.

This module provides:
- VMProvider: Cloud-agnostic VM provider protocol
- AzureVMManager: Azure VM lifecycle management (SDK + CLI fallback)
- AWSVMManager: AWS EC2 lifecycle management (boto3)
- PoolManager: Multi-VM pool orchestration
- VMMonitor: Azure VM status monitoring
- AzureOpsTracker: Azure operation logging
- SSHTunnelManager: SSH tunnel management for VNC/API access
- WAAConnection: Auto-recovering SSH tunnel for WAA servers
- QEMUResetManager: QEMU monitor-based Windows restart

Example:
    ```python
    from openadapt_evals.infrastructure import AzureVMManager, PoolManager

    # Manage VMs (Azure)
    vm = AzureVMManager()
    ip = vm.get_vm_ip("waa-eval-vm")

    # Or use AWS
    from openadapt_evals.infrastructure import AWSVMManager
    vm = AWSVMManager(region="us-east-1")
    pool = PoolManager(vm_manager=vm)

    # Create and manage pools
    pool = PoolManager()
    pool.create(workers=3)

    # Restart Windows inside QEMU
    from openadapt_evals.infrastructure import QEMUResetManager
    mgr = QEMUResetManager(vm_ip="10.0.0.1")
    success, msg = mgr.restart_windows()

    # Auto-detect VM IP
    from openadapt_evals.infrastructure import resolve_vm_ip
    ip = resolve_vm_ip()  # pool registry → Azure CLI
    ```
"""

from openadapt_evals.infrastructure.azure_ops_tracker import AzureOpsTracker
from openadapt_evals.infrastructure.azure_vm import AzureVMManager
from openadapt_evals.infrastructure.pool import PoolManager, PoolRunResult
from openadapt_evals.infrastructure.probe import (
    MultiLayerProbeResult,
    ProbeLayerResult,
    multi_layer_probe,
    print_probe_results,
)
from openadapt_evals.infrastructure.qemu_reset import QEMUResetManager
from openadapt_evals.infrastructure.screen_stability import (
    compare_screenshots,
    wait_for_stable_screen,
)
from openadapt_evals.infrastructure.ssh_tunnel import SSHTunnelManager, get_tunnel_manager
from openadapt_evals.infrastructure.vm_ip import resolve_vm_ip
from openadapt_evals.infrastructure.vm_monitor import VMConfig, VMMonitor
from openadapt_evals.infrastructure.vm_provider import VMProvider
from openadapt_evals.infrastructure.waa_connection import WAAConnection

try:
    from openadapt_evals.infrastructure.aws_vm import AWSVMManager
except ImportError:
    AWSVMManager = None  # boto3 not installed; use `pip install openadapt-evals[aws]`

__all__ = [
    "AWSVMManager",
    "AzureOpsTracker",
    "AzureVMManager",
    "MultiLayerProbeResult",
    "PoolManager",
    "PoolRunResult",
    "ProbeLayerResult",
    "QEMUResetManager",
    "VMMonitor",
    "VMConfig",
    "VMProvider",
    "SSHTunnelManager",
    "WAAConnection",
    "compare_screenshots",
    "get_tunnel_manager",
    "multi_layer_probe",
    "print_probe_results",
    "resolve_vm_ip",
    "wait_for_stable_screen",
]
