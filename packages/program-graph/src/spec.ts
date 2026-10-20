// The program-graph spec: the wire contract every OpenAdapt surface renders.
//
// openadapt-flow emits it (`openadapt-flow visualize --format json`, or
// `openadapt_flow.visualize.build_program_graph(...).model_dump(mode="json")`).
// Its JSON Schema is schema/program-graph-v2.json in this package, a
// byte-identical copy of packages/flow/schemas/program-graph-v2.json. Flow's
// test suite fails when the pydantic models, that schema, or the interfaces
// below drift apart (packages/flow/tests/test_program_graph_package.py).
//
// Optionality follows the schema: a key the schema requires is required here,
// and a key with a default is optional. Flow's own output carries every key.

export const SPEC_VERSION = 2;

/** The title Flow gives the terminal that ends the declared steps. */
export const DECLARED_STEPS_END_TITLE = "End of declared steps";

export type NodeKind =
  | "action"
  | "branch"
  | "business_decision"
  | "loop"
  | "subflow_call"
  | "child_bundle"
  | "terminal";

export type EdgeKind = "sequence" | "branch" | "exception" | "loop_body" | "handoff";

export const NODE_KINDS: readonly NodeKind[] = [
  "action",
  "branch",
  "business_decision",
  "loop",
  "subflow_call",
  "child_bundle",
  "terminal",
];

export const EDGE_KINDS: readonly EdgeKind[] = [
  "sequence",
  "branch",
  "exception",
  "loop_body",
  "handoff",
];

export interface ResolutionRung {
  name: string;
  label: string;
  present: boolean;
  detail?: string;
}

export interface ResolutionInfo {
  rungs?: ResolutionRung[];
  top_rung?: string | null;
}

export interface IdentityInfo {
  applicable?: boolean;
  armed?: boolean | null;
  reason?: string | null;
  phi_free?: boolean;
  has_structured?: boolean;
  has_identifier_crop?: boolean;
}

export interface EffectInfo {
  kind: string;
  summary: string;
  risk?: string;
  needs_operator_confirmation?: boolean;
}

export interface ParamInfo {
  name: string;
  type?: string;
  required?: boolean;
  secret?: boolean;
  example?: string | null;
  choices?: string[];
}

export interface ProvenanceInfo {
  compiler_version?: string;
  certified?: boolean;
  certification_status?: string | null;
  policy_name?: string | null;
  expires_at?: string | null;
  content_digest?: string | null;
  source_recording_sha256?: string | null;
}

export interface BundleMeta {
  name: string;
  schema_version: number;
  created_at?: string | null;
  viewport?: [number, number] | null;
  is_program?: boolean;
  is_composition?: boolean;
  composition_schema?: string | null;
  contains_phi?: boolean;
  phi_scrubbed?: boolean;
  encrypted?: boolean;
  step_count?: number;
  action_count?: number;
  irreversible_count?: number;
  identity_armed_count?: number;
  identity_unarmed_count?: number;
  effect_count?: number;
  api_binding_count?: number;
  halt_point_count?: number;
  child_count?: number;
  params?: ParamInfo[];
  provenance?: ProvenanceInfo;
}

export interface GraphNode {
  id: string;
  index: number;
  kind?: NodeKind;
  title: string;
  action?: string | null;
  risk?: string | null;
  risk_explanation?: string | null;
  risk_review_required?: boolean;
  param?: string | null;
  secret?: boolean;
  key?: string | null;
  resolution?: ResolutionInfo | null;
  identity?: IdentityInfo | null;
  effects?: EffectInfo[];
  has_api_binding?: boolean;
  api_summary?: string | null;
  postconditions?: string[];
  guard?: string | null;
  guard_on_unmet?: string | null;
  wait_until?: string | null;
  outcome?: string | null;
  reason?: string;
  halts?: string[];
  badges?: string[];
  surface?: string | null;
}

export interface GraphEdge {
  source: string;
  target: string;
  kind?: EdgeKind;
  label?: string;
  guard?: string | null;
}

export interface ProgramGraphSpec {
  spec_version?: number;
  bundle: BundleMeta;
  nodes?: GraphNode[];
  edges?: GraphEdge[];
}

/** A cheap structural check for data read from a file or a network response. */
export function isProgramGraphSpec(value: unknown): value is ProgramGraphSpec {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  const bundle = v.bundle as Record<string, unknown> | null | undefined;
  if (typeof bundle !== "object" || bundle === null) return false;
  if (typeof bundle.name !== "string") return false;
  if (v.nodes !== undefined && !Array.isArray(v.nodes)) return false;
  if (v.edges !== undefined && !Array.isArray(v.edges)) return false;
  if (v.spec_version !== undefined && typeof v.spec_version !== "number") return false;
  return true;
}

// Accessors that apply the schema defaults, so callers never branch on a
// missing key.
export const nodeKind = (node: GraphNode): NodeKind => node.kind ?? "action";
export const edgeKind = (edge: GraphEdge): EdgeKind => edge.kind ?? "sequence";
export const nodeHalts = (node: GraphNode): string[] => node.halts ?? [];
export const nodeEffects = (node: GraphNode): EffectInfo[] => node.effects ?? [];
export const nodePostconditions = (node: GraphNode): string[] => node.postconditions ?? [];
export const specNodes = (spec: ProgramGraphSpec): GraphNode[] => spec.nodes ?? [];
export const specEdges = (spec: ProgramGraphSpec): GraphEdge[] => spec.edges ?? [];
