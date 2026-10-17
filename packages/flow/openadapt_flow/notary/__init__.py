"""Notarized Seals: trusted timestamps, salted field commitments, dossiers.

An optional layer over the existing Seal (``openadapt.seal-signature/v1``). It
adds nothing to the receipt or its evidence manifest; every addition lives in
``extensions["openadapt.notary/v1"]`` beside them. See docs/NOTARIZED_SEALS.md.

Nothing here runs during replay and nothing here calls a model.
"""

from openadapt_flow.notary.commitments import (
    build_disclosure,
    commit_fields,
    verify_disclosure,
)
from openadapt_flow.notary.notarize import (
    EXTENSION_KEY,
    CloudNotaryIssuer,
    LocalNotaryIssuer,
    NotaryIssuer,
    notarize_document,
    timestamp_baseline,
    verify_notarized,
)
from openadapt_flow.notary.tsa import (
    HttpTsa,
    LocalTsa,
    TimestampAuthority,
    verify_timestamp_token,
)

__all__ = [
    "EXTENSION_KEY",
    "CloudNotaryIssuer",
    "HttpTsa",
    "LocalNotaryIssuer",
    "LocalTsa",
    "NotaryIssuer",
    "TimestampAuthority",
    "build_disclosure",
    "commit_fields",
    "notarize_document",
    "timestamp_baseline",
    "verify_disclosure",
    "verify_notarized",
    "verify_timestamp_token",
]
