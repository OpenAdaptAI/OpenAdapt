"""Countersigned Seals: trusted timestamps, salted field commitments, dossiers.

An optional layer over the existing Seal (``openadapt.seal-signature/v1``). It
adds nothing to the receipt or its evidence manifest; every addition lives in
``extensions["openadapt.countersign/v1"]`` beside them. See docs/COUNTERSIGNED_SEALS.md.

Nothing here runs during replay and nothing here calls a model.
"""

from openadapt_flow.countersign.commitments import (
    build_disclosure,
    commit_fields,
    verify_disclosure,
)
from openadapt_flow.countersign.document import (
    EXTENSION_KEY,
    CloudCountersignIssuer,
    CountersignIssuer,
    LocalCountersignIssuer,
    countersign_document,
    timestamp_baseline,
    verify_countersigned,
)
from openadapt_flow.countersign.tsa import (
    HttpTsa,
    LocalTsa,
    TimestampAuthority,
    verify_timestamp_token,
)

__all__ = [
    "EXTENSION_KEY",
    "CloudCountersignIssuer",
    "HttpTsa",
    "LocalCountersignIssuer",
    "LocalTsa",
    "CountersignIssuer",
    "TimestampAuthority",
    "build_disclosure",
    "commit_fields",
    "countersign_document",
    "timestamp_baseline",
    "verify_disclosure",
    "verify_countersigned",
    "verify_timestamp_token",
]
