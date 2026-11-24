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
from openadapt_verify.mcpify import (
    McpifyServer,
    ReadbackResult,
    SafetyPolicy,
    SignedReceipt,
    WorkflowToolSpec,
    generate_mcp_client_config,
    issue_mcpify_receipt,
    load_workflow_spec,
    load_workflow_specs,
    verify_mcpify_receipt,
)
from openadapt_verify.portal_readback import (
    ConfirmationPdfOracle,
    FieldCheck,
    FreshReloadSummaryOracle,
    PortalReadbackResult,
    ReloadProof,
)

__version__ = "0.2.1"

__all__ = [
    "Actor",
    "AlreadyWritten",
    "ConfirmationPdfOracle",
    "Contract",
    "FieldCheck",
    "FreshReloadSummaryOracle",
    "LocalSealVerification",
    "McpifyServer",
    "Outcome",
    "PortalReadbackResult",
    "PortableReceiptVerification",
    "ReadbackResult",
    "ReceiptError",
    "ReloadProof",
    "SafetyPolicy",
    "Scope",
    "SignedReceipt",
    "WorkflowToolSpec",
    "WriteBlocked",
    "WriteGuard",
    "WriteResult",
    "begin_write",
    "checked_write",
    "generate_mcp_client_config",
    "issue_mcpify_receipt",
    "issue_write_receipt",
    "load_workflow_spec",
    "load_workflow_specs",
    "recheck",
    "verify_local_seal",
    "verify_mcpify_receipt",
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
