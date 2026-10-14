"""Built-in task verifiers for common verification scenarios.

This module contains reference verifier implementations that demonstrate the
verifier registry pattern. Import this module to register the built-in
verifiers with the global registry.

Example:
    # Import to register built-in verifiers
    import openadapt_evals.evaluation.builtin_verifiers

    from openadapt_evals.evaluation.verifier_registry import registry
    result = registry.verify("clear_browsing_data", adapter)
"""

from __future__ import annotations

import logging

from openadapt_evals.evaluation.verifier_registry import (
    VerificationResult,
    register,
)

logger = logging.getLogger(__name__)


@register("clear_browsing_data")
def verify_clear_browsing_data(adapter) -> VerificationResult:
    """Verify Chrome browsing data has been cleared.

    Checks that Chrome's cache directory is empty by running a PowerShell
    command on the Windows VM via the adapter's run_powershell() method.

    Args:
        adapter: A BenchmarkAdapter with run_powershell() support
            (e.g., WAALiveAdapter).

    Returns:
        VerificationResult indicating whether the cache was cleared.
    """
    try:
        result = adapter.run_powershell(
            "(Get-ChildItem -Path "
            "$env:LOCALAPPDATA\\Google\\Chrome\\"
            "'User Data'\\Default\\Cache "
            "-Recurse -ErrorAction SilentlyContinue "
            "| Measure-Object).Count"
        )
        count = int(result.strip())
        success = count == 0
        return VerificationResult(
            success=success,
            score=1.0 if success else 0.0,
            details={
                "cache_file_count": count,
                "cache_path": (
                    "%LOCALAPPDATA%\\Google\\Chrome\\"
                    "User Data\\Default\\Cache"
                ),
            },
        )
    except Exception as e:
        logger.error("Failed to verify clear_browsing_data: %s", e)
        return VerificationResult(
            success=False,
            score=0.0,
            details={"error": str(e)},
        )
