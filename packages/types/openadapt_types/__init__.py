"""openadapt-types: Canonical Pydantic schemas for computer-use agents.

This package provides the shared type definitions used across the OpenAdapt
ecosystem and designed for adoption by any computer-use agent project.

Quick start::

    from openadapt_types import ComputerState, Action, ActionType, UINode

    state = ComputerState(
        viewport=(1920, 1080),
        nodes=[
            UINode(node_id="n0", role="button", name="Submit"),
        ],
    )

    action = Action(
        type=ActionType.CLICK,
        target=ActionTarget(node_id="n0"),
    )
"""

from importlib import metadata

from openadapt_types.action import (
    Action,
    ActionResult,
    ActionTarget,
    ActionType,
)
from openadapt_types.benchmark import (
    BenchmarkAction,
    BenchmarkAgent,
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_types.computer_state import (
    BoundingBox,
    ComputerState,
    ElementRole,
    ProcessInfo,
    UINode,
)
from openadapt_types.control_overlay import (
    CONTROL_OVERLAY_FRAME_SCHEMA,
    CONTROL_OVERLAY_STATE_ID_COMPONENTS,
    CONTROL_OVERLAY_STATUS_BY_PHASE,
    CONTROL_OVERLAY_TERMINAL_PHASES,
    CONTROL_OVERLAY_TIMELINE_SCHEMA,
    CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE,
    ControlOverlayControlsV1,
    ControlOverlayDataClassification,
    ControlOverlayFrameV1,
    ControlOverlayMode,
    ControlOverlayPhase,
    ControlOverlayProfile,
    ControlOverlayStepV1,
    ControlOverlayTimelineBindingV1,
    ControlOverlayTimelineEventV1,
    ControlOverlayTimelineV1,
    ControlOverlayWorkflowLabel,
    build_control_overlay_timeline,
    control_overlay_state_id,
    is_terminal_control_overlay_phase,
)
from openadapt_types.control_overlay_tracking import (
    CONTROL_OVERLAY_FRAME_V2_SCHEMA,
    CONTROL_OVERLAY_TIMELINE_V2_SCHEMA,
    ControlOverlayFrameV2,
    ControlOverlayMediaFrameBindingV2,
    ControlOverlayNormalizedRectV2,
    ControlOverlayObservationBindingV2,
    ControlOverlaySourceViewportV2,
    ControlOverlayTargetActionKind,
    ControlOverlayTargetBindingV2,
    ControlOverlayTargetTrackingV2,
    ControlOverlayTimelineBindingV2,
    ControlOverlayTimelineEventV2,
    ControlOverlayTimelineV2,
    build_control_overlay_timeline_v2,
    control_overlay_state_id_v2,
)
from openadapt_types.episode import Episode, Step
from openadapt_types.execute import (
    EXECUTE_ACCEPTED_SCHEMA,
    EXECUTE_EVIDENCE_RECEIPT_SCHEMA,
    EXECUTE_OPENAPI_SCHEMA,
    EXECUTE_REQUEST_SCHEMA,
    EXECUTE_STATUS_SCHEMA,
    EXECUTE_WEBHOOK_SCHEMA,
    EffectStrengthV1,
    ExecuteAcceptedV1,
    ExecuteAuthorizationContextV1,
    ExecuteDecisionRequiredWebhookV1,
    ExecuteEvidenceContractV1,
    ExecuteEvidenceReceiptV1,
    ExecuteLifecycleStateV1,
    ExecuteRequestV1,
    ExecuteStateChangedWebhookV1,
    ExecuteStatusV1,
    ExecuteTerminalOutcomeV1,
    ExecuteTerminalWebhookV1,
    ExecuteWebhookEventTypeV1,
    ExecuteWebhookV1,
    sign_execute_webhook_hmac,
)
from openadapt_types.execute_openapi import execute_openapi_document
from openadapt_types.execute_client import ExecuteApiError, ExecuteClient
from openadapt_types.execution_requirements import (
    CAPABILITY_MATCH_SCHEMA,
    EXECUTION_REQUIREMENTS_SCHEMA,
    CapabilityMatchV1,
    CapabilityMismatchCode,
    ExecutionRequirementsV1,
    match_runner_capabilities,
)
from openadapt_types.failure import FailureCategory, FailureRecord
from openadapt_types.human_decision import (
    HUMAN_DECISION_RECEIPT_REASONS,
    HUMAN_DECISION_RECEIPT_SCHEMA,
    HUMAN_DECISION_RECEIPT_SUCCESS_STATES,
    HUMAN_DECISION_TASK_SCHEMA,
    HUMAN_DECISION_TASK_V2_SCHEMA,
    HumanDecisionAction,
    HumanDecisionDeliveryState,
    HumanDecisionEntityFallback,
    HumanDecisionEvidenceSummaryV1,
    HumanDecisionQuestionTemplate,
    HumanDecisionQuestionV1,
    HumanDecisionQualifiedEntityV1,
    HumanDecisionReceiptReason,
    HumanDecisionReceiptState,
    HumanDecisionReceiptV1,
    HumanDecisionRequiredAuthn,
    HumanDecisionRiskClass,
    HumanDecisionSafeSlotsV1,
    HumanDecisionSubstrate,
    HumanDecisionTaskKind,
    HumanDecisionTaskV1,
    HumanDecisionTaskV2,
    sign_human_decision_receipt_hmac,
    sign_human_decision_task_hmac,
    sign_human_decision_task_v2_hmac,
)
from openadapt_types.parsing import (
    PARSE_ERROR_KEY,
    from_benchmark_action,
    parse_action,
    parse_action_dsl,
    parse_action_json,
    to_benchmark_action_dict,
)
from openadapt_types.runner_capability import (
    RUNNER_CAPABILITY_MANIFEST_SCHEMA,
    EffectVerificationTier,
    ExecutionMode,
    ExecutionProfile,
    ExecutionSurface,
    RunnerArchitecture,
    RunnerCapability,
    RunnerCapabilityLaneV1,
    RunnerCapabilityManifestV1,
    RunnerHostOS,
)

try:
    __version__ = metadata.version("openadapt-types")
except metadata.PackageNotFoundError:  # pragma: no cover - source checkout only
    # Never report a hard-coded version. An unmeasurable version is reported as
    # unknown so a caller cannot mistake a stale literal for the installed one.
    __version__ = "unknown"

# Keep the exports grouped by contract family for API review.
__all__ = [  # noqa: RUF022
    # computer_state
    "BoundingBox",
    "ComputerState",
    "ElementRole",
    "ProcessInfo",
    "UINode",
    # control_overlay
    "CONTROL_OVERLAY_FRAME_SCHEMA",
    "CONTROL_OVERLAY_STATE_ID_COMPONENTS",
    "CONTROL_OVERLAY_STATUS_BY_PHASE",
    "CONTROL_OVERLAY_TERMINAL_PHASES",
    "CONTROL_OVERLAY_TIMELINE_SCHEMA",
    "CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE",
    "ControlOverlayControlsV1",
    "ControlOverlayDataClassification",
    "ControlOverlayFrameV1",
    "ControlOverlayMode",
    "ControlOverlayPhase",
    "ControlOverlayProfile",
    "ControlOverlayStepV1",
    "ControlOverlayTimelineBindingV1",
    "ControlOverlayTimelineEventV1",
    "ControlOverlayTimelineV1",
    "ControlOverlayWorkflowLabel",
    "build_control_overlay_timeline",
    "control_overlay_state_id",
    "is_terminal_control_overlay_phase",
    # exact control-overlay target tracking
    "CONTROL_OVERLAY_FRAME_V2_SCHEMA",
    "CONTROL_OVERLAY_TIMELINE_V2_SCHEMA",
    "ControlOverlayFrameV2",
    "ControlOverlayMediaFrameBindingV2",
    "ControlOverlayNormalizedRectV2",
    "ControlOverlayObservationBindingV2",
    "ControlOverlaySourceViewportV2",
    "ControlOverlayTargetActionKind",
    "ControlOverlayTargetBindingV2",
    "ControlOverlayTargetTrackingV2",
    "ControlOverlayTimelineBindingV2",
    "ControlOverlayTimelineEventV2",
    "ControlOverlayTimelineV2",
    "build_control_overlay_timeline_v2",
    "control_overlay_state_id_v2",
    # action
    "Action",
    "ActionResult",
    "ActionTarget",
    "ActionType",
    # benchmark
    "BenchmarkAction",
    "BenchmarkAgent",
    "BenchmarkObservation",
    "BenchmarkTask",
    # episode
    "Episode",
    "Step",
    # Execute v1
    "EXECUTE_ACCEPTED_SCHEMA",
    "EXECUTE_EVIDENCE_RECEIPT_SCHEMA",
    "EXECUTE_OPENAPI_SCHEMA",
    "EXECUTE_REQUEST_SCHEMA",
    "EXECUTE_STATUS_SCHEMA",
    "EXECUTE_WEBHOOK_SCHEMA",
    "EffectStrengthV1",
    "ExecuteAcceptedV1",
    "ExecuteAuthorizationContextV1",
    "ExecuteDecisionRequiredWebhookV1",
    "ExecuteEvidenceContractV1",
    "ExecuteEvidenceReceiptV1",
    "ExecuteLifecycleStateV1",
    "ExecuteRequestV1",
    "ExecuteStateChangedWebhookV1",
    "ExecuteStatusV1",
    "ExecuteTerminalOutcomeV1",
    "ExecuteTerminalWebhookV1",
    "ExecuteWebhookEventTypeV1",
    "ExecuteWebhookV1",
    "sign_execute_webhook_hmac",
    "execute_openapi_document",
    "ExecuteApiError",
    "ExecuteClient",
    # runner capability and execution requirements
    "CAPABILITY_MATCH_SCHEMA",
    "EXECUTION_REQUIREMENTS_SCHEMA",
    "RUNNER_CAPABILITY_MANIFEST_SCHEMA",
    "CapabilityMatchV1",
    "CapabilityMismatchCode",
    "EffectVerificationTier",
    "ExecutionMode",
    "ExecutionProfile",
    "ExecutionRequirementsV1",
    "ExecutionSurface",
    "RunnerArchitecture",
    "RunnerCapability",
    "RunnerCapabilityLaneV1",
    "RunnerCapabilityManifestV1",
    "RunnerHostOS",
    "match_runner_capabilities",
    # failure
    "FailureCategory",
    "FailureRecord",
    # attended human decisions
    "HUMAN_DECISION_RECEIPT_REASONS",
    "HUMAN_DECISION_RECEIPT_SCHEMA",
    "HUMAN_DECISION_RECEIPT_SUCCESS_STATES",
    "HUMAN_DECISION_TASK_SCHEMA",
    "HUMAN_DECISION_TASK_V2_SCHEMA",
    "HumanDecisionAction",
    "HumanDecisionDeliveryState",
    "HumanDecisionEntityFallback",
    "HumanDecisionEvidenceSummaryV1",
    "HumanDecisionQuestionTemplate",
    "HumanDecisionQuestionV1",
    "HumanDecisionQualifiedEntityV1",
    "HumanDecisionReceiptReason",
    "HumanDecisionReceiptState",
    "HumanDecisionReceiptV1",
    "HumanDecisionRequiredAuthn",
    "HumanDecisionRiskClass",
    "HumanDecisionSafeSlotsV1",
    "HumanDecisionSubstrate",
    "HumanDecisionTaskKind",
    "HumanDecisionTaskV1",
    "HumanDecisionTaskV2",
    "sign_human_decision_receipt_hmac",
    "sign_human_decision_task_hmac",
    "sign_human_decision_task_v2_hmac",
    # parsing
    "PARSE_ERROR_KEY",
    "from_benchmark_action",
    "parse_action",
    "parse_action_dsl",
    "parse_action_json",
    "to_benchmark_action_dict",
]
