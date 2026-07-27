// Shared frontend types mirroring the engine's IPC payloads (spec §3).

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
  kind?: "ingest_token" | "supabase_session";
  host?: string;
  org_id?: string | null;
}

export interface EngineStatus {
  recording: boolean;
  paused: boolean;
  duration_secs?: number | null;
  capture_id?: string | null;
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
    parameters: {
      name: string;
      type: string;
      secret: boolean;
    }[];
    actions: Record<string, QualificationActionControls>;
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

// Runner lane (EXPERIMENTAL — outbound dispatch loop, spec §2).

export type RunnerState =
  | "disabled"
  | "offline"
  | "polling"
  | "running"
  | "reauth_required"
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
