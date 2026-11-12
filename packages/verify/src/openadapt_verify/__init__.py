"""Public facade over OpenAdapt's canonical verifier and receipt formats.

WriteResult describes the store's outcome. LocalSealVerification and
PortableReceiptVerification describe different signature formats; neither
substitutes for the store result or a caller's pinned issuer trust.
"""

from typing import TYPE_CHECKING, Any

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

from openadapt_verify.guard import checked_write, verified

__version__ = "0.2.0"

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
    "checked_write",
    "issue_write_receipt",
    "recheck",
    "verify_local_seal",
    "verified",
    "verify_receipt",
    "verify_write",
]


if TYPE_CHECKING:
    from openadapt_flow.integrations.agents import WriteGuard


def __getattr__(name: str) -> Any:
    if name == "WriteGuard":
        from openadapt_flow.integrations.agents import WriteGuard

        globals()[name] = WriteGuard
        return WriteGuard
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
