"""openadapt-kernel: minimal commit-boundary verification and shadow-to-autopilot runtime."""

from openadapt_kernel.copilot import (
    MicroHandoffRequest,
    MicroHandoffSession,
    MutantQualificationReport,
    PortalPack,
    ShadowObserver,
    ShadowTrace,
    generate_ephemeral_ed25519_keypair,
    mint_receipt,
    qualify_verifier_offline,
    verify_receipt,
)
from openadapt_kernel.extension_bridge import (
    ExtensionSessionSummary,
    induce_pack_from_extension_recordings,
    parse_extension_evidence_jsonl,
    replay_evidence_into_interceptor,
)
from openadapt_kernel.interceptor import (
    CommitClassification,
    CommitInterceptor,
    NetworkEvent,
    is_search_or_filter_field,
    sanitize_action_string,
)
from openadapt_kernel.ledger import (
    CapabilityToken,
    FinalizationDecision,
    Obligation,
    ObligationLedger,
    ProvenanceViolation,
    ReadBackProof,
)
from openadapt_kernel.macro import (
    ControlSchema,
    HealResult,
    PlanStep,
    StageResult,
    StepwiseHealer,
    observe_schema,
    stage_form,
)
from openadapt_kernel.mcp_server import (
    dispatch_tool_call,
    handle_jsonrpc_message,
    serve_stdio,
)
from openadapt_kernel.oracles import (
    OracleVerdict,
    extract_pdf_visible_text,
    token_boundary_check,
    verify_cross_tab_dom,
    verify_pdf_readback,
    verify_sqlite_ro,
)
from openadapt_kernel.runner import (
    RunResult,
    format_run_result_json,
    run_portal_pack,
)

__all__ = [
    "CapabilityToken",
    "CommitClassification",
    "CommitInterceptor",
    "ControlSchema",
    "ExtensionSessionSummary",
    "FinalizationDecision",
    "HealResult",
    "MicroHandoffRequest",
    "MicroHandoffSession",
    "MutantQualificationReport",
    "NetworkEvent",
    "Obligation",
    "ObligationLedger",
    "OracleVerdict",
    "PlanStep",
    "PortalPack",
    "ProvenanceViolation",
    "ReadBackProof",
    "RunResult",
    "ShadowObserver",
    "ShadowTrace",
    "StageResult",
    "StepwiseHealer",
    "dispatch_tool_call",
    "extract_pdf_visible_text",
    "format_run_result_json",
    "generate_ephemeral_ed25519_keypair",
    "handle_jsonrpc_message",
    "induce_pack_from_extension_recordings",
    "is_search_or_filter_field",
    "mint_receipt",
    "observe_schema",
    "parse_extension_evidence_jsonl",
    "qualify_verifier_offline",
    "replay_evidence_into_interceptor",
    "run_portal_pack",
    "sanitize_action_string",
    "serve_stdio",
    "stage_form",
    "token_boundary_check",
    "verify_cross_tab_dom",
    "verify_pdf_readback",
    "verify_receipt",
    "verify_sqlite_ro",
]
