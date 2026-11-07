"""Explicit record-read contracts for owned synthetic fixtures.

The endpoint count properties validate these declarations against the actual
fixture source/response. They are not inferred from paths or response length.
"""

from typing import Any

from openadapt_flow.mockmed.fault_server import MOCKMED_RECORDS_READ_CONTRACT


def mockmed_effects_config(base_url: str) -> dict[str, Any]:
    return {**MOCKMED_RECORDS_READ_CONTRACT, "base_url": base_url}


def mockmed_rest_read_kwargs() -> dict[str, Any]:
    return {
        key: value
        for key, value in MOCKMED_RECORDS_READ_CONTRACT.items()
        if key != "kind"
    }
