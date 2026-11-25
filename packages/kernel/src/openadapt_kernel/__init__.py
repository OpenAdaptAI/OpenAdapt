"""openadapt-kernel: minimal commit-boundary verification and shadow-to-autopilot runtime."""

from openadapt_kernel.aci import OnlineACICalibrator, SessionStartNormalizer
from openadapt_kernel.canonical import (
    VALID_FIELD_TYPES,
    DossierVariableSchema,
    FieldType,
    SemanticSlotVerificationReport,
    SlotBindingStatus,
    SlotBindingVerdict,
    canonical_equal,
    canonical_var_name,
    extract_code_prefix_token,
    match_masked_id,
    normalize_casefold_ws,
    parse_canonical_currency_cents,
    parse_canonical_date,
    parse_canonical_phone,
    tokenize_role_label,
    verify_semantic_slot_bindings,
)
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
from openadapt_kernel.digital_twin import (
    DigitalTwinSnapshot,
    TwinHealResult,
    capture_zero_phi_digital_twin,
    self_heal_in_digital_twin,
)
from openadapt_kernel.egress_guard import (
    EgressBlockedError,
    EgressPolicy,
    EgressVerdict,
    LocalEgressGuard,
    inspect_outbound_payload,
    summarize_egress_ledger,
)
from openadapt_kernel.extension_bridge import (
    ExtensionSessionSummary,
    induce_pack_from_extension_recordings,
    parse_extension_evidence_jsonl,
    replay_evidence_into_interceptor,
)
from openadapt_kernel.fsm import (
    FSMState,
    FSMTransition,
    PortalFSM,
    SafetyProof,
    SafetyViolation,
    verify_fsm_safety,
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
from openadapt_kernel.mcp_server import dispatch_tool_call, handle_jsonrpc_message, serve_stdio
from openadapt_kernel.oracles import (
    OracleVerdict,
    extract_pdf_visible_text,
    token_boundary_check,
    verify_cross_tab_dom,
    verify_pdf_readback,
    verify_sqlite_ro,
)
from openadapt_kernel.prepare import (
    PackMutantReport,
    PendingReconciliationRecord,
    PrepareCard,
    PrepareExecutionResult,
    PrepareOutcome,
    PrepareResolution,
    ReadBackFieldSpec,
    ReadBackLens,
    RubberStampViolationError,
    UnresolvedSlot,
    build_prepare_card,
    execute_prepare_resolution,
    qualify_and_promote_pack,
    reconcile_pending_submission,
    verify_warm_pack_putget,
)
from openadapt_kernel.probe import (
    ProbeResult,
    compute_pixel_delta_ratio,
    probe_before_click,
    probe_hover_pixel_delta,
)
from openadapt_kernel.registry import (
    SignedPackEnvelope,
    publish_signed_pack,
    pull_verified_pack,
    render_audit_card_html,
    scan_pack_for_pii,
)
from openadapt_kernel.runner import RunResult, format_run_result_json, run_portal_pack
from openadapt_kernel.zero_phi_repair import (
    ASTVerificationResult,
    DistilledRepairStep,
    ZeroPHIRepairResult,
    execute_zero_phi_coding_repair,
    export_zero_phi_dom_twin,
    replay_distilled_zero_phi_repair,
    sanitize_ui_label_allowlist,
    verify_symbolic_repair_ast,
)

__all__ = [
    "ASTVerificationResult", "CapabilityToken", "CommitClassification", "CommitInterceptor", "ControlSchema",
    "DigitalTwinSnapshot", "DistilledRepairStep", "DossierVariableSchema", "EgressBlockedError",
    "EgressPolicy", "EgressVerdict", "ExtensionSessionSummary", "FSMState", "FSMTransition",
    "FieldType", "FinalizationDecision", "HealResult", "LocalEgressGuard", "MicroHandoffRequest",
    "MicroHandoffSession",
    "MutantQualificationReport", "NetworkEvent", "Obligation", "ObligationLedger", "OnlineACICalibrator",
    "OracleVerdict", "PackMutantReport", "PendingReconciliationRecord", "PlanStep", "PortalFSM", "PortalPack",
    "PrepareCard", "PrepareExecutionResult", "PrepareOutcome", "PrepareResolution", "ProbeResult",
    "ProvenanceViolation", "ReadBackFieldSpec", "ReadBackLens", "ReadBackProof", "RubberStampViolationError",
    "RunResult", "SafetyProof", "SafetyViolation", "SemanticSlotVerificationReport", "SessionStartNormalizer",
    "ShadowObserver", "ShadowTrace", "SignedPackEnvelope", "SlotBindingStatus", "SlotBindingVerdict",
    "StageResult", "StepwiseHealer", "TwinHealResult", "UnresolvedSlot", "VALID_FIELD_TYPES",
    "ZeroPHIRepairResult", "build_prepare_card", "canonical_equal", "canonical_var_name",
    "capture_zero_phi_digital_twin", "compute_pixel_delta_ratio",
    "dispatch_tool_call", "execute_prepare_resolution", "execute_zero_phi_coding_repair",
    "export_zero_phi_dom_twin", "extract_code_prefix_token", "extract_pdf_visible_text",
    "format_run_result_json", "generate_ephemeral_ed25519_keypair", "handle_jsonrpc_message",
    "induce_pack_from_extension_recordings", "inspect_outbound_payload", "is_search_or_filter_field",
    "match_masked_id", "mint_receipt",
    "normalize_casefold_ws", "observe_schema", "parse_canonical_currency_cents", "parse_canonical_date",
    "parse_canonical_phone", "parse_extension_evidence_jsonl", "probe_before_click", "probe_hover_pixel_delta",
    "publish_signed_pack", "pull_verified_pack", "qualify_and_promote_pack", "qualify_verifier_offline",
    "reconcile_pending_submission", "render_audit_card_html", "replay_distilled_zero_phi_repair",
    "replay_evidence_into_interceptor", "run_portal_pack", "sanitize_action_string",
    "sanitize_ui_label_allowlist", "scan_pack_for_pii", "self_heal_in_digital_twin", "serve_stdio",
    "stage_form", "summarize_egress_ledger", "token_boundary_check",
    "tokenize_role_label", "verify_cross_tab_dom", "verify_fsm_safety", "verify_pdf_readback",
    "verify_receipt", "verify_semantic_slot_bindings", "verify_sqlite_ro", "verify_symbolic_repair_ast",
    "verify_warm_pack_putget",
]
