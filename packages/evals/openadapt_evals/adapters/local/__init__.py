"""Local desktop adapter for running agents on the local machine.

This module provides a BenchmarkAdapter that operates on the local desktop
rather than a remote VM. Useful for development, testing, and local replay.

Example:
    ```python
    from openadapt_evals.adapters.local import LocalAdapter

    adapter = LocalAdapter(action_delay=0.5)
    obs = adapter.observe()
    ```
"""

from openadapt_evals.adapters.local.adapter import LocalAdapter

__all__ = ["LocalAdapter"]
