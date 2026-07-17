// Shared frontend types mirroring the engine's IPC payloads (spec §3).

export type DeploymentLane = "cloud" | "byoc";
export type PhiMode = "off" | "on";

export type StepState =
  | "pending"
  | "running"
  | "verified"
  | "halted"
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

export interface RunStep {
  index: number;
  action: string;
  target: string;
  state: StepState;
  latency_ms?: number | null;
  effect?: "verified" | "not_verified" | "checking" | null;
}

export interface RunReport {
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
}
