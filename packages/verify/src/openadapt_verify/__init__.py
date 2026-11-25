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

from openadapt_verify import jit
from openadapt_verify.commit_firewall import (
    CommitBlockedError,
    CommitResult,
    UiCommitCheck,
    async_verify_commit,
    ui_commit_check,
    verify_commit,
)
from openadapt_verify.guard import checked_write, verified
from openadapt_verify.jit import (
    CompiledPlan,
    CompiledStep,
    JitResult,
    ParameterBinder,
    format_scorecard,
    jit_wrap,
)
from openadapt_verify.mcpify import (
    McpifyServer,
    ReadbackResult,
    SafetyPolicy,
    SignedReceipt,
    WorkflowToolSpec,
    generate_mcp_client_config,
    issue_mcpify_receipt,
    load_compiled_tools,
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
    "CommitBlockedError",
    "CommitResult",
    "CompiledPlan",
    "CompiledStep",
    "ConfirmationPdfOracle",
    "Contract",
    "FieldCheck",
    "FreshReloadSummaryOracle",
    "JitResult",
    "LocalSealVerification",
    "McpifyServer",
    "Outcome",
    "ParameterBinder",
    "PortalReadbackResult",
    "PortableReceiptVerification",
    "ReadbackResult",
    "ReceiptError",
    "ReloadProof",
    "SafetyPolicy",
    "Scope",
    "SignedReceipt",
    "UiCommitCheck",
    "WorkflowToolSpec",
    "WriteBlocked",
    "WriteGuard",
    "WriteResult",
    "async_verify_commit",
    "begin_write",
    "checked_write",
    "format_scorecard",
    "generate_mcp_client_config",
    "issue_mcpify_receipt",
    "issue_write_receipt",
    "jit",
    "jit_wrap",
    "load_compiled_tools",
    "load_workflow_spec",
    "load_workflow_specs",
    "recheck",
    "ui_commit_check",
    "verify_commit",
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
