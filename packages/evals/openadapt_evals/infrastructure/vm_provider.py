"""Cloud-agnostic VM provider protocol for pool operations.

Defines the interface that AzureVMManager and AWSVMManager both satisfy.
Uses typing.Protocol (structural subtyping) so existing classes need zero
inheritance changes — they just implement the right method signatures.

Only covers the methods used by PoolManager and pool CLI commands.
Azure-only methods (generalize_vm, create_image, list_images, delete_image)
are intentionally excluded.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class VMProvider(Protocol):
    """Protocol for cloud VM managers used by PoolManager.

    Any class with these methods and properties satisfies the protocol
    automatically — no inheritance required.
    """

    @property
    def resource_scope(self) -> str:
        """Cloud-specific resource scope identifier.

        Azure: resource group name.  AWS: region string.
        """
        ...

    @property
    def ssh_username(self) -> str:
        """Default SSH username for VMs created by this provider.

        Azure: "azureuser".  AWS: "ubuntu".
        """
        ...

    def get_vm_ip(self, name: str) -> Optional[str]:
        """Get VM public IP address."""
        ...

    def get_vm_state(self, name: str) -> Optional[str]:
        """Get VM power state."""
        ...

    def create_vm(
        self,
        name: str,
        region: str,
        size: str,
        image: str = "",
        admin_username: str = "",
        image_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a VM. Must return dict with at least 'publicIpAddress' key."""
        ...

    def delete_vm(self, name: str) -> bool:
        """Delete VM and associated resources."""
        ...

    def deallocate_vm(self, name: str) -> bool:
        """Stop VM (keep disk, stop billing)."""
        ...

    def start_vm(self, name: str) -> bool:
        """Start a stopped/deallocated VM."""
        ...

    def set_auto_shutdown(self, name: str, hours: int = 4) -> bool:
        """Set auto-shutdown policy on a VM."""
        ...

    def find_available_size_and_region(
        self, gpu: bool = False,
    ) -> tuple[str, str, float]:
        """Find a working VM size and region.

        Args:
            gpu: If True, try GPU sizes for RL training.

        Returns:
            Tuple of (vm_size, region, cost_per_hour).
        """
        ...

    def list_pool_resources(self, prefix: str) -> dict[str, list[str]]:
        """List cloud resources matching a pool prefix.

        Returns:
            Dict mapping resource type to list of resource names.
            Keys are provider-specific (e.g. "vms", "nics", "ips", "disks"
            for Azure; "instances", "eips" for AWS).
        """
        ...

    def cleanup_pool_resources(
        self, prefix: str, resources: dict[str, list[str]]
    ) -> bool:
        """Delete pool resources returned by list_pool_resources.

        Args:
            prefix: Pool name prefix.
            resources: Dict from list_pool_resources().

        Returns:
            True if all resources were cleaned up.
        """
        ...
