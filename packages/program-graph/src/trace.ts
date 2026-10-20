// A run trace: what one recorded run did, step by step, so it can be played
// over the map. Steps light up as the run reaches them, a loop counts its
// iterations, and a stop shows where the run stopped and why.
//
// The JSON Schema is schema/run-trace-v1.json. A trace names spec node ids;
// it never carries the values a run typed.

import { STOP_NODE_ID } from "./fold.js";

export const TRACE_VERSION = 1;

export type TraceStatus = "ok" | "halted" | "skipped" | "failed";

export interface TraceEvent {
  /** Spec node id the run reached. */
  node: string;
  status: TraceStatus;
  /** 1-based iteration of the enclosing loop, when there is one. */
  iteration?: number | null;
  /** Milliseconds from the start of the run. */
  at_ms?: number | null;
  duration_ms?: number | null;
  /** The resolution rung that found the target (structural, template, ...). */
  rung?: string | null;
  identity?: "verified" | "not_verified" | null;
  screen_check?: "passed" | "failed" | null;
  effect?: "confirmed" | "refuted" | "indeterminate" | null;
  effect_checks?: { confirmed: number; total: number } | null;
  /** Why the run stopped or skipped here, in plain words. */
  reason?: string | null;
}

export interface RunTrace {
  trace_version: number;
  /** completed, halted, or the engine's own outcome word. */
  outcome: string;
  /** Short name for the run, such as "Recorded run, 4 referrals". */
  label?: string;
  total_ms?: number | null;
  events: TraceEvent[];
  /** Where the trace came from: files, commits, checksums. Free-form. */
  source?: Record<string, unknown>;
}

export type NodeRunStatus = "pending" | "active" | "done" | "halted" | "skipped";

export interface NodeRunState {
  status: NodeRunStatus;
  /** How many trace events reached this card so far. */
  visits: number;
  /** Distinct loop iterations that reached this card so far. */
  iterations: number;
  last?: TraceEvent;
  /** Every event that reached this card so far, oldest first. */
  events: TraceEvent[];
}

export interface TraceState {
  /** Number of events applied (0 = before the run starts). */
  cursor: number;
  nodes: Map<string, NodeRunState>;
  /** Edges the run has taken, as "source>target" display ids. */
  taken: Set<string>;
  /** The edge taken by the latest event. */
  latestEdge: string | null;
  current: TraceEvent | null;
  iteration: number | null;
  halted: TraceEvent | null;
  finished: boolean;
}

export function isRunTrace(value: unknown): value is RunTrace {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return typeof v.trace_version === "number" && typeof v.outcome === "string" && Array.isArray(v.events);
}

/** The event that stopped the run, if any. */
export function haltEvent(trace: RunTrace | null | undefined): TraceEvent | null {
  return trace?.events.find((event) => event.status === "halted") ?? null;
}

/**
 * Replay the first `cursor` events. `ownerOf` maps a spec node id to the card
 * that shows it (a fold), so a folded card lights up for any of its members.
 */
export function traceStateAt(
  trace: RunTrace,
  cursor: number,
  ownerOf?: ReadonlyMap<string, string>,
): TraceState {
  const end = Math.max(0, Math.min(cursor, trace.events.length));
  const owner = (id: string) => ownerOf?.get(id) ?? id;
  const nodes = new Map<string, NodeRunState>();
  const seenIterations = new Map<string, Set<number>>();
  const taken = new Set<string>();
  let latestEdge: string | null = null;
  let previous: string | null = null;
  let halted: TraceEvent | null = null;
  let iteration: number | null = null;

  for (let i = 0; i < end; i += 1) {
    const event = trace.events[i];
    const id = owner(event.node);
    const state = nodes.get(id) ?? { status: "pending", visits: 0, iterations: 0, events: [] };
    state.visits += 1;
    state.last = event;
    state.events.push(event);
    if (event.iteration != null) {
      const set = seenIterations.get(id) ?? new Set<number>();
      set.add(event.iteration);
      seenIterations.set(id, set);
      state.iterations = set.size;
      iteration = event.iteration;
    }
    if (event.status === "halted" || event.status === "failed") {
      state.status = "halted";
      halted = event;
    } else if (event.status === "skipped" && state.status === "pending") {
      state.status = "skipped";
    } else if (state.status !== "halted") {
      state.status = "done";
    }
    nodes.set(id, state);
    if (previous !== null && previous !== id) {
      latestEdge = `${previous}>${id}`;
      taken.add(latestEdge);
    } else {
      latestEdge = null;
    }
    previous = id;
    if (halted === event) {
      // The run left the program here: light the exit to the stop card.
      latestEdge = `${id}>${STOP_NODE_ID}`;
      taken.add(latestEdge);
      nodes.set(STOP_NODE_ID, { status: "halted", visits: 1, iterations: 0, last: event, events: [event] });
    }
  }

  const current = end > 0 ? trace.events[end - 1] : null;
  if (current) {
    const state = nodes.get(owner(current.node))!;
    if (state.status !== "halted") state.status = "active";
  }
  return {
    cursor: end,
    nodes,
    taken,
    latestEdge,
    current,
    iteration,
    halted,
    finished: end === trace.events.length,
  };
}

/** Milliseconds to wait before showing event `index` during playback. */
export function playbackDelay(trace: RunTrace, index: number, speed: number): number {
  const event = trace.events[index];
  const ms = event?.duration_ms;
  if (ms == null || !Number.isFinite(ms)) return 140;
  return Math.max(45, Math.min(700, ms / Math.max(speed, 0.1)));
}
