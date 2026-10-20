// A guided tour over the workbench: a few stops, each selecting a step and
// moving the run trace to the moment that shows it. Every sentence is built
// from the spec and the trace, so a stop only appears when the program or the
// run has the thing it points at.

import { stopRuleWords } from "./present.js";
import { nodeKind, nodeHalts, specNodes, type GraphNode, type ProgramGraphSpec } from "./spec.js";
import { haltEvent, type RunTrace } from "./trace.js";

export interface TourStop {
  id: string;
  title: string;
  body: string;
  /** Node to select (a spec node id). */
  select?: string | null;
  /** Trace events applied at this stop. */
  cursor?: number | null;
  view?: "program" | "evidence" | "stops";
}

const firstEventIndex = (trace: RunTrace, test: (index: number) => boolean) => {
  for (let i = 0; i < trace.events.length; i += 1) if (test(i)) return i;
  return -1;
};

/**
 * The default stops: the recording, one step, the loop, the right-record
 * check, a stop, and the receipt. A stop the spec or trace cannot show is
 * left out.
 */
export function defaultTourStops(
  spec: ProgramGraphSpec,
  options: { trace?: RunTrace | null; title?: (node: GraphNode) => string } = {},
): TourStop[] {
  const { trace } = options;
  const title = options.title ?? ((node: GraphNode) => node.title);
  const nodes = [...specNodes(spec)].sort((a, b) => a.index - b.index);
  const actions = nodes.filter((node) => nodeKind(node) === "action");
  const stops: TourStop[] = [];
  const eventsAt = (id: string) => (trace ? firstEventIndex(trace, (i) => trace.events[i].node === id) : -1);
  const cursorAt = (id: string) => {
    const i = eventsAt(id);
    return i >= 0 ? i + 1 : null;
  };

  if (actions.length) {
    stops.push({
      id: "recording",
      title: "Recorded once",
      body: `OpenAdapt compiled these ${actions.length} steps from one recorded run of the task. Each card is a step, or a section of steps you can open.`,
      select: actions[0].id,
      cursor: trace ? 0 : null,
      view: "program",
    });
  }

  const step = actions.find((node) => (node.resolution?.rungs ?? []).filter((rung) => rung.present).length >= 2);
  if (step) {
    const kinds = (step.resolution?.rungs ?? []).filter((rung) => rung.present).map((rung) => rung.label.toLowerCase());
    stops.push({
      id: "step",
      title: "One step",
      body: `“${title(step)}” recorded ${kinds.length} ways to find its target: ${kinds.join(", ")}. A run tries the strongest first.`,
      select: step.id,
      cursor: cursorAt(step.id),
      view: "program",
    });
  }

  const loop = nodes.find((node) => nodeKind(node) === "loop");
  if (loop) {
    const iterations = trace ? new Set(trace.events.map((event) => event.iteration).filter((n) => n != null)).size : 0;
    const second = trace ? firstEventIndex(trace, (i) => trace.events[i].node === loop.id && trace.events[i].iteration === 2) : -1;
    stops.push({
      id: "loop",
      title: "The loop",
      body:
        iterations > 1
          ? `The steps run once for each item in the list. This run reached ${iterations} items; the dashed line is the return for the next one.`
          : "The steps run once for each item in the list; the dashed line is the return for the next one.",
      select: loop.id,
      cursor: second >= 0 ? second + 1 : cursorAt(loop.id),
      view: "program",
    });
  }

  const check = actions.find((node) => node.identity?.armed);
  if (check) {
    stops.push({
      id: "right-record",
      title: "The right-record check",
      body: "Before this step acts, it checks that the screen shows the right record. If it doesn’t, the run stops instead of acting.",
      select: check.id,
      cursor: cursorAt(check.id),
      view: "program",
    });
  }

  const halt = haltEvent(trace);
  if (trace && halt) {
    stops.push({
      id: "stop",
      title: "A stop",
      body: halt.reason ?? "The run stopped here, where a check failed.",
      select: halt.node,
      cursor: trace.events.indexOf(halt) + 1,
      view: "program",
    });
  } else {
    const guarded = actions.find((node) => nodeHalts(node).length > 0);
    if (guarded) {
      stops.push({
        id: "stop",
        title: "A stop rule",
        body: stopRuleWords(guarded).map((rule) => rule.text).join(" "),
        select: guarded.id,
        cursor: cursorAt(guarded.id),
        view: "program",
      });
    }
  }

  if (trace?.receipt) {
    stops.push({
      id: "receipt",
      title: trace.receipt.kind === "seal" ? "The Seal" : "The run record",
      body: trace.receipt.summary,
      select: halt?.node ?? null,
      cursor: trace.events.length,
      view: "program",
    });
  }
  return stops;
}
