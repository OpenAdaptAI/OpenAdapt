"""Task verifier registry for custom task verification.

This module provides a registry pattern for registering and running custom
task verifiers. Verifiers are functions that check whether a task was completed
successfully by inspecting the state of the environment (e.g., checking that
Chrome's cache directory is empty after clearing browsing data).

Verifiers are registered declaratively and only executed when explicitly called.
They do not require a live VM or WAA connection at registration time.

Example:
    from openadapt_evals.evaluation.verifier_registry import register, registry

    @register("clear_browsing_data")
    def verify_clear_browsing(adapter):
        # Use adapter to inspect VM state
        result = adapter.run_powershell(
            "(Get-ChildItem -Path $env:LOCALAPPDATA\\\\Google\\\\Chrome\\\\"
            "User Data\\\\Default\\\\Cache -Recurse | Measure-Object).Count"
        )
        return VerificationResult(
            success=int(result.strip()) == 0,
            score=1.0 if int(result.strip()) == 0 else 0.0,
            details={"cache_file_count": int(result.strip())},
        )

    # Later, during evaluation:
    result = registry.verify("clear_browsing_data", adapter)
    print(f"Success: {result.success}, Score: {result.score}")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    """Result of a task verification.

    Attributes:
        success: Whether the task was completed successfully.
        score: Score between 0.0 and 1.0.
        details: Arbitrary metadata about the verification.
    """

    success: bool
    score: float  # 0.0-1.0
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate score is in range."""
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(
                f"Score must be between 0.0 and 1.0, got {self.score}"
            )


class TaskVerifierRegistry:
    """Registry for custom task verifiers.

    Verifiers are functions that inspect the environment state (via a
    BenchmarkAdapter) to determine whether a task was completed successfully.

    The registry supports:
    - Decorator-based registration
    - Programmatic registration
    - Lookup and execution of verifiers by task name
    - Listing all registered verifiers

    Example:
        registry = TaskVerifierRegistry()

        @registry.register("my_task")
        def verify_my_task(adapter):
            return VerificationResult(success=True, score=1.0)

        result = registry.verify("my_task", adapter)
    """

    def __init__(self) -> None:
        self._verifiers: dict[str, Callable] = {}

    def register(self, task_name: str) -> Callable:
        """Decorator to register a verifier function for a task.

        Args:
            task_name: The task name this verifier handles.

        Returns:
            Decorator that registers the function and returns it unchanged.

        Raises:
            ValueError: If a verifier is already registered for this task name.

        Example:
            @registry.register("clear_browsing_data")
            def verify_clear_browsing(adapter):
                ...
        """

        def decorator(fn: Callable) -> Callable:
            if task_name in self._verifiers:
                raise ValueError(
                    f"A verifier is already registered for task "
                    f"'{task_name}': {self._verifiers[task_name].__name__}. "
                    f"Cannot register {fn.__name__}."
                )
            self._verifiers[task_name] = fn
            logger.debug(
                "Registered verifier '%s' for task '%s'",
                fn.__name__,
                task_name,
            )
            return fn

        return decorator

    def register_function(
        self, task_name: str, fn: Callable, *, force: bool = False
    ) -> None:
        """Programmatically register a verifier function.

        Args:
            task_name: The task name this verifier handles.
            fn: The verifier function.
            force: If True, overwrite any existing registration.

        Raises:
            ValueError: If a verifier is already registered and force is False.
        """
        if task_name in self._verifiers and not force:
            raise ValueError(
                f"A verifier is already registered for task "
                f"'{task_name}': {self._verifiers[task_name].__name__}. "
                f"Use force=True to overwrite."
            )
        self._verifiers[task_name] = fn
        logger.debug(
            "Registered verifier '%s' for task '%s'",
            fn.__name__,
            task_name,
        )

    def verify(self, task_name: str, adapter: Any) -> VerificationResult:
        """Run the registered verifier for a task.

        Args:
            task_name: The task to verify.
            adapter: A BenchmarkAdapter (or any object the verifier expects).
                For WAA tasks, this is typically a WAALiveAdapter with
                run_powershell() available.

        Returns:
            VerificationResult with success, score, and details.

        Raises:
            KeyError: If no verifier is registered for the task name.
            Exception: Any exception raised by the verifier function is
                propagated to the caller.
        """
        if task_name not in self._verifiers:
            raise KeyError(
                f"No verifier registered for task '{task_name}'. "
                f"Available verifiers: {self.list_verifiers()}"
            )

        fn = self._verifiers[task_name]
        logger.info("Running verifier '%s' for task '%s'", fn.__name__, task_name)
        result = fn(adapter)

        if not isinstance(result, VerificationResult):
            raise TypeError(
                f"Verifier '{fn.__name__}' for task '{task_name}' returned "
                f"{type(result).__name__}, expected VerificationResult."
            )

        logger.info(
            "Verification result for '%s': success=%s, score=%.2f",
            task_name,
            result.success,
            result.score,
        )
        return result

    def list_verifiers(self) -> list[str]:
        """List all registered task names.

        Returns:
            Sorted list of task names with registered verifiers.
        """
        return sorted(self._verifiers.keys())

    def has_verifier(self, task_name: str) -> bool:
        """Check if a verifier is registered for a task.

        Args:
            task_name: Task name to check.

        Returns:
            True if a verifier is registered for this task name.
        """
        return task_name in self._verifiers

    def get_verifier(self, task_name: str) -> Callable:
        """Get the verifier function for a task.

        Args:
            task_name: Task name to look up.

        Returns:
            The registered verifier function.

        Raises:
            KeyError: If no verifier is registered for this task name.
        """
        if task_name not in self._verifiers:
            raise KeyError(
                f"No verifier registered for task '{task_name}'. "
                f"Available verifiers: {self.list_verifiers()}"
            )
        return self._verifiers[task_name]

    def clear(self) -> None:
        """Remove all registered verifiers.

        Primarily useful for testing.
        """
        self._verifiers.clear()

    def __len__(self) -> int:
        """Return the number of registered verifiers."""
        return len(self._verifiers)

    def __repr__(self) -> str:
        """Return a string representation."""
        return (
            f"TaskVerifierRegistry("
            f"{len(self._verifiers)} verifier(s): "
            f"{self.list_verifiers()})"
        )


# Global registry instance
registry = TaskVerifierRegistry()

# Convenience alias for decorator usage
register = registry.register
