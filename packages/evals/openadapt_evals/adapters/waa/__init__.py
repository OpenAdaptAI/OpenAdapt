"""Windows Agent Arena (WAA) adapters.

This module provides adapters for the Windows Agent Arena benchmark:
- WAAAdapter: Full WAA integration (requires WAA repo)
- WAAMockAdapter: Mock adapter for testing (no Windows required)
- WAALiveAdapter: HTTP adapter for remote WAA server

Example:
    ```python
    from openadapt_evals.adapters.waa import WAAMockAdapter, WAALiveAdapter

    # For local testing (no Windows VM)
    adapter = WAAMockAdapter(num_tasks=10)

    # For remote evaluation
    adapter = WAALiveAdapter(server_url="http://vm-ip:5000")
    ```
"""

from openadapt_evals.adapters.waa.live import (
    SYNTHETIC_TASK_PATTERNS,
    WAA_TASK_ID_PATTERN,
    SyntheticTaskError,
    WAALiveAdapter,
    WAALiveConfig,
    is_real_waa_task_id,
    is_synthetic_task_id,
)
from openadapt_evals.adapters.waa.mock import (
    WAA_DOMAINS,
    WAAAdapter,
    WAAConfig,
    WAAMockAdapter,
)

__all__ = [
    # Mock/full adapters
    "WAAAdapter",
    "WAAConfig",
    "WAAMockAdapter",
    "WAA_DOMAINS",
    # Live adapter
    "WAALiveAdapter",
    "WAALiveConfig",
    "WAA_TASK_ID_PATTERN",
    "SYNTHETIC_TASK_PATTERNS",
    # Task ID validation
    "SyntheticTaskError",
    "is_real_waa_task_id",
    "is_synthetic_task_id",
]
