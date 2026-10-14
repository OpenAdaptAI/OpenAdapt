"""Client-side evaluation module for WAA benchmarks.

This module provides client-side evaluation without requiring a sidecar service.
The evaluators run locally, making HTTP calls to the WAA server's /execute endpoint.

It also provides a task verifier registry for registering custom verification
functions that can inspect the VM state after task execution.

Example usage:
    from openadapt_evals.evaluation import EvaluatorClient, discover_vm_ip

    # Auto-detect VM IP
    vm_ip = discover_vm_ip()

    # Or create client with auto-detection
    client = EvaluatorClient()  # Auto-detects IP
    client = EvaluatorClient(vm_ip="20.127.64.200")  # Explicit IP

    # Evaluate a task
    result = client.evaluate(task_config)
    print(f"Success: {result.success}, Score: {result.score}")

    # Custom task verification
    from openadapt_evals.evaluation import register, registry, VerificationResult

    @register("my_task")
    def verify_my_task(adapter):
        return VerificationResult(success=True, score=1.0)

    result = registry.verify("my_task", adapter)
"""

from .client import (
    EvaluationError,
    EvaluationResult,
    EvaluatorClient,
    EvaluatorsUnavailableError,
)
from .discovery import DiscoveryMethod, VMIPDiscovery, discover_vm_ip
from .verifier_registry import (
    TaskVerifierRegistry,
    VerificationResult,
    register,
    registry,
)

__all__ = [
    "VMIPDiscovery",
    "DiscoveryMethod",
    "discover_vm_ip",
    "EvaluatorClient",
    "EvaluationResult",
    "EvaluationError",
    "EvaluatorsUnavailableError",
    "TaskVerifierRegistry",
    "VerificationResult",
    "register",
    "registry",
]
