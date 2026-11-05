"""Public facade over OpenAdapt's canonical verifier and receipt formats.

WriteResult describes the store's outcome. LocalSealVerification and
PortableReceiptVerification describe different signature formats; neither
substitutes for the store result or a caller's pinned issuer trust.
"""

from openadapt_flow.integrations.agents import WriteGuard
from openadapt_flow.offline_seal import LocalSealVerification, verify_local_seal
from openadapt_flow.verifier import (
    Actor,
    AlreadyWritten,
    Contract,
    Outcome,
    Scope,
    WriteBlocked,
    WriteResult,
    begin_write,
    issue_write_receipt,
    recheck,
    verify_write,
)
from openadapt_flow.write_receipt import ReceiptError
from openadapt_flow.write_receipt import Verification as PortableReceiptVerification
from openadapt_flow.write_receipt import verify as verify_receipt

__version__ = "0.1.0"

__all__ = [
    "Actor",
    "AlreadyWritten",
    "Contract",
    "LocalSealVerification",
    "Outcome",
    "PortableReceiptVerification",
    "ReceiptError",
    "Scope",
    "WriteBlocked",
    "WriteGuard",
    "WriteResult",
    "begin_write",
    "issue_write_receipt",
    "recheck",
    "verify_local_seal",
    "verify_receipt",
    "verify_write",
]
