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

// The compiled program behind the halted workflow, as the qualification
// projection shapes it for src/ui/ProgramWorkbench.tsx. The six run steps above
// are the executable actions here; the loop, branch, and terminal nodes are the
// control structure the compiler recovered around them.
const QUALIFICATION_LADDER = (present) => [
  { name: 'structural', label: 'Structural', present: present.includes('structural'), detail: '' },
  { name: 'template', label: 'Template', present: present.includes('template'), detail: '' },
  { name: 'ocr', label: 'OCR anchor', present: present.includes('ocr'), detail: '' },
  { name: 'geometry', label: 'Geometry', present: present.includes('geometry'), detail: '' },
];

const ARMED_IDENTITY = {
  applicable: true,
  armed: true,
  phi_free: true,
  has_structured: true,
  has_identifier_crop: true,
};

const QUALIFICATION_NODES = [
  {
    id: 'open_portal',
    index: 0,
    kind: 'action',
    title: 'Open the practice portal',
    action: 'open',
    resolution: { top_rung: 'structural', rungs: QUALIFICATION_LADDER(['structural', 'template']) },
    risk: 'reversible',
    identity: null,
    effects: [],
    postconditions: ['the portal home view is visible'],
    halts: ['the portal does not reach a signed-in state'],
    badges: [],
  },
  {
    id: 'for_each_claim',
    index: 1,
    kind: 'loop',
    title: 'For each claim in the worklist',
    action: null,
    resolution: null,
    risk: null,
    identity: null,
    effects: [],
    postconditions: [],
    halts: [],
    badges: [],
  },
  {
    id: 'open_claims_tab',
    index: 2,
    kind: 'action',
    title: 'Open the claims tab',
    action: 'click',
    resolution: { top_rung: 'structural', rungs: QUALIFICATION_LADDER(['structural', 'template', 'ocr']) },
    risk: 'reversible',
    identity: null,
    effects: [],
    postconditions: ['the claims list is visible'],
    halts: ['the claims tab cannot be resolved uniquely'],
    badges: [],
  },
  {
    id: 'type_member_id',
    index: 3,
    kind: 'action',
    title: 'Enter the member identifier',
    action: 'type',
    resolution: { top_rung: 'structural', rungs: QUALIFICATION_LADDER(['structural', 'template']) },
    risk: 'reversible',
    identity: ARMED_IDENTITY,
    effects: [],
    postconditions: ['the member identifier field holds the declared value'],
    halts: ['the identifier field is not editable'],
    badges: ['identity armed'],
  },
  {
    id: 'search_claim',
    index: 4,
    kind: 'action',
    title: 'Search for the member record',
    action: 'click',
    resolution: { top_rung: 'template', rungs: QUALIFICATION_LADDER(['template', 'ocr', 'geometry']) },
    risk: 'reversible',
    identity: ARMED_IDENTITY,
    effects: [],
    postconditions: ['exactly one member record is shown'],
    halts: ['identity does not match', 'more than one record matches'],
    badges: ['identity armed'],
  },
  {
    id: 'save_status_note',
    index: 5,
    kind: 'action',
    title: 'Save the claim status note',
    action: 'click',
    resolution: { top_rung: 'structural', rungs: QUALIFICATION_LADDER(['structural', 'template']) },
    risk: 'irreversible',
    identity: ARMED_IDENTITY,
    effects: [
      { kind: 'record_written', risk: 'irreversible', needs_operator_confirmation: false },
    ],
    postconditions: ['the saved note is readable on the member record'],
    halts: ['the fresh frame changed', 'the effect cannot be verified independently'],
    badges: ['consequential'],
  },
  {
    id: 'close_claim',
    index: 6,
    kind: 'action',
    title: 'Close the claim',
    action: 'click',
    resolution: { top_rung: 'structural', rungs: QUALIFICATION_LADDER(['structural']) },
    risk: 'reversible',
    identity: null,
    effects: [],
    postconditions: ['the worklist is visible again'],
    halts: ['the claim view does not close'],
    badges: [],
  },
  {
    id: 'more_claims',
    index: 7,
    kind: 'branch',
    title: 'More claims in the worklist?',
    action: null,
    resolution: null,
    risk: null,
    identity: null,
    effects: [],
    postconditions: [],
    halts: [],
    badges: [],
  },
  {
    id: 'done',
    index: 8,
    kind: 'terminal',
    title: 'End of declared steps',
    action: null,
    resolution: null,
    risk: null,
    identity: null,
    effects: [],
    postconditions: [],
    halts: [],
    badges: [],
  },
];

const QUALIFICATION_GRAPH = {
  bundle: {
    name: 'Claims review handoff',
    action_count: 6,
    irreversible_count: 1,
    identity_armed_count: 3,
    identity_unarmed_count: 0,
    effect_count: 1,
    encrypted: false,
    provenance: { content_digest: '3f9b17c4a2d0' },
  },
  nodes: QUALIFICATION_NODES,
  edges: [
    { source: 'open_portal', target: 'for_each_claim', kind: 'next', label: 'portal ready' },
    { source: 'for_each_claim', target: 'open_claims_tab', kind: 'loop_body', label: 'next claim' },
    { source: 'for_each_claim', target: 'done', kind: 'loop_exit', label: 'worklist empty' },
    { source: 'open_claims_tab', target: 'type_member_id', kind: 'next', label: 'claims list shown' },
    { source: 'type_member_id', target: 'search_claim', kind: 'next', label: 'identifier entered' },
    { source: 'search_claim', target: 'save_status_note', kind: 'next', label: 'identity verified' },
    { source: 'save_status_note', target: 'close_claim', kind: 'next', label: 'effect verified' },
    { source: 'close_claim', target: 'more_claims', kind: 'next', label: 'claim closed' },
    { source: 'more_claims', target: 'for_each_claim', kind: 'branch', label: 'more' },
    { source: 'more_claims', target: 'done', kind: 'branch', label: 'complete' },
  ],
};

const actionControls = (stepId, classification, armed) => ({
  step_id: stepId,
  execution_paths: ['gui'],
  classification: {
    step_id: stepId,
    classification,
    explanation: 'Reviewed and confirmed by the operator during qualification.',
    operator_confirmed: true,
  },
  identity: {
    can_arm: armed,
    armed,
    sources: armed
      ? [{ kind: 'structured', label: 'Member identifier field', match: 'normalized' }]
      : [],
    policy: armed
      ? {
          step_id: stepId,
          enforcement: 'canonical_ladder',
          signals: [
            {
              key: 'record_id',
              source: 'structured',
              match: 'normalized',
              normalizers: ['unicode_nfkc', 'collapse_whitespace'],
              region: null,
              extract_pattern: null,
              expected_value: null,
              params: ['member_id'],
            },
          ],
          quorum: 1,
        }
      : null,
  },
  effects: stepId === 'save_status_note'
    ? [
        {
          index: 0,
          kind: 'record_written',
          match: { record_id: { source: 'parameter', value: 'member_id' } },
          field: 'status_note',
          value: { source: 'parameter', value: 'status_note' },
          expected_count: 1,
          idempotency_key: null,
          key_field: 'claim_id',
          count_new_only: true,
          risk: 'irreversible',
          needs_operator_confirmation: false,
          verification_tier: 2,
          effect_contract_hash: 'b71d5a0c94e2f338',
        },
      ]
    : [],
});

// The qualification projection for the halted workflow. Every value is
// invented. There is no project, policy engine, certification, or case runner
// behind it; the table below answers `get_qualification` so the cockpit's own
// qualification screen renders exactly as it does against a real engine.
export const QUALIFICATION = {
  ok: true,
  workflow_id: HALTED_WORKFLOW,
  policy: 'clinical-write',
  qualification_schema: 'openadapt.qualification-project/v1',
  migration_required: false,
  draft_environment: false,
  project: {
    schema_version: 'openadapt.qualification-project/v1',
    project_id: 'proj-claims-review-handoff',
    revision: 4,
    environment: {
      target_kind: 'web',
      application: 'Practice portal',
      application_version: '11.4',
      environment_digest: 'e2c81f4a6b90',
      runtime_version: '0.15.0',
      required_capabilities: ['web.chromium', 'ocr.local'],
    },
    minimum_effect_tier: 2,
    cases: [
      {
        id: 'case-nominal',
        kind: 'nominal',
        description: 'One claim in the worklist reaches a saved status note.',
        input_ref: null,
        expected_outcome: 'The status note is readable on the member record.',
        required: true,
        results: [
          {
            project_revision: 4,
            runner_capabilities: ['web.chromium', 'ocr.local'],
            status: 'passed',
            observed_outcome: 'The status note is readable on the member record.',
            evidence: [
              { kind: 'report', sha256: '9c2a4e17f0b6d853', relative_path: 'cases/case-nominal/report.json' },
            ],
          },
        ],
      },
      {
        id: 'case-wrong-record',
        kind: 'fault',
        description: 'The search returns a member who is not the declared member.',
        input_ref: null,
        expected_outcome: 'The run halts before the write.',
        required: true,
        results: [
          {
            project_revision: 4,
            runner_capabilities: ['web.chromium', 'ocr.local'],
            status: 'passed',
            observed_outcome: 'The run halted before the write.',
            evidence: [
              { kind: 'report', sha256: '4d70b83c15ae2f69', relative_path: 'cases/case-wrong-record/report.json' },
            ],
          },
        ],
      },
    ],
    last_certification: null,
  },
  capability_coverage: {
    required: ['web.chromium', 'ocr.local'],
    observed: ['web.chromium', 'ocr.local'],
    missing: [],
    satisfied: true,
    cases: [
      {
        case_id: 'case-nominal',
        has_current_receipt: true,
        has_current_result: true,
        status: 'passed',
        observed: ['web.chromium', 'ocr.local'],
        missing: [],
        runtime_version: '0.15.0',
        target_kind: 'web',
      },
      {
        case_id: 'case-wrong-record',
        has_current_receipt: true,
        has_current_result: true,
        status: 'passed',
        observed: ['web.chromium', 'ocr.local'],
        missing: [],
        runtime_version: '0.15.0',
        target_kind: 'web',
      },
    ],
  },
  report: {
    schema_version: 'openadapt.qualification-report/v1',
    passed: true,
    action_count: 6,
    state_changing_action_count: 2,
    consequential_action_count: 1,
    identity_covered_action_count: 1,
    effect_required_action_count: 1,
    effect_covered_action_count: 1,
    minimum_effect_tier: 2,
    case_count: 2,
    passed_case_count: 2,
    refusals: [],
  },
  certification_current: false,
  graph: QUALIFICATION_GRAPH,
  lint: {
    findings: [
      {
        severity: 'info',
        code: 'identity_armed',
        step_id: 'save_status_note',
        message: 'The consequential write carries an armed identity gate and an independent effect check.',
      },
    ],
    consequential_steps: 1,
    effect_covered_consequential_steps: 1,
  },
  certification: {
    policy_name: 'clinical-write',
    workflow_name: 'Claims review handoff',
    passed: true,
    n_steps: 6,
    violations: [],
  },
  provenance: {
    policy_name: 'clinical-write',
    certified: false,
    certification_status: 'not_run_for_this_revision',
    certified_at: null,
  },
  controls: {
    parameters: [
      { name: 'member_id', type: 'string', secret: false, required: true, example: 'M-4821', choices: [] },
      { name: 'status_note', type: 'string', secret: false, required: true, example: 'Reviewed', choices: [] },
    ],
    actions: {
      open_portal: actionControls('open_portal', 'read_only', false),
      open_claims_tab: actionControls('open_claims_tab', 'read_only', false),
      type_member_id: actionControls('type_member_id', 'state_changing', true),
      search_claim: actionControls('search_claim', 'state_changing', true),
      save_status_note: actionControls('save_status_note', 'irreversible', true),
      close_claim: actionControls('close_claim', 'read_only', false),
    },
    business_decisions: {
      available: true,
      required_flow_capability: 'qualification.set_business_decision',
      graphs: [
        {
          id: 'claims-review-handoff',
          label: 'Claims review handoff',
          entry: 'open_portal',
          states: [
            {
              id: 'more_claims',
              kind: 'branch',
              title: 'More claims in the worklist?',
              has_revalidation_anchor: true,
              can_insert_before: true,
              decision: null,
            },
          ],
        },
      ],
    },
    judgment_cases: {
      available: false,
      required_flow_capability: 'qualification.set_judgment_cases',
      contexts: [],
      report: null,
    },
  },
};

const QUALIFICATIONS = {
  [HALTED_WORKFLOW]: QUALIFICATION,
  // The verified workflow has no qualification fixture. A refusal is the shape
  // the engine sends, so a mis-aimed capture shows an honest message instead of
  // a half-rendered screen.
  [VERIFIED_WORKFLOW]: {
    ok: false,
    workflow_id: VERIFIED_WORKFLOW,
    error: 'This workflow has no qualification project yet.',
  },
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
    get_qualification: QUALIFICATIONS,
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
