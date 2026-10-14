// Shared frontend types mirroring the engine's IPC payloads (spec §3).

import type { PlainResultKey } from "./outcomes";

export type DeploymentLane = "cloud" | "byoc";
export type PhiMode = "off" | "on";
export type TargetBackend =
  | "web"
  | "windows"
  | "macos"
  | "linux"
  | "rdp"
  | "citrix";

/**
 * PHI-capable execution target staged in a private Flow deployment config.
 * Backend credentials and policy stay in the operator-owned deployment config
 * or its referenced environment.
 */
export interface ExecutionTarget {
  backend: TargetBackend;
  url?: string;
  /**
   * Web only: loopback DevTools address of the person's running, signed-in
   * Chromium. Runs attach to it and start from the screen it shows, so a task
   * recorded after sign-in starts signed in. No credential is stored.
   */
  browser_cdp_endpoint?: string;
  /** Web only: exact URL of the tab to use when several share the origin. */
  browser_page_url?: string;
  agent_url?: string;
  macos_app?: string;
  macos_window_title?: string;
  linux_app?: string;
  linux_window_title?: string;
  linux_allow_physical_input?: boolean;
  rdp_host?: string;
  rdp_window?: string;
  rdp_window_title?: string;
  rdp_readiness_text?: string;
}

/**
 * Capability-aware surface availability (engine/capabilities.py), schema
 * "openadapt-desktop.capability-report/v1". The engine's detection is the
 * single source of truth; the UI never claims "Available" without it.
 */
export type CapabilityState =
  | "available"
  | "driver_required"
  | "permission_required"
  | "unsupported_host";

export interface SurfaceCapability {
  state: CapabilityState;
  detail: string;
  remediation: string | null;
  driver: { name: string; version: string | null } | null;
}

export interface CapabilityReport {
  schema: string;
  generated_at: string;
  host: {
    os: string;
    os_version: string;
    arch: string;
    app_version: string;
  };
  surfaces: Partial<Record<TargetBackend, SurfaceCapability>>;
}

export type StepState =
  | "pending"
  | "running"
  | "verified"
  | "halted"
  | "attention"
  | "failed";

export interface AuthStatus {
  authenticated: boolean;
  kind?: "ingest_token";
  host?: string;
  org_id?: string | null;
}

export interface EngineStatus {
  recording: boolean;
  paused: boolean;
  duration_secs?: number | null;
  capture_id?: string | null;
  pause_prompt?: string | null;
  controls?: {
    pause: boolean;
    resume: boolean;
    stop: boolean;
  };
}

export interface Workflow {
  id: string;
  name: string;
  steps: number;
  updated_at?: string;
  last_run_state?: StepState;
  open_halts?: number;
  synced?: boolean;
  /** Plain result key of the last run, derived from its transaction outcome. */
  last_result?: PlainResultKey | null;
  /** Items of this task waiting for a person. */
  open_items?: number;
  /** Waiting items a person chose to fix by showing the step again. */
  teaching_items?: number;
  /** An open item's save may have gone through; more items wait for a check. */
  blocked?: boolean;
  /** new: not checked yet; checking: readiness test under way; ready: certified and sealed. */
  readiness?: "new" | "checking" | "ready";
  certified?: boolean;
  encrypted?: boolean;
  version?: number;
  capture_id?: string | null;
  /** What the person said the task does when they recorded it. */
  task?: string;
  /** Length of the person's own recording, the default manual baseline. */
  demo_seconds?: number | null;
}

/** One row of input values a task ran on, and what happened. Local only. */
export interface WorkItem {
  item_id: string;
  batch_id: string;
  workflow_id: string;
  position: number;
  /** Short local label from the item's first input. Can identify a record. */
  label: string;
  run_id?: string | null;
  status:
    | "queued"
    | "done"
    | "open"
    | "skipped"
    | "declined"
    | "done_by_person"
    | "confirmed_saved"
    | "retried"
    | "teaching";
  result: PlainResultKey | null;
  nothing_written: boolean;
  execution_outcome?: string | null;
  transaction_outcome?: string | null;
  halt_kind?: "wrong_record" | "screen_changed" | "save_not_confirmed" | "other" | null;
  halt_step?: string;
  halt_reason?: string;
  expected?: string;
  observed?: string;
  duration_s?: number | null;
  created_at?: string | null;
  finished_at?: string | null;
  resolved_at?: string | null;
  retry_of?: string | null;
  actions: AttentionAction[];
  /** Flow's approval-mode block for this item, as closed codes. */
  approval?: ItemApproval | null;
  /**
   * Flow saved this item with every check passed and no person, and both
   * post-run checks passed. Never true for an item a person approved or closed.
   */
  saved_without_person?: boolean;
}

/** Flow's per-item approval block, projected by engine/approval_mode.py. */
export interface ItemApproval {
  /**
   * False when Flow's own model rejected the block, or when Desktop's own
   * records didn't confirm a "no_person" decision. The item then needs a
   * person and never counts as saved without one.
   */
  validated: boolean;
  /** Why Desktop didn't accept "no_person" here (a closed code), if it didn't. */
  unconfirmed?:
    | "engine_without_switch"
    | "flow_rejected"
    | "checks_incomplete"
    | "not_verified"
    | "retry_or_repeat"
    | "caller_configuration"
    | "switch_not_on_here"
    | "switch_mismatch"
    | null;
  /** The switch the block was decided under (an opaque id), if any. */
  switch_id?: string | null;
  switch_record_sha256?: string | null;
  mode: "each_item" | "problems_only" | "not_gated" | null;
  decision:
    | "no_person"
    | "approval_required"
    | "stop_before_saving"
    | "problem"
    | "not_gated"
    | "approved"
    | "declined"
    | "unverified"
    | null;
  reason: string | null;
  needs_person: boolean;
  counts_toward_problems: boolean;
  switch_off_reason: OffReason | null;
  failed_check: { code: string; passed: boolean; detail: string } | null;
  post_checks: { verified: boolean; whole_run_clean: boolean };
  saves_without_person: number;
}

/** Why "Only ask a person about problems" turned off (design 7.6). */
export type OffReason =
  | "uncertain_result"
  | "wrong_item_found"
  | "too_many_problems"
  | "screen_changed"
  | "record_check_lost"
  | "spot_checks_overdue"
  | "version_changed"
  | "environment_changed"
  | "admission_lost"
  | "setup_lapsed"
  | "turned_off";

/**
 * The switch for one task version, as this computer sees it.
 *
 * none: never turned on here. on: on, and this computer checks every item
 * before and after each save. waiting_for_cloud: on, but the last Cloud status
 * is stale, so items wait for a person. not_used_here: on in Cloud, but a piece
 * this computer needs is missing (see blockers). off: closed for good.
 */
export type SwitchState = "none" | "on" | "waiting_for_cloud" | "not_used_here" | "off";

export interface SwitchPerson {
  name: string;
  at: string;
  role?: "owner" | "admin";
}

export interface RequirementResult {
  code: "E1" | "E2" | "E3" | "E4" | "E5" | "E6" | "E7" | "E8";
  met: boolean;
  reason: string;
  checked_by: "software" | "person" | "software_and_person";
  step_ids?: string[];
}

export interface ApprovalModeState {
  ok: boolean;
  workflow_id: string;
  state: SwitchState;
  recorded_state?: SwitchState;
  /**
   * Whether the task card shows the switch at all. False until this computer
   * has a switch for the task (the design shows it only on runners with a
   * runner setup, which isn't built yet).
   */
  visible?: boolean;
  switch_id?: string | null;
  off_reason?: OffReason | null;
  off_at?: string | null;
  off_by_this_computer?: boolean;
  /** The step a screen-changed switch-off happened at, such as "click 'Save'". */
  off_step?: string;
  turned_on?: SwitchPerson | null;
  confirmed?: SwitchPerson | null;
  blockers: string[];
  can_turn_off?: boolean;
  cloud_url?: string | null;
  evidence?: {
    switch_id: string;
    record_sha256: string;
    bundle_digest: string;
    runner_id: string;
    verifiers: number;
    profile: string;
    log_intact: boolean;
    log_events: number;
    items_finished: number;
    items_without_person: number;
  } | null;
  requirements?: {
    available: boolean;
    reason?: string;
    source?: "this_computer";
    items: RequirementResult[];
  };
  error?: string;
}

/** One task's switch, as the inbox and Results list it. */
export interface SwitchSummary {
  workflow_id: string;
  task_name: string;
  state: SwitchState;
  off_reason: OffReason | null;
  off_at: string | null;
  off_by_this_computer: boolean;
  turned_on: SwitchPerson | null;
  off_step: string;
}

export type AttentionAction =
  | "retry"
  | "skip"
  | "decline"
  | "teach"
  | "confirmed_saved"
  | "retry_after_check"
  | "done_by_person"
  | "handled";

/** An inbox entry: a stopped item, or a stopped run that Cloud sent here. */
export interface AttentionItem extends Omit<WorkItem, "batch_id" | "position"> {
  source: "item" | "hosted";
  task_name: string;
  batch_id?: string;
  position?: number;
}

export interface AttentionScreenshot {
  ok: boolean;
  data_url?: string;
  width?: number;
  height?: number;
  focus?: { x: number; y: number } | null;
  step?: string;
}

export interface ResultCounts {
  done_checked: number;
  /** Of done_checked: saved with every check passed and no person. */
  saved_without_person?: number;
  needed_person: number;
  waiting: number;
  wrong_record_prevented: number;
  done_by_person: number;
  skipped: number;
  declined: number;
  items: number;
  baseline_seconds?: number | null;
  estimated_seconds_saved?: number | null;
}

export interface ResultsSummary {
  ok: boolean;
  total: ResultCounts;
  tasks: Record<string, ResultCounts>;
  names: Record<string, string>;
  recent: (WorkItem & { task_name: string })[];
  switches?: SwitchSummary[];
}

export interface TaskInputs {
  ok: boolean;
  workflow_id: string;
  inputs?: QualificationParameter[];
  target?: ExecutionTarget | null;
  error?: string;
}

export interface RunItemResponse {
  ok: boolean;
  item?: WorkItem;
  report?: RunReport;
  pre_action_refusal?: boolean;
  error?: string;
}

export interface SampleRun {
  kind: "normal" | "save_dropped";
  result: PlainResultKey;
  nothing_written: boolean;
  execution_outcome?: string | null;
  transaction_outcome?: string | null;
  steps: { index: number; intent: string; ok: boolean; saves: boolean }[];
  right_record_checks: number;
  record_checks_confirmed: number;
  record_checks_failed: number;
  model_calls?: number | null;
  inputs: Record<string, string>;
  screen: AttentionScreenshot | null;
}

export interface SampleResponse {
  ok: boolean;
  seconds?: number;
  runs?: SampleRun[];
  error?: string;
}

export interface FirstWorkflowState {
  stage:
    | "record"
    | "review"
    | "executing"
    | "reconciliation"
    | "result"
    | "qualification"
    | "qualification_after_result"
    | "complete";
  capture_id?: string | null;
  workflow_id?: string | null;
  target?: ExecutionTarget | null;
  task?: string;
  updated_at: string;
}

export interface FirstWorkflowStateResponse {
  ok: true;
  state: FirstWorkflowState | null;
}

export type QualificationRisk =
  | "read_only"
  | "state_changing"
  | "consequential"
  | "irreversible";
export type QualificationExecutableRisk = "reversible" | "irreversible";
export type QualificationEffectKind = "record_written" | "field_equals";
export type QualificationCaseKind =
  | "representative"
  | "ambiguity"
  | "wrong_identity"
  | "stale_identity"
  | "weak_effect"
  | "missing_effect";
export type QualificationIdentitySourceKind =
  | "structured"
  | "identifier_region"
  | "captured_context"
  | "application"
  | "session"
  | "workflow_state";
export type QualificationIdentitySignalKey =
  | "subject_name"
  | "record_id"
  | "secondary_identifier"
  | "application"
  | "session"
  | "workflow_state";
export type QualificationIdentityEnforcement =
  | "canonical_ladder"
  | "signal_quorum";
export type QualificationIdentityMatch = "exact" | "normalized";
export type QualificationIdentityNormalizer =
  | "unicode_nfkc"
  | "casefold"
  | "collapse_whitespace"
  | "strip_punctuation";
export type QualificationTargetKind =
  | "web"
  | "windows"
  | "macos"
  | "linux"
  | "rdp"
  | "citrix";

export interface QualificationIdentity {
  applicable: boolean;
  armed?: boolean | null;
  reason?: string | null;
  phi_free: boolean;
  has_structured: boolean;
  has_identifier_crop: boolean;
}

export interface QualificationEffect {
  kind: string;
  summary: string;
  risk: QualificationExecutableRisk;
  needs_operator_confirmation: boolean;
}

export interface QualificationIdentitySource {
  kind: QualificationIdentitySourceKind;
  label: string;
  match: string;
  region?: [number, number, number, number];
}

export interface QualificationIdentitySignal {
  key: QualificationIdentitySignalKey;
  source: QualificationIdentitySourceKind;
  match: QualificationIdentityMatch;
  normalizers: QualificationIdentityNormalizer[];
  region?: [number, number, number, number] | null;
  extract_pattern?: string | null;
  expected_value?: string | null;
  params: string[];
}

export interface QualificationValueExpression {
  source: "parameter" | "literal";
  value?: string | null;
}

export interface QualificationEditableEffect {
  index: number;
  kind: QualificationEffectKind;
  match: Record<string, QualificationValueExpression | null>;
  field?: string | null;
  value?: QualificationValueExpression | null;
  expected_count: number;
  idempotency_key?: QualificationValueExpression | null;
  key_field: string;
  count_new_only: boolean;
  risk: QualificationExecutableRisk;
  needs_operator_confirmation: boolean;
  verification_tier?: number | null;
  effect_contract_hash: string;
}

export interface QualificationActionControls {
  step_id: string;
  execution_paths: ("gui" | "api")[];
  classification?: {
    step_id: string;
    classification: QualificationRisk | "unknown";
    explanation: string;
    operator_confirmed: boolean;
  } | null;
  identity: {
    can_arm: boolean;
    armed: boolean;
    sources: QualificationIdentitySource[];
    policy?: {
      step_id: string;
      enforcement: QualificationIdentityEnforcement;
      signals: QualificationIdentitySignal[];
      quorum: number;
    } | null;
  };
  effects: QualificationEditableEffect[];
}

export interface QualificationParameter {
  name: string;
  type: string;
  secret: boolean;
  required: boolean;
  example: string | null;
  choices: string[];
}

export interface QualificationNode {
  id: string;
  index: number;
  kind: string;
  title: string;
  action?: string | null;
  resolution?: {
    top_rung?: string | null;
    rungs: {
      name: string;
      label: string;
      present: boolean;
      detail: string;
    }[];
  } | null;
  risk?: QualificationExecutableRisk | null;
  identity?: QualificationIdentity | null;
  effects: QualificationEffect[];
  postconditions: string[];
  halts: string[];
  badges: string[];
}

export interface QualificationBusinessDecisionOption {
  id: string;
  label: string;
  value: string;
  target: string;
  required_evidence: string[];
}

export interface QualificationBusinessDecisionContract {
  schema_version: "openadapt.business-decision/v1";
  question: string;
  authorized_roles: string[];
  output_param: string;
  options: QualificationBusinessDecisionOption[];
  evidence_requirements: { id: string; label: string }[];
  expires_after_s: number;
  revalidation: {
    kind: "text_present" | "anchor_resolves";
    text: string | null;
    state_id: string | null;
  } | null;
  editable: boolean;
}

export interface QualificationBusinessDecisionState {
  id: string;
  kind: string;
  title: string;
  has_revalidation_anchor: boolean;
  can_insert_before: boolean;
  decision: QualificationBusinessDecisionContract | null;
}

export interface QualificationBusinessDecisionControls {
  available: boolean;
  required_flow_capability: "qualification.set_business_decision";
  graphs: {
    id: string;
    label: string;
    entry: string;
    states: QualificationBusinessDecisionState[];
  }[];
}

/**
 * Local-only qualification evidence for an institutional-judgment case.
 *
 * A reference never contains a screenshot, a record value, or free text. The
 * Desktop and Flow resolve it inside the customer boundary.
 */
export interface LocalEvidenceRefV1 {
  relative_path: string;
  sha256: string;
  kind: "frame" | "recording" | "report" | "document" | "system_read";
}

export type JudgmentFactTypeV1 =
  | "boolean"
  | "integer"
  | "number"
  | "string"
  | "enum";

export interface JudgmentFactFieldV1 {
  type: JudgmentFactTypeV1;
  allowed_values?: string[];
}

export interface JudgmentFactSchemaV1 {
  schema_version: "openadapt.judgment-fact-schema/v1";
  fields: Record<string, JudgmentFactFieldV1>;
}

export interface JudgmentFactSchemaBindingV1 {
  graph_id: string;
  state_id: string;
  fact_schema: JudgmentFactSchemaV1;
}

export interface JudgmentCaseProvenanceV1 {
  source: string;
  source_ref_sha256: string;
  reviewer_role: string;
  reviewer_principal_ref_sha256: string;
}

export interface JudgmentDecisionBindingV1 {
  graph_id: string;
  state_id: string;
  workflow_contract_sha256: string;
  decision_contract_sha256: string;
}

export type JudgmentDispositionV1 =
  | "automatic_rule"
  | "human_node"
  | "more_evidence_required";

/** The exact local qualification payload owned and validated by Flow. */
export interface JudgmentCaseV1 {
  id: string;
  decision: JudgmentDecisionBindingV1;
  fact_schema_sha256: string;
  facts: Record<string, boolean | number | string>;
  local_evidence: LocalEvidenceRefV1[];
  review_note_ref?: LocalEvidenceRefV1 | null;
  provenance: JudgmentCaseProvenanceV1;
  disposition: JudgmentDispositionV1;
  reviewed_rule_id?: string | null;
  option_id?: string | null;
  contrast_case_ids: string[];
}

/**
 * Flow supplies this local read model. It is intentionally not a portable
 * decision-task type and it cannot authorize a runtime answer.
 */
export interface JudgmentCaseCaptureContextV1 {
  decision: JudgmentDecisionBindingV1;
  fact_schema: JudgmentFactSchemaV1;
  fact_schema_sha256: string;
  options: { id: string; label: string }[];
  authorized_roles: string[];
  cases: JudgmentCaseV1[];
}

export interface JudgmentCaseQualificationReportV1 {
  schema_version: "openadapt.judgment-case-report/v1";
  workflow_contract_sha256: string;
  passed: boolean;
  case_count: number;
  automatic_case_count: number;
  retained_human_authority_count: number;
  findings: { code: string; case_id?: string | null; message: string }[];
}

export interface QualificationJudgmentCaseControls {
  available: boolean;
  required_flow_capability: "qualification.set_judgment_cases";
  contexts: JudgmentCaseCaptureContextV1[];
  report: JudgmentCaseQualificationReportV1 | null;
}

export interface QualificationViolation {
  rule: string;
  step_id?: string | null;
  reason: string;
}

export interface QualificationFinding {
  severity: "info" | "warn" | "error";
  code: string;
  step_id?: string | null;
  message: string;
}

export interface QualificationProject {
  ok: true;
  workflow_id: string;
  policy: string;
  qualification_schema: "openadapt.qualification-project/v1";
  migration_required: boolean;
  draft_environment?: boolean;
  project: {
    schema_version: "openadapt.qualification-project/v1";
    project_id: string;
    revision: number;
    environment: {
      target_kind: QualificationTargetKind;
      application: string;
      application_version: string;
      environment_digest: string;
      runtime_version: string;
      required_capabilities: string[];
    };
    minimum_effect_tier: number;
    cases: {
      id: string;
      kind: string;
      description: string;
      input_ref?: string | null;
      expected_outcome: string;
      required: boolean;
      action_targets?: { step_id: string; actuation_path: "gui" | "api" }[];
      fault_target?: { step_id: string; actuation_path: "gui" | "api" } | null;
      results: {
        project_revision: number;
        runner_capabilities: string[];
        status: "passed" | "failed" | "blocked";
        observed_outcome: string;
        evidence: {
          kind: string;
          sha256: string;
          relative_path: string;
        }[];
      }[];
    }[];
    last_certification?: {
      passed: boolean;
      project_revision: number;
      report_sha256: string;
      certified_at: string;
    } | null;
  } | null;
  capability_coverage: {
    required: string[];
    observed: string[];
    missing: string[];
    satisfied: boolean;
    cases: {
      case_id: string;
      has_current_receipt: boolean;
      has_current_result: boolean;
      status: "passed" | "failed" | "blocked" | null;
      observed: string[];
      missing: string[];
      runtime_version: string | null;
      target_kind: QualificationTargetKind | null;
    }[];
  };
  report: {
    schema_version: "openadapt.qualification-report/v1";
    passed: boolean;
    action_count: number;
    state_changing_action_count: number;
    consequential_action_count: number;
    identity_covered_action_count: number;
    effect_required_action_count: number;
    effect_covered_action_count: number;
    minimum_effect_tier?: number | null;
    case_count: number;
    passed_case_count: number;
    refusals: {
      code: string;
      path: string;
      message: string;
      step_id?: string | null;
      case_id?: string | null;
      details: Record<string, string | number | boolean>;
    }[];
  };
  certification_current: boolean;
  graph: {
    bundle: {
      name: string;
      action_count: number;
      irreversible_count: number;
      identity_armed_count: number;
      identity_unarmed_count: number;
      effect_count: number;
      encrypted: boolean;
      provenance: {
        content_digest?: string | null;
      };
    };
    nodes: QualificationNode[];
    edges: { source: string; target: string; kind: string; label: string }[];
  };
  lint: {
    findings: QualificationFinding[];
    consequential_steps: number;
    effect_covered_consequential_steps: number;
  };
  certification: {
    policy_name: string;
    workflow_name: string;
    passed: boolean;
    n_steps: number;
    violations: QualificationViolation[];
  };
  provenance: {
    policy_name?: string | null;
    certified: boolean;
    certification_status?: string | null;
    certified_at?: string | null;
  };
  controls: {
    parameters: QualificationParameter[];
    actions: Record<string, QualificationActionControls>;
    business_decisions: QualificationBusinessDecisionControls;
    judgment_cases: QualificationJudgmentCaseControls;
  };
}

export interface QualificationRefusal {
  ok: false;
  workflow_id: string;
  error: string;
}

export type QualificationResponse =
  | QualificationProject
  | QualificationRefusal;

export interface RunStep {
  index: number;
  action: string;
  target: string;
  state: StepState;
  latency_ms?: number | null;
  effect?: "verified" | "not_verified" | "checking" | null;
}

export interface RunReport {
  ok: boolean;
  outcome: ExecutionOutcome;
  /** Flow's transaction outcome, forwarded only when consistent with `outcome`. */
  transaction_outcome?: string | null;
  stop_kind?: WorkItem["halt_kind"];
  pre_action_refusal: false;
  error?: string;
  run_id: string;
  workflow_id: string;
  workflow_name: string;
  total_steps: number;
  steps: RunStep[];
  halt?: {
    step_index: number;
    step_intent: string;
    reason: string;
    resolver_rung?: string;
  } | null;
  metrics?: { duration_s?: number; cost_usd?: number } | null;
  outcome_details?: {
    profile: "demo" | "standard" | "regulated" | null;
    production_eligible: boolean;
    execution_completed: boolean;
    required_contracts: ExecutionContractCounts;
    passed_contracts: ExecutionContractCounts;
    evidence_classes: string[];
    model_calls: number;
    external_network_calls: "none" | "observed" | "unknown";
    compensation_actions: number;
  } | null;
  persistence?: {
    state: "persisted" | "degraded" | "failed";
    retryable: boolean;
    message: string;
  };
}

export interface RunPersistenceRetryResponse {
  ok: boolean;
  report?: RunReport;
  error?: string;
}

export interface ExecutionContractCounts {
  authorization: number;
  identity: number;
  postcondition: number;
  effect: number;
}

export interface ExecutionRefusal {
  ok: false;
  outcome: "refused";
  pre_action_refusal: true;
  error: string;
}

export type ExecutionResponse = RunReport | ExecutionRefusal;

export type PreciseExecutionOutcome =
  | "VERIFIED"
  | "COMPLETED_UNVERIFIED"
  | "HALTED"
  | "FAILED"
  | "ROLLED_BACK";

export type ExecutionOutcome =
  | PreciseExecutionOutcome
  | "success"
  | "halt"
  | "unknown";

export interface BrowserRuntimeStatus {
  workflow_id: string;
  state: "checking" | "installing" | "ready" | "error";
  detail: string;
}

export interface ReplayProgress {
  workflow_id: string;
  state:
    | "running"
    | "halted"
    | "done"
    | "completed_unverified"
    | "failed"
    | "rolled_back"
    | "unknown"
    | "error";
  backend: TargetBackend | "configured";
  /** Precise runtime contract outcome. Legacy state alone never proves VERIFIED. */
  outcome?: ExecutionOutcome;
  mode?: "replay" | "governed" | "managed";
  profile?: "demo" | "standard" | "regulated" | null;
  current_step?: number | null;
  total_steps?: number | null;
  duration_s?: number | null;
  evidence_classes?: string[];
  model_calls?: number | null;
  external_network_calls?: "none" | "observed" | "unknown" | null;
}

export interface SyncState {
  state: "synced" | "pushing" | "offline" | "paused";
  queued: number;
}

export interface NeedsAttention {
  count: number;
  open_halts: number;
  open_items?: number;
  failed_runs: number;
}

export interface PermissionStatus {
  screen_recording: boolean;
  accessibility: boolean;
  input_monitoring: boolean;
}

export interface FfmpegRuntimeStatus {
  phase:
    | "checking"
    | "downloading"
    | "verifying"
    | "ready"
    | "error"
    | "unavailable";
  source: "managed" | "override";
  runtime_version: string;
  target: string;
  path?: string | null;
  ffprobe_path?: string | null;
  detail?: string | null;
}

export interface PresentationExportStatus {
  ready: boolean;
  reason?: string | null;
  media_sha256?: string;
  media_frame_count?: number;
}

export interface PresentationExportResult {
  ok: true;
  path: string;
  sha256: string;
  source_media_sha256: string;
  media_frame_count: number;
  raw_media_unchanged: true;
  placement_policy: "step-stable-collision-aware-bottom-corner";
}

// Hosted runner state and terminal outcomes.

export type RunnerState =
  | "disabled"
  | "stopping"
  | "offline"
  | "polling"
  | "running"
  | "reauth_required"
  | "incompatible"
  | "error";

export interface RunnerRun {
  run_id: string;
  phase?: string;
  outcome?: string | null;
  reason?: string | null;
  updated_at?: string;
  workflow_id?: string | null;
}

export interface RunnerStatus {
  enabled: boolean;
  state: RunnerState;
  runner_id?: string | null;
  registered?: boolean;
  host?: string;
  last_error?: string | null;
  last_seen_at?: string | null;
  last_runs: RunnerRun[];
}

// The grounding-model config (engine runtime.grounding_model), resolved from the
// cloud effective policy. Admin-scoped Tier-3 egress capability: OFF by default,
// fail-closed. The desktop renders it READ-ONLY (the canonical write path is the
// cloud dashboard) — the raw API key is never here, only the env-var NAME.
export type GroundingProvider = "anthropic" | "openai_compatible";
export interface GroundingModelConfig {
  enabled: boolean;
  provider: GroundingProvider;
  base_url: string;
  model: string;
  api_key_env: string;
  phi_grounding_allowlist: string[];
  phi_egress_attested: boolean;
}

// A minimal view of GET /api/policy/effective (resolved by the cloud control
// plane, fetched + cached fail-closed by the engine — see engine/policy.py /
// docs/POLICY_SYNC.md). Only the fields the grounding-model section needs are
// typed here.
export interface EffectivePolicy {
  is_admin: boolean;
  grounding_model: GroundingModelConfig;
  resolved_at?: string;
  offline?: boolean;
}

/** A run waiting for a person to approve a save (``get_item_approval``). */
export interface ItemApprovalState {
  ok: boolean;
  running: boolean;
  waiting: boolean;
  workflow_id?: string;
  /** "practice" for Check it's practice item, "item" for Run it. */
  kind?: "practice" | "item";
  error?: string;
  request?: {
    request_id: string;
    request_sha256: string;
    step_id: string;
    step_intent: string;
    /** When the run stops waiting and stops without saving. */
    answer_by: string | null;
    /** The filled form as a data URL, or null when it can't be shown. */
    screenshot: string | null;
    /** The item's own values, as given. Local only. */
    values: { name: string; value: string }[];
  };
}

export interface ItemApprovalAnswer {
  ok: boolean;
  decision?: "approved" | "declined";
  error?: string;
  code?: string;
}
