"""Records interchange: an open format between a document side and an executor.

See ``docs/INTERCHANGE.md`` for the specification and
``schemas/records-interchange-v0.json`` for the JSON Schema.
"""

from openadapt_flow.interchange.conformance import (
    CODES,
    Violation,
    check,
    check_file,
    compute_checksum,
    run_cases,
    with_checksum,
)
from openadapt_flow.interchange.l1 import ExchangeNotConforming, emit_exchange
from openadapt_flow.interchange.model import (
    INDEPENDENT_CHANNELS,
    READ_GRADES,
    SCHEMA_VERSION,
    Exchange,
)

__all__ = [
    "CODES",
    "INDEPENDENT_CHANNELS",
    "READ_GRADES",
    "SCHEMA_VERSION",
    "Exchange",
    "ExchangeNotConforming",
    "Violation",
    "check",
    "check_file",
    "compute_checksum",
    "emit_exchange",
    "json_schema",
    "run_cases",
    "with_checksum",
]


def json_schema() -> dict:
    """The JSON Schema for :class:`Exchange`, as published in ``schemas/``."""
    return Exchange.model_json_schema()
