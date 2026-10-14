"""VM IP resolution with automatic detection.

Resolves the VM IP address using a layered approach:

1. Explicit IP (from ``--vm-ip`` argument) — instant
2. Pool registry (local JSON file) — instant, no Azure auth
3. Azure CLI query (``az vm list-ip-addresses``) — always accurate, ~3s

Usage::

    from openadapt_evals.infrastructure.vm_ip import resolve_vm_ip

    # Auto-detect (pool registry → Azure CLI)
    ip = resolve_vm_ip()

    # Explicit override
    ip = resolve_vm_ip(explicit_ip="10.0.0.1")
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_vm_ip(explicit_ip: str | None = None) -> str:
    """Resolve the VM IP address.

    Resolution order:

    1. ``explicit_ip`` — returned immediately if provided
    2. Pool registry — reads ``benchmark_results/vm_pool_registry.json``
       for the first active worker's IP
    3. Azure CLI query — calls ``AzureVMManager.get_vm_ip()`` (always accurate, ~3s)

    Args:
        explicit_ip: If provided, returned as-is (from ``--vm-ip`` argument).

    Returns:
        The resolved IP address string.

    Raises:
        RuntimeError: If no running VM can be found by any method.
    """
    if explicit_ip:
        return explicit_ip

    # Try pool registry (fast, local file)
    ip = _ip_from_pool_registry()
    if ip:
        logger.info("VM IP resolved from pool registry: %s", ip)
        return ip

    # Fall back to Azure query (always accurate, ~3s)
    ip = _ip_from_azure_query()
    if ip:
        logger.info("VM IP resolved from Azure query: %s", ip)
        return ip

    raise RuntimeError(
        "No running VM found. Either:\n"
        "  1. Create one:  oa-vm pool-create --workers 1\n"
        "  2. Start it:    oa-vm vm start\n"
        "  3. Pass explicitly:  --vm-ip <IP>"
    )


def _ip_from_pool_registry() -> str | None:
    """Try to read VM IP from the pool registry file.

    Returns the IP of the first non-deleted, non-failed worker, or None.
    """
    registry_path = Path("benchmark_results/vm_pool_registry.json")
    if not registry_path.exists():
        return None

    try:
        import json

        with open(registry_path) as f:
            data = json.load(f)

        for worker in data.get("workers", []):
            ip = worker.get("ip")
            status = worker.get("status", "")
            if ip and status not in ("deleted", "failed"):
                return ip
    except Exception as e:
        logger.debug("Could not read pool registry: %s", e)

    return None


def _ip_from_azure_query() -> str | None:
    """Query Azure for the VM IP address.

    Tries pool-style names (``waa-pool-00``) first, then falls back to
    the legacy name (``waa-eval-vm``).
    """
    try:
        from openadapt_evals.config import settings
        from openadapt_evals.infrastructure.azure_vm import AzureVMManager

        mgr = AzureVMManager(resource_group=settings.azure_resource_group)

        # Try pool-style name first (most common)
        ip = mgr.get_vm_ip("waa-pool-00")
        if ip:
            return ip

        # Fallback to legacy name
        ip = mgr.get_vm_ip("waa-eval-vm")
        if ip:
            return ip

    except Exception as e:
        logger.debug("Azure VM IP query failed: %s", e)

    return None
