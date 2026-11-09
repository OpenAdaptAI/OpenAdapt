"""Explicit record-read contracts for owned synthetic fixtures.

The endpoint count properties validate these declarations against the actual
fixture source/response. They are not inferred from paths or response length.
"""

from typing import Any

import yaml

from openadapt_flow.mockmed.fault_server import MOCKMED_RECORDS_READ_CONTRACT
from openadapt_flow.mockmed.fault_server import (
    mockmed_rest_read_kwargs as _mockmed_rest_read_kwargs,
)


def mockmed_effects_config(base_url: str) -> dict[str, Any]:
    return {**MOCKMED_RECORDS_READ_CONTRACT, "base_url": base_url}


def mockmed_rest_read_kwargs() -> dict[str, Any]:
    return _mockmed_rest_read_kwargs()


def mockmed_effects_yaml(base_url: str) -> str:
    """Indented verifier mapping for the owned MockMed HTTP fixture."""
    return "".join(
        "  " + line + "\n"
        for line in yaml.safe_dump(
            mockmed_effects_config(base_url), sort_keys=False
        ).splitlines()
    )


def memory_rest_effects_config(base_url: str) -> dict[str, Any]:
    """The owned MemoryRestCopy fixture's complete, unpaged /api/db read.

    Its HTTP handler copies every row under the store lock. This declaration
    applies to that fixture, not an arbitrary endpoint with the same path.
    """
    return {
        "kind": "rest",
        "base_url": base_url,
        "records_path": "/api/db",
        "records_key": "records",
        "records_complete": True,
    }
