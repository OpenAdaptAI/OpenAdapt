// Synthetic fixture data for the cockpit capture harness.
//
// Every value here is invented for a screenshot. There is no customer, no
// organization, no recording, no engine, and no run behind any of it. The
// harness answers the engine IPC surface with this table so the eight
// published cockpit images can be re-shot from source instead of from a
// throwaway adapter that lived only in one branch.
//
// Keep the shapes in step with src/lib/types.ts. A field the engine would send
// but this table omits shows up as an empty region in a published image.

const HALTED_WORKFLOW = 'wf-claims-review-handoff';
const VERIFIED_WORKFLOW = 'wf-invoice-review-handoff';

export const WORKFLOWS = [
  {
    id: HALTED_WORKFLOW,
    name: 'Claims review handoff',
    steps: 6,
    updated_at: '2026-08-26T14:12:00Z',
    last_run_state: 'halted',
    open_halts: 1,
    synced: true,
  },
  {
    id: VERIFIED_WORKFLOW,
    name: 'Invoice review handoff',
    steps: 6,
    updated_at: '2026-08-26T13:48:00Z',
    last_run_state: 'verified',
    open_halts: 0,
    synced: true,
  },
];

const STEPS = [
  { index: 0, action: 'open', target: 'Practice portal', state: 'verified', latency_ms: 412, effect: null },
  { index: 1, action: 'click', target: 'Claims', state: 'verified', latency_ms: 168, effect: null },
  { index: 2, action: 'type', target: 'Member ID', state: 'verified', latency_ms: 240, effect: null },
  { index: 3, action: 'click', target: 'Search', state: 'verified', latency_ms: 191, effect: null },
];

const HALTED_STEPS = [
  ...STEPS,
  { index: 4, action: 'click', target: 'Save', state: 'halted', latency_ms: 305, effect: 'not_verified' },
  { index: 5, action: 'click', target: 'Close', state: 'pending', latency_ms: null, effect: null },
];

const VERIFIED_STEPS = [
  ...STEPS,
  { index: 4, action: 'click', target: 'Save', state: 'verified', latency_ms: 288, effect: 'verified' },
  { index: 5, action: 'click', target: 'Close', state: 'verified', latency_ms: 154, effect: null },
];

export const HALTED_REPORT = {
  ok: false,
  outcome: 'ROLLED_BACK',
  pre_action_refusal: false,
  run_id: 'run-2026-08-26-0004',
  workflow_id: HALTED_WORKFLOW,
  workflow_name: 'Claims review handoff',
  total_steps: 6,
  steps: HALTED_STEPS,
  halt: {
    step_index: 4,
    step_intent: 'Save the claim status note on the member record',
    reason:
      'The record on screen did not match the member the run was told to act on, so the write was not attempted.',
    resolver_rung: 'relative geometry',
  },
  metrics: { duration_s: 14.8, cost_usd: 0.031 },
  outcome_details: {
    profile: 'standard',
    production_eligible: false,
    execution_completed: true,
    required_contracts: { authorization: 1, identity: 2, postcondition: 1, effect: 1 },
    passed_contracts: { authorization: 1, identity: 1, postcondition: 1, effect: 0 },
    evidence_classes: ['authorization', 'identity', 'compensation'],
    model_calls: 2,
    external_network_calls: 'observed',
    compensation_actions: 1,
  },
  persistence: {
    state: 'persisted',
    retryable: false,
    message: 'The report is saved in local history.',
  },
};

export const VERIFIED_REPORT = {
  ok: true,
  outcome: 'VERIFIED',
  pre_action_refusal: false,
  run_id: 'run-2026-08-26-0007',
  workflow_id: VERIFIED_WORKFLOW,
  workflow_name: 'Invoice review handoff',
  total_steps: 6,
  steps: VERIFIED_STEPS,
  halt: null,
  metrics: { duration_s: 11.2, cost_usd: 0.024 },
  outcome_details: {
    profile: 'standard',
    production_eligible: true,
    execution_completed: true,
    required_contracts: { authorization: 1, identity: 2, postcondition: 1, effect: 1 },
    passed_contracts: { authorization: 1, identity: 2, postcondition: 1, effect: 1 },
    evidence_classes: ['authorization', 'identity', 'effect_tier_1'],
    model_calls: 0,
    external_network_calls: 'none',
    compensation_actions: 0,
  },
  persistence: {
    state: 'persisted',
    retryable: false,
    message: 'The report is saved in local history.',
  },
};

export const CAPABILITY_REPORT = {
  schema: 'openadapt-desktop.capability-report/v1',
  generated_at: '2026-08-26T14:00:00Z',
  host: { os: 'macos', os_version: '15.6', arch: 'aarch64', app_version: '0.15.0' },
  surfaces: {
    web: { state: 'available', detail: 'Managed browser runtime is installed.', remediation: null, driver: null },
    macos: { state: 'available', detail: 'Accessibility and screen recording are granted.', remediation: null, driver: null },
    windows: { state: 'unsupported_host', detail: 'This surface needs a Windows host.', remediation: null, driver: null },
    linux: { state: 'unsupported_host', detail: 'This surface needs a Linux host.', remediation: null, driver: null },
    rdp: { state: 'driver_required', detail: 'No RDP client driver was detected.', remediation: 'Install the OpenAdapt RDP driver.', driver: null },
    citrix: { state: 'driver_required', detail: 'No Citrix Workspace driver was detected.', remediation: 'Install Citrix Workspace.', driver: null },
  },
};

const REPORTS = {
  [HALTED_WORKFLOW]: HALTED_REPORT,
  [VERIFIED_WORKFLOW]: VERIFIED_REPORT,
};

// The IPC surface, as a plain table so it serializes into the page.
export function ipcTable({ authenticated, workflows, permissions }) {
  return {
    get_auth_status: authenticated
      ? { authenticated: true, kind: 'ingest_token', host: 'app.openadapt.ai', org_id: 'org_demo' }
      : { authenticated: false },
    get_workflows: workflows,
    get_first_workflow_state: { ok: true, state: null },
    get_needs_attention: {
      count: workflows.some((w) => w.open_halts) ? 1 : 0,
      open_halts: workflows.some((w) => w.open_halts) ? 1 : 0,
      failed_runs: 0,
    },
    get_sync_state: { state: 'synced', queued: 0 },
    get_status: { recording: false, paused: false, duration_secs: null, capture_id: null, controls: { pause: false, resume: false, stop: false } },
    get_captures: [],
    get_storage_usage: { bytes: 0, captures: 0 },
    get_capabilities: CAPABILITY_REPORT,
    check_permissions: permissions,
    get_run_report: REPORTS,
    get_qualification: null,
    get_effective_policy: null,
    get_presentation_export_status: { ready: false, reason: 'Nothing is recorded yet.' },
    get_config: {
      lane: 'byoc',
      phi_mode: 'on',
      host: 'app.openadapt.ai',
      grounding: 'managed',
      overlay: true,
    },
    portal_status: { running: false, devices: [], ingress: 'loopback' },
    portal_devices: [],
    runner_status: { enabled: false, state: 'disabled' },
    get_pending_reviews: [],
    sidecar_status: true,
    ffmpeg_status: { phase: 'ready', source: 'managed', runtime_version: '7.1', target: 'macos-aarch64' },
  };
}

export const IDS = { HALTED_WORKFLOW, VERIFIED_WORKFLOW };
