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
const NEW_WORKFLOW = 'wf-referral-intake';

// Fields the operator screens read (readiness, open items, the task text, the
// recording length) are part of the same projection the engine sends.
export const WORKFLOWS = [
  {
    id: HALTED_WORKFLOW,
    name: 'Claims review handoff',
    steps: 6,
    updated_at: '2026-08-26T14:12:00Z',
    last_run_state: 'halted',
    open_halts: 1,
    synced: true,
    last_result: 'stopped_before_saving',
    open_items: 1,
    blocked: false,
    readiness: 'ready',
    certified: true,
    encrypted: true,
    version: 2,
    capture_id: 'cap-claims',
    task: 'Add a claim status note to a member record',
    demo_seconds: 74,
  },
  {
    id: VERIFIED_WORKFLOW,
    name: 'Invoice review handoff',
    steps: 6,
    updated_at: '2026-08-26T13:48:00Z',
    last_run_state: 'verified',
    open_halts: 0,
    synced: true,
    last_result: 'done_checked',
    open_items: 0,
    blocked: false,
    readiness: 'checking',
    certified: false,
    encrypted: false,
    version: 1,
    capture_id: 'cap-invoice',
    task: 'Mark an invoice as reviewed and hand it to billing',
    demo_seconds: 52,
  },
];

// A third task, only in the operator-screen captures: shown, not yet checked.
export const NEW_TASK = {
  id: NEW_WORKFLOW,
  name: 'Referral intake entry',
  steps: 9,
  updated_at: '2026-08-26T15:02:00Z',
  last_run_state: null,
  open_halts: 0,
  synced: false,
  last_result: null,
  open_items: 0,
  blocked: false,
  readiness: 'new',
  certified: false,
  encrypted: false,
  version: 1,
  capture_id: 'cap-referral',
  task: 'Enter a faxed referral into the EMR',
  demo_seconds: 640,
};

// A generic, synthetic application screen. No vendor's layout, name, or
// trade dress; every value is invented.
function syntheticScreen({ banner, member, note, saved }) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="800" viewBox="0 0 1280 800">
<rect width="1280" height="800" fill="#f4f6f8"/>
<rect width="1280" height="56" fill="#2f4858"/>
<text x="28" y="35" font-family="Helvetica, Arial, sans-serif" font-size="20" fill="#ffffff" font-weight="700">Sample practice portal</text>
<text x="300" y="35" font-family="Helvetica, Arial, sans-serif" font-size="14" fill="#d7e1e8">Synthetic data for screenshots</text>
<rect x="28" y="84" width="880" height="52" fill="#e6edf3" stroke="#9fb3c4"/>
<text x="46" y="117" font-family="Helvetica, Arial, sans-serif" font-size="18" fill="#1d2a33">${member}</text>
${banner ? `<rect x="28" y="152" width="880" height="44" fill="#dff3e6" stroke="#5aa874"/><text x="46" y="180" font-family="Helvetica, Arial, sans-serif" font-size="16" fill="#1e5b33">${banner}</text>` : ''}
<text x="28" y="246" font-family="Helvetica, Arial, sans-serif" font-size="16" fill="#3b4a55">Claim status note</text>
<rect x="28" y="258" width="560" height="44" fill="#ffffff" stroke="#9fb3c4"/>
<text x="42" y="286" font-family="Helvetica, Arial, sans-serif" font-size="16" fill="#1d2a33">${note}</text>
<rect x="28" y="330" width="150" height="42" rx="4" fill="${saved ? '#8aa0b1' : '#2f6fa3'}"/>
<text x="76" y="357" font-family="Helvetica, Arial, sans-serif" font-size="16" fill="#ffffff">Save</text>
<text x="28" y="430" font-family="Helvetica, Arial, sans-serif" font-size="18" fill="#1d2a33" font-weight="700">Notes on this claim</text>
<text x="40" y="462" font-family="Helvetica, Arial, sans-serif" font-size="15" fill="#3b4a55">${saved ? 'Reviewed (today)' : 'No notes yet'}</text>
</svg>`;
  return `data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`;
}

const STOPPED_SCREEN = syntheticScreen({
  banner: '',
  member: 'Member M-4822   Alex Sample   DOB 1980-01-01',
  note: 'Reviewed',
  saved: false,
});

export const ATTENTION_ITEMS = [
  {
    item_id: 'item-0001',
    source: 'item',
    workflow_id: HALTED_WORKFLOW,
    task_name: 'Claims review handoff',
    batch_id: 'batch-1',
    position: 3,
    label: 'M-4821',
    run_id: 'run-0004',
    status: 'open',
    result: 'stopped_before_saving',
    nothing_written: true,
    execution_outcome: 'HALTED',
    transaction_outcome: 'HALTED_BEFORE_EFFECT',
    halt_kind: 'wrong_record',
    halt_step: "click 'Save'",
    halt_reason: 'Identity mismatch on the record banner before the save.',
    expected: 'M-4821',
    observed: 'M-4822',
    created_at: '2026-08-26T14:12:00Z',
    actions: ['retry', 'skip', 'decline', 'teach'],
  },
  {
    item_id: 'item-0002',
    source: 'item',
    workflow_id: HALTED_WORKFLOW,
    task_name: 'Claims review handoff',
    batch_id: 'batch-1',
    position: 7,
    label: 'M-5530',
    run_id: 'run-0008',
    status: 'open',
    result: 'check_record',
    nothing_written: false,
    execution_outcome: 'HALTED',
    transaction_outcome: 'RECONCILIATION_REQUIRED',
    halt_kind: 'save_not_confirmed',
    halt_step: "click 'Save'",
    halt_reason: 'The record check could not confirm the saved note.',
    expected: '',
    observed: '',
    created_at: '2026-08-26T14:31:00Z',
    actions: ['confirmed_saved', 'retry_after_check', 'done_by_person'],
  },
];

export const ATTENTION_SCREENSHOT = {
  ok: true,
  data_url: STOPPED_SCREEN,
  width: 1280,
  height: 800,
  focus: { x: 300, y: 110 },
  step: "click 'Save'",
};

export const RESULTS_SUMMARY = {
  ok: true,
  total: {
    done_checked: 38, needed_person: 4, waiting: 2, wrong_record_prevented: 1,
    done_by_person: 1, skipped: 1, declined: 0, items: 42,
  },
  tasks: {
    [HALTED_WORKFLOW]: {
      done_checked: 31, needed_person: 3, waiting: 2, wrong_record_prevented: 1,
      done_by_person: 1, skipped: 0, declined: 0, items: 34,
      baseline_seconds: 74, estimated_seconds_saved: 2294,
    },
    [VERIFIED_WORKFLOW]: {
      done_checked: 7, needed_person: 1, waiting: 0, wrong_record_prevented: 0,
      done_by_person: 0, skipped: 1, declined: 0, items: 8,
      baseline_seconds: 52, estimated_seconds_saved: 364,
    },
  },
  names: {
    [HALTED_WORKFLOW]: 'Claims review handoff',
    [VERIFIED_WORKFLOW]: 'Invoice review handoff',
  },
  recent: [
    { item_id: 'r1', batch_id: 'b', workflow_id: HALTED_WORKFLOW, position: 7, label: 'M-5530', status: 'open', result: 'check_record', nothing_written: false, execution_outcome: 'HALTED', transaction_outcome: 'RECONCILIATION_REQUIRED', finished_at: '2026-08-26T14:31:00Z', actions: [], task_name: 'Claims review handoff' },
    { item_id: 'r2', batch_id: 'b', workflow_id: HALTED_WORKFLOW, position: 6, label: 'M-5529', status: 'done', result: 'done_checked', nothing_written: false, execution_outcome: 'VERIFIED', transaction_outcome: 'VERIFIED', finished_at: '2026-08-26T14:29:00Z', actions: [], task_name: 'Claims review handoff' },
    { item_id: 'r3', batch_id: 'b', workflow_id: HALTED_WORKFLOW, position: 5, label: 'M-5528', status: 'done', result: 'done_checked', nothing_written: false, execution_outcome: 'VERIFIED', transaction_outcome: 'VERIFIED', finished_at: '2026-08-26T14:27:00Z', actions: [], task_name: 'Claims review handoff' },
    { item_id: 'r4', batch_id: 'b', workflow_id: HALTED_WORKFLOW, position: 3, label: 'M-4821', status: 'open', result: 'stopped_before_saving', nothing_written: true, execution_outcome: 'HALTED', transaction_outcome: 'HALTED_BEFORE_EFFECT', finished_at: '2026-08-26T14:12:00Z', actions: [], task_name: 'Claims review handoff' },
    { item_id: 'r5', batch_id: 'c', workflow_id: VERIFIED_WORKFLOW, position: 1, label: 'INV-2207', status: 'done', result: 'done_checked', nothing_written: false, execution_outcome: 'VERIFIED', transaction_outcome: 'VERIFIED', finished_at: '2026-08-26T13:48:00Z', actions: [], task_name: 'Invoice review handoff' },
  ],
};

export const TASK_INPUTS = {
  [HALTED_WORKFLOW]: {
    ok: true,
    workflow_id: HALTED_WORKFLOW,
    inputs: [
      { name: 'member_id', type: 'string', secret: false, required: true, example: 'M-4821', choices: [] },
      { name: 'status_note', type: 'string', secret: false, required: true, example: 'Reviewed', choices: [] },
    ],
    target: { backend: 'web', url: 'https://portal.example.test/claims' },
  },
  [VERIFIED_WORKFLOW]: {
    ok: true,
    workflow_id: VERIFIED_WORKFLOW,
    inputs: [
      { name: 'invoice_id', type: 'string', secret: false, required: true, example: 'INV-2207', choices: [] },
    ],
    target: { backend: 'web', url: 'https://billing.example.test' },
  },
  [NEW_WORKFLOW]: {
    ok: true,
    workflow_id: NEW_WORKFLOW,
    inputs: [],
    target: { backend: 'citrix', rdp_readiness_text: 'Referrals' },
  },
};

export const SAMPLE_RESPONSE = {
  ok: true,
  seconds: 71.4,
  runs: [
    {
      kind: 'normal',
      result: 'done_checked',
      nothing_written: false,
      execution_outcome: 'VERIFIED',
      transaction_outcome: 'VERIFIED',
      steps: [
        { index: 0, intent: "click 'Open'", ok: true, saves: false },
        { index: 1, intent: "click 'New Encounter'", ok: true, saves: false },
        { index: 2, intent: "click 'Triage'", ok: true, saves: false },
        { index: 3, intent: 'type <note>', ok: true, saves: false },
        { index: 4, intent: "click 'Save Encounter'", ok: true, saves: true },
      ],
      right_record_checks: 4,
      record_checks_confirmed: 2,
      record_checks_failed: 0,
      model_calls: 0,
      inputs: { note: 'Synthetic follow-up in two weeks' },
      screen: {
        ok: true,
        data_url: syntheticScreen({ banner: 'Saved: Synthetic follow-up in two weeks', member: 'Jane Sample   MRN P1   DOB 1980-01-01', note: 'Synthetic follow-up in two weeks', saved: true }),
        width: 1280,
        height: 800,
        focus: null,
      },
    },
    {
      kind: 'save_dropped',
      result: 'check_record',
      nothing_written: false,
      execution_outcome: 'HALTED',
      transaction_outcome: 'RECONCILIATION_REQUIRED',
      steps: [
        { index: 0, intent: "click 'Open'", ok: true, saves: false },
        { index: 1, intent: "click 'New Encounter'", ok: true, saves: false },
        { index: 2, intent: "click 'Triage'", ok: true, saves: false },
        { index: 3, intent: 'type <note>', ok: true, saves: false },
        { index: 4, intent: "click 'Save Encounter'", ok: false, saves: true },
      ],
      right_record_checks: 4,
      record_checks_confirmed: 0,
      record_checks_failed: 1,
      model_calls: 0,
      inputs: { note: 'Synthetic follow-up in two weeks' },
      screen: {
        ok: true,
        data_url: syntheticScreen({ banner: 'Saved: Synthetic follow-up in two weeks', member: 'Jane Sample   MRN P1   DOB 1980-01-01', note: 'Synthetic follow-up in two weeks', saved: false }),
        width: 1280,
        height: 800,
        focus: null,
      },
    },
  ],
};

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
// projection shapes it for the program map (@openadapt/program-graph). The six run steps above
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
export function ipcTable({ authenticated, workflows, permissions, operator = false }) {
  const attention = operator ? ATTENTION_ITEMS : [];
  return {
    get_task_inputs: TASK_INPUTS,
    list_attention_items: { ok: true, items: attention },
    get_attention_screenshot: ATTENTION_SCREENSHOT,
    get_results_summary: operator
      ? RESULTS_SUMMARY
      : { ok: true, total: { ...RESULTS_SUMMARY.total, done_checked: 0, needed_person: 0, waiting: 0, wrong_record_prevented: 0, done_by_person: 0, skipped: 0, items: 0 }, tasks: {}, names: {}, recent: [] },
    run_sample: SAMPLE_RESPONSE,
    propose_qualification_from_demo: { ok: false, error: 'Fixture: proposal not shown.' },
    get_auth_status: authenticated
      ? { authenticated: true, kind: 'ingest_token', host: 'app.openadapt.ai', org_id: 'org_demo' }
      : { authenticated: false },
    get_workflows: workflows,
    get_first_workflow_state: { ok: true, state: null },
    get_needs_attention: {
      // One count for the badge, the task cards, and the inbox.
      count: workflows.reduce((sum, w) => sum + (w.open_items || 0) + (w.open_halts || 0), 0),
      open_halts: workflows.some((w) => w.open_halts) ? 1 : 0,
      open_items: attention.length,
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
      deployment_lane: 'byoc',
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

export const IDS = { HALTED_WORKFLOW, VERIFIED_WORKFLOW, NEW_WORKFLOW };
