"""Anthropic defaults that product code shares with the benchmark harness.

These live here, not in ``openadapt_flow.benchmark``, because the product
imports them and a frozen build leaves the benchmark package out
(``tests/test_runtime_never_imports_benchmark.py`` enforces the rule).
"""

from __future__ import annotations

import os
from pathlib import Path

#: The repo's single Anthropic-model constant; a bump here reaches every user.
MODEL = "claude-sonnet-5"
#: Pixels dispatched per computer-use ``scroll_amount`` unit (wheel click).
SCROLL_PX_PER_UNIT = 120


def load_api_key() -> str:
    """Resolve the Anthropic API key.

    Reads ``ANTHROPIC_API_KEY`` from the environment, falling back to the
    file ``~/.anthropic/api_key``.

    Returns:
        The API key string.

    Raises:
        RuntimeError: If neither source provides a key.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    path = Path.home() / ".anthropic" / "api_key"
    if path.is_file():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    raise RuntimeError(
        "No Anthropic API key: set ANTHROPIC_API_KEY or write the key to "
        "~/.anthropic/api_key"
    )
